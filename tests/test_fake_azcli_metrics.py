import http.server
import json
import subprocess
import threading

import pytest

from aca_rollout import (ContainerAppRefused, DeployError, FakeContainerApp, Observation, Rollout, StaticMetrics, Target)
from aca_rollout.azcli import AzContainerApp, label_url
from aca_rollout.live import AzMonitorMetrics, http_smoke
from aca_rollout.metrics import monitor_command, parse_metrics

PREV, NEW = "orders--v1", "orders--v2"
FQDN = "orders.kind-sea-1a2b3c.westeurope.azurecontainerapps.io"


# --- the fake enforces the rules the controller must respect -------------------------------------------------------


def ready_pair():
    fake = FakeContainerApp(serving=PREV, new_revision=NEW)
    fake.deploy("orders", "image:new", "v2")
    for _ in range(5):
        fake.describe("orders")  # let the new revision finish provisioning
    return fake


def test_the_fake_refuses_what_azure_would_refuse():
    fake = ready_pair()
    with pytest.raises(ContainerAppRefused, match="sum to 100"):
        fake.set_traffic("orders", [Target(PREV, 60)])
    with pytest.raises(ContainerAppRefused, match="does not exist"):
        fake.set_traffic("orders", [Target("nope", 100)])
    with pytest.raises(ContainerAppRefused, match="at least one"):
        fake.set_traffic("orders", [])
    with pytest.raises(ContainerAppRefused, match="whole numbers"):
        fake.set_traffic("orders", [Target(PREV, 150), Target(NEW, -50)])
    with pytest.raises(ContainerAppRefused, match="whole numbers"):
        fake.set_traffic("orders", [Target(PREV, 50.5), Target(NEW, 49.5)])


def test_traffic_to_a_revision_that_is_still_provisioning_is_refused():
    fake = FakeContainerApp(serving=PREV, new_revision=NEW, provision_polls=9)
    fake.deploy("orders", "image:new", "v2")
    with pytest.raises(ContainerAppRefused, match="not running and healthy"):
        fake.set_traffic("orders", [Target(NEW, 10), Target(PREV, 90)])


def test_a_zero_weight_on_a_revision_that_is_not_ready_is_allowed():
    fake = FakeContainerApp(serving=PREV, new_revision=NEW, provision_polls=9)
    fake.deploy("orders", "image:new", "v2")
    fake.set_traffic("orders", [Target(PREV, 100), Target(NEW, 0)])
    assert fake.traffic == (Target(PREV, 100),)


def test_label_rules():
    fake = ready_pair()
    fake.set_label("orders", "candidate", NEW)
    fake.set_label("orders", "candidate", NEW)  # the same label on the same revision is fine
    with pytest.raises(ContainerAppRefused, match="already used"):
        fake.set_label("orders", "candidate", PREV)
    with pytest.raises(ContainerAppRefused, match="does not exist"):
        fake.set_label("orders", "x", "nope")
    fake.remove_label("orders", "candidate")
    with pytest.raises(ContainerAppRefused, match="no label"):
        fake.remove_label("orders", "candidate")
    with pytest.raises(ContainerAppRefused, match="no label"):
        fake.label_url("orders", "candidate")


def test_the_label_url_has_the_documented_shape():
    fake = ready_pair()
    fake.set_label("orders", "candidate", NEW)
    assert fake.label_url("orders", "candidate") == "https://orders---candidate.kind-sea-1a2b3c.westeurope.azurecontainerapps.io"


def test_a_revision_with_traffic_cannot_be_deactivated_and_an_inactive_one_cannot_take_traffic():
    fake = ready_pair()
    with pytest.raises(ContainerAppRefused, match="still has traffic"):
        fake.deactivate("orders", PREV)
    fake.deactivate("orders", NEW)
    with pytest.raises(ContainerAppRefused, match="inactive"):
        fake.set_traffic("orders", [Target(PREV, 50), Target(NEW, 50)])
    fake.activate("orders", NEW)
    for _ in range(3):
        fake.describe("orders")
    fake.set_traffic("orders", [Target(PREV, 50), Target(NEW, 50)])


def test_an_explicit_split_replaces_a_latest_revision_rule():
    fake = FakeContainerApp(serving=PREV, latest_weight=100, new_revision=NEW)
    fake.set_traffic("orders", [Target(PREV, 100)])
    assert fake.describe("orders").latest_weight == 0


# --- the Azure CLI wrapper: exact commands and parsing -----------------------------------------------------------------


def az_json(mode="Multiple", traffic=None, fqdn=FQDN):
    ingress = {"fqdn": fqdn, "traffic": traffic if traffic is not None else [
        {"revisionName": PREV, "weight": 90}, {"revisionName": NEW, "weight": 10, "label": "candidate"}]}
    return {"properties": {"configuration": {"activeRevisionsMode": mode, "ingress": ingress}}}


REVISIONS = [
    {"name": PREV, "properties": {"active": True, "runningState": "Running", "healthState": "Healthy", "provisioningState": "Provisioned"}},
    {"name": NEW, "properties": {"active": True, "provisioningState": "Provisioning"}},
    {"name": "orders--v0", "properties": {"active": False, "runningState": "Stopped", "healthState": "None"}},
]


class Runner:
    def __init__(self, show=None, revisions=None, fail_on=None):
        self.cmds, self.show, self.revisions, self.fail_on = [], show or az_json(), REVISIONS if revisions is None else revisions, fail_on

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: boom")
        if "show" in cmd:
            return json.dumps(self.show)
        if "list" in cmd:
            return json.dumps(self.revisions)
        return ""


def test_describe_reads_mode_traffic_labels_and_revisions():
    s = AzContainerApp("rg", Runner()).describe("orders")
    assert s.mode == "Multiple" and s.traffic == (Target(PREV, 90), Target(NEW, 10)) and s.latest_weight == 0
    assert s.labels == {"candidate": NEW}
    assert s.revisions[PREV].ready and not s.revisions["orders--v0"].active


def test_a_revision_with_no_running_state_falls_back_to_its_provisioning_state():
    new = AzContainerApp("rg", Runner()).describe("orders").revisions[NEW]
    assert (new.running, new.health, new.ready) == ("Provisioning", "None", False)


def test_a_latest_revision_rule_is_reported_and_not_mistaken_for_a_named_revision():
    runner = Runner(show=az_json(traffic=[{"latestRevision": True, "weight": 100}]))
    s = AzContainerApp("rg", runner).describe("orders")
    assert s.latest_weight == 100 and s.traffic == ()


def test_single_mode_is_the_default_when_the_api_does_not_say():
    doc = az_json()
    del doc["properties"]["configuration"]["activeRevisionsMode"]
    assert AzContainerApp("rg", Runner(show=doc)).describe("orders").mode == "Single"


def test_an_app_with_no_ingress_has_no_traffic():
    doc = {"properties": {"configuration": {"activeRevisionsMode": "Multiple", "ingress": None}}}
    assert AzContainerApp("rg", Runner(show=doc)).describe("orders").traffic == ()


def test_an_app_that_does_not_exist_describes_as_empty_so_the_controller_refuses_it():
    s = AzContainerApp("rg", Runner(fail_on="show")).describe("orders")
    assert s.mode == "" and s.traffic == ()
    ro = Rollout(AzContainerApp("rg", Runner(fail_on="show")), StaticMetrics({}), lambda u: (True, ""), sleep=lambda s: None)
    assert ro.run("orders", "img", "v2").status == "aborted"


def test_zero_weight_entries_are_not_counted_as_traffic():
    runner = Runner(show=az_json(traffic=[{"revisionName": PREV, "weight": 100}, {"revisionName": NEW, "weight": 0, "label": "candidate"}]))
    s = AzContainerApp("rg", runner).describe("orders")
    assert s.traffic == (Target(PREV, 100),) and s.labels == {"candidate": NEW}


def test_deploy_creates_a_revision_with_the_given_suffix_and_returns_its_name():
    runner = Runner()
    name = AzContainerApp("rg", runner).deploy("orders", "reg.azurecr.io/orders:2", "v2")
    cmd = runner.cmds[0]
    assert name == NEW and cmd[:3] == ["az", "containerapp", "update"]
    assert cmd[cmd.index("--image") + 1] == "reg.azurecr.io/orders:2" and cmd[cmd.index("--revision-suffix") + 1] == "v2"
    assert cmd[cmd.index("--name") + 1] == "orders" and cmd[cmd.index("--resource-group") + 1] == "rg"


def test_deploy_fails_clearly_when_the_command_fails_or_the_revision_is_missing():
    with pytest.raises(DeployError, match="boom"):
        AzContainerApp("rg", Runner(fail_on="update")).deploy("orders", "img", "v2")
    with pytest.raises(DeployError, match="no revision named"):
        AzContainerApp("rg", Runner(revisions=[])).deploy("orders", "img", "v2")


def test_label_traffic_activate_and_deactivate_use_the_exact_flags():
    runner = Runner()
    az = AzContainerApp("rg", runner)
    az.set_label("orders", "candidate", NEW)
    az.remove_label("orders", "candidate")
    az.set_traffic("orders", [Target(NEW, 10), Target(PREV, 90)])
    az.activate("orders", PREV)
    az.deactivate("orders", NEW)
    cmds = [" ".join(c) for c in runner.cmds]
    assert cmds[0].startswith("az containerapp revision label add") and "--label candidate --revision orders--v2" in cmds[0]
    assert cmds[1].startswith("az containerapp revision label remove") and "--label candidate" in cmds[1]
    assert cmds[2].startswith("az containerapp ingress traffic set --revision-weight orders--v2=10 orders--v1=90")
    assert cmds[3].startswith("az containerapp revision activate --revision orders--v1")
    assert cmds[4].startswith("az containerapp revision deactivate --revision orders--v2")
    assert all("--name orders" in c and "--resource-group rg" in c for c in cmds)


def test_the_label_url_is_built_from_the_app_address():
    assert label_url(FQDN, "orders", "candidate") == "https://orders---candidate.kind-sea-1a2b3c.westeurope.azurecontainerapps.io"
    assert AzContainerApp("rg", Runner()).label_url("orders", "candidate").startswith("https://orders---candidate.")


@pytest.mark.parametrize("fqdn", ["other.kind-sea.westeurope.azurecontainerapps.io", "orders", ""])
def test_an_unexpected_app_address_is_an_error_not_a_guess(fqdn):
    with pytest.raises(DeployError):
        AzContainerApp("rg", Runner(show=az_json(fqdn=fqdn))).label_url("orders", "candidate")


# --- the wrapper and the controller together, against an emulated CLI ---------------------------------------------------


class CliEmulator:
    """Runs the wrapper's `az containerapp ...` commands against a FakeContainerApp and prints the JSON shapes the
    wrapper parses, so the wrapper, the parsing and the controller are exercised together. Hand-written, not Azure."""

    def __init__(self, fake):
        self.fake, self.cmds = fake, []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        arg = lambda flag: cmd[cmd.index(flag) + 1]  # noqa: E731
        app, words = arg("--name"), " ".join(cmd)
        try:
            if "containerapp show" in words:
                s = self.fake.describe(app)
                traffic = [{"revisionName": t.revision, "weight": t.percent, **({"label": lab} if (lab := next((k for k, v in s.labels.items() if v == t.revision), None)) else {})}
                           for t in s.traffic]
                traffic += [{"revisionName": rev, "weight": 0, "label": lab} for lab, rev in s.labels.items() if rev not in {t.revision for t in s.traffic}]
                if s.latest_weight:
                    traffic.append({"latestRevision": True, "weight": s.latest_weight})
                return json.dumps({"properties": {"configuration": {"activeRevisionsMode": s.mode, "ingress": {"fqdn": f"{app}.{self.fake.domain}", "traffic": traffic}}}})
            if "revision list" in words:
                s = self.fake.describe(app)
                return json.dumps([{"name": r.name, "properties": {"active": r.active, "runningState": r.running, "healthState": r.health}} for r in s.revisions.values()])
            if "containerapp update" in words:
                self.fake.deploy(app, arg("--image"), arg("--revision-suffix"))
            elif "label add" in words:
                self.fake.set_label(app, arg("--label"), arg("--revision"))
            elif "label remove" in words:
                self.fake.remove_label(app, arg("--label"))
            elif "traffic set" in words:
                i = cmd.index("--revision-weight") + 1
                weights = [w.split("=") for w in cmd[i:] if "=" in w and not w.startswith("--")]
                self.fake.set_traffic(app, [Target(r, int(p)) for r, p in weights])
            elif "revision activate" in words:
                self.fake.activate(app, arg("--revision"))
            elif "revision deactivate" in words:
                self.fake.deactivate(app, arg("--revision"))
            else:
                raise AssertionError(f"unexpected command: {words}")
        except ContainerAppRefused as e:
            raise subprocess.CalledProcessError(1, cmd, stderr=str(e)) from e
        return ""


def through_the_cli(cand):
    fake = FakeContainerApp(serving=PREV, new_revision=NEW)
    emu = CliEmulator(fake)
    ro = Rollout(AzContainerApp("rg", emu), StaticMetrics({NEW: cand, PREV: [Observation(1000, 1)]}), lambda u: (True, ""), sleep=lambda s: None)
    return fake, emu, ro


def test_a_healthy_rollout_through_the_cli_wrapper_promotes():
    fake, emu, ro = through_the_cli([Observation(1000, 2)])
    r = ro.run("orders", "reg.azurecr.io/orders:2", "v2")
    assert r.status == "promoted" and fake.percent(NEW) == 100
    assert any("--revision-weight orders--v2=10 orders--v1=90" in " ".join(c) for c in emu.cmds)


def test_a_bad_rollout_through_the_cli_wrapper_rolls_back_and_cleans_up():
    fake, emu, ro = through_the_cli([Observation(1000, 120)])
    r = ro.run("orders", "reg.azurecr.io/orders:2", "v2")
    assert r.status == "rolled_back" and fake.percent(PREV) == 100
    assert "candidate" not in fake.labels and not fake.revisions[NEW]["active"]


def test_a_manual_rollback_through_the_cli_wrapper_reactivates_first():
    fake, emu, ro = through_the_cli([Observation(1000, 2)])
    ro.deactivate_previous = True
    assert ro.run("orders", "img", "v2").status == "promoted"
    emu.cmds.clear()
    assert ro.rollback_to("orders", PREV).status == "rolled_back"
    flat = [" ".join(c) for c in emu.cmds]
    assert next(i for i, c in enumerate(flat) if "revision activate" in c) < next(i for i, c in enumerate(flat) if "traffic set" in c)


# --- the metrics --------------------------------------------------------------------------------------------------------


def sample(**per_category):
    return json.dumps({"cost": 0, "value": [{"name": {"value": "Requests"}, "timeseries": [
        {"metadatavalues": [{"name": {"value": "statuscodecategory", "localizedValue": "Status Code Category"}, "value": cat}],
         "data": [{"timeStamp": f"2026-10-17T10:0{i}:00Z", "total": t} for i, t in enumerate(totals)]}
        for cat, totals in per_category.items()]}]})


def test_requests_and_5xx_are_summed_per_status_category():
    assert parse_metrics(sample(**{"2xx": [100, 120.0], "4xx": [5], "5xx": [3, 4]})) == Observation(232, 7)


def test_missing_totals_count_as_zero_and_an_empty_result_is_no_requests():
    assert parse_metrics(sample(**{"2xx": [None, 10], "5xx": [None]})) == Observation(10, 0)
    assert parse_metrics(json.dumps({"value": []})) == Observation(0, 0)
    assert parse_metrics(json.dumps({"value": [{"timeseries": []}]})) == Observation(0, 0)


def test_the_category_name_is_matched_case_insensitively():
    doc = json.loads(sample(**{"5XX": [9]}))
    doc["value"][0]["timeseries"][0]["metadatavalues"][0]["name"]["value"] = "StatusCodeCategory"
    assert parse_metrics(json.dumps(doc)) == Observation(9, 9)


def test_a_series_with_no_category_counts_toward_requests_but_not_errors():
    doc = {"value": [{"timeseries": [{"data": [{"total": 50}]}]}]}
    assert parse_metrics(json.dumps(doc)) == Observation(50, 0)


def test_the_metrics_query_names_the_metric_revision_and_split():
    cmd = monitor_command("/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/containerApps/orders", NEW, "a", "b")
    assert cmd[cmd.index("--metric") + 1] == "Requests" and cmd[cmd.index("--aggregation") + 1] == "Total"
    assert cmd[cmd.index("--filter") + 1] == f"Revision eq '{NEW}' and StatusCodeCategory eq '*'"
    assert cmd[cmd.index("--start-time") + 1] == "a" and cmd[cmd.index("--end-time") + 1] == "b"


def test_the_monitor_reader_asks_for_the_window_ending_now():
    seen = []
    m = AzMonitorMetrics("/res", runner=lambda c: (seen.append(c), sample(**{"2xx": [10], "5xx": [1]}))[1], now=lambda: 1_792_231_200)
    assert m.observe("orders", NEW, 600) == Observation(11, 1)
    cmd = seen[0]
    assert cmd[cmd.index("--end-time") + 1] == "2026-10-17T10:00:00Z" and cmd[cmd.index("--start-time") + 1] == "2026-10-17T09:50:00Z"


# --- the smoke test, against a real local HTTP server ----------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    statuses: list[int] = []

    def do_GET(self):  # noqa: N802
        code = Handler.statuses.pop(0) if len(Handler.statuses) > 1 else Handler.statuses[0]
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_the_smoke_test_passes_on_200(server):
    Handler.statuses = [200]
    assert http_smoke(server, sleep=lambda s: None) == (True, "")


def test_the_smoke_test_retries_a_fresh_revision_that_is_not_quite_up(server):
    Handler.statuses = [503, 503, 200]
    sleeps = []
    assert http_smoke(server, sleep=sleeps.append, pause=2.0) == (True, "") and sleeps == [2.0, 2.0]


def test_the_smoke_test_fails_with_the_last_status_after_its_attempts(server):
    Handler.statuses = [503]
    ok, detail = http_smoke(server, attempts=3, sleep=lambda s: None)
    assert not ok and "503" in detail


def test_the_smoke_test_reports_a_connection_failure():
    ok, detail = http_smoke("http://127.0.0.1:9", attempts=2, timeout=1, sleep=lambda s: None)
    assert not ok and "failed" in detail
