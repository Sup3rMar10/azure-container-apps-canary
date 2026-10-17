"""The app definition the controller depends on, and the pipeline's own permissions."""

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load((ROOT / "containerapp.yaml").read_text(encoding="utf-8"))


def props(spec):
    return spec["properties"]


# --- containerapp.yaml: the settings the rollout controller relies on ---------------------------------------------


def test_the_app_is_in_multiple_revision_mode(spec):
    assert props(spec)["configuration"]["activeRevisionsMode"] == "Multiple"


def test_traffic_is_pinned_to_revisions_by_name_and_never_to_latest(spec):
    traffic = props(spec)["configuration"]["ingress"]["traffic"]
    assert traffic and all("revisionName" in t and "latestRevision" not in t for t in traffic)
    assert "latestRevision" not in (ROOT / "containerapp.yaml").read_text(encoding="utf-8").replace("`latestRevision: true`", "").replace("never `latestRevision", "")
    assert sum(t["weight"] for t in traffic) == 100


def test_the_pinned_revision_is_the_one_the_template_creates(spec):
    p = props(spec)
    assert p["configuration"]["ingress"]["traffic"][0]["revisionName"] == f"{spec['name']}--{p['template']['revisionSuffix']}"


def test_the_ingress_targets_the_port_the_app_listens_on(spec):
    assert props(spec)["configuration"]["ingress"]["targetPort"] == 8080
    assert 'os.environ.get("PORT", "8080")' in (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def test_the_ingress_refuses_plain_http(spec):
    assert props(spec)["configuration"]["ingress"]["allowInsecure"] is False


def test_all_three_probes_check_the_health_endpoint_on_the_apps_port(spec):
    probes = {p["type"]: p for p in props(spec)["template"]["containers"][0]["probes"]}
    assert set(probes) == {"Startup", "Liveness", "Readiness"}
    assert all(p["httpGet"] == {"path": "/healthz", "port": 8080} for p in probes.values())


def test_there_is_a_warm_replica_and_a_cap_on_scale_and_resource_limits(spec):
    scale = props(spec)["template"]["scale"]
    assert scale["minReplicas"] >= 1 and 1 <= scale["maxReplicas"] <= 100
    res = props(spec)["template"]["containers"][0]["resources"]
    assert res["cpu"] and res["memory"]


def test_the_termination_grace_period_is_set_and_longer_than_the_app_needs_to_drain(spec):
    assert 10 <= props(spec)["template"]["terminationGracePeriodSeconds"] <= 600   # the app drains in seconds (CI checks < 6)


def test_image_and_environment_are_placeholders_the_pipeline_must_replace(spec):
    assert props(spec)["template"]["containers"][0]["image"] == "IMAGE" and props(spec)["managedEnvironmentId"] == "ENVIRONMENT_ID"


def test_no_environment_variable_looks_like_a_secret(spec):
    names = [e["name"] for e in props(spec)["template"]["containers"][0].get("env", [])]
    assert not [n for n in names if re.search(r"(password|secret|token|key|connection)", n, re.I)]


def test_the_dockerfile_does_not_run_as_root():
    text = (ROOT / "app" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001" in text and text.index("USER 10001") > text.index("COPY")


# --- the workflows ----------------------------------------------------------------------------------------------------


def wf(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def logs_in(job):
    return any("azure/login" in str(s.get("uses", "")) for s in job.get("steps", []))


def test_there_are_workflows_to_check():
    assert {p.name for p in FILES} >= {"ci.yml", "deploy.yml"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_workflow_permissions_default_to_read_only(path):
    assert wf(path).get("permissions") == {"contents": "read"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_stored_azure_credentials_and_no_pull_request_target(path):
    text = path.read_text(encoding="utf-8")
    for needle in ("creds:", "AZURE_CREDENTIALS", "client-secret", "ARM_CLIENT_SECRET", "AZURE_CLIENT_SECRET", "pull_request_target"):
        assert needle not in text


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_only_jobs_that_log_in_to_azure_may_mint_an_oidc_token(path):
    for name, job in wf(path)["jobs"].items():
        assert (job.get("permissions", {}).get("id-token") == "write") == logs_in(job), f"{path.name}:{name}"


def test_the_deploy_job_is_gated_protected_and_takes_identifiers_from_variables():
    job = wf(WORKFLOWS / "deploy.yml")["jobs"]["rollout"]
    assert "AZURE_DEPLOY_CLIENT_ID" in job["if"] and job["environment"] == "production"
    login = next(s for s in job["steps"] if "azure/login" in str(s.get("uses", "")))
    assert all(login["with"][k].startswith("${{ vars.") for k in ("client-id", "tenant-id", "subscription-id"))


def test_the_deploy_job_runs_the_rollout_command():
    text = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
    assert "python -m aca_rollout deploy" in text and "--resource-id" in text


def test_ci_checks_the_container_contract_on_the_real_image():
    ci = wf(WORKFLOWS / "ci.yml")
    assert {"test", "container", "workflows"} <= set(ci["jobs"])
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    for needle in ("docker stop", "id -u", "stopped cleanly", "trivy"):
        assert needle in text


def test_the_simulation_scenarios_all_run_without_error():
    from aca_rollout.__main__ import SCENARIOS, main
    import sys
    for name in SCENARIOS:
        old = sys.argv
        sys.argv = ["aca_rollout", "simulate", name]
        try:
            assert main() in (0, 1), name
        finally:
            sys.argv = old
