"""The real client: drives Azure Container Apps through the Azure CLI. Never run against a live subscription.

The JSON shapes parsed here (properties.configuration.activeRevisionsMode, ingress.traffic[] with revisionName /
weight / label / latestRevision, ingress.fqdn, and a revision's properties.active / runningState / healthState) are
the ones I understand Azure's API to return. They are tested against hand-written samples of those shapes and a
fake command runner that checks the exact flags that matter, not against a live app.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

from .models import DeployError, Revision, ServiceState, Target

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def label_url(fqdn: str, app: str, label: str) -> str:
    """app.<env-domain> -> https://app---label.<env-domain>, the URL a label gets."""
    host, dot, rest = fqdn.partition(".")
    if not dot or host != app:
        raise DeployError(f"unexpected app address {fqdn!r}: cannot work out the URL for label {label!r}")
    return f"https://{host}---{label}.{rest}"


class AzContainerApp:
    def __init__(self, resource_group: str, runner: Runner = _run):
        self.group, self.runner = resource_group, runner

    def _az(self, *args: str, app: str) -> list[str]:
        return ["az", "containerapp", *args, "--name", app, "--resource-group", self.group]

    def _show(self, app: str) -> dict:
        return json.loads(self.runner(self._az("show", "--output", "json", app=app)))

    def describe(self, app: str) -> ServiceState:
        try:
            doc = self._show(app)
        except subprocess.CalledProcessError:  # the app does not exist
            return ServiceState("")
        config = doc.get("properties", {}).get("configuration", {})
        traffic, labels, latest = [], {}, 0
        for t in (config.get("ingress") or {}).get("traffic", []):
            weight = int(t.get("weight") or 0)
            if t.get("latestRevision"):
                latest += weight
            elif t.get("revisionName") and weight > 0:
                traffic.append(Target(t["revisionName"], weight))
            if t.get("label") and t.get("revisionName"):
                labels[t["label"]] = t["revisionName"]
        revisions = {}
        for r in json.loads(self.runner(self._az("revision", "list", "--output", "json", app=app))):
            p = r.get("properties", {})
            running = p.get("runningState") or p.get("provisioningState") or "Unknown"
            revisions[r["name"]] = Revision(r["name"], bool(p.get("active")), running, p.get("healthState") or "None")
        return ServiceState(config.get("activeRevisionsMode", "Single"), tuple(traffic), latest, revisions, labels)

    def deploy(self, app: str, image: str, suffix: str) -> str:
        try:
            # In Multiple revision mode this creates a new revision; the traffic rules are left as they were.
            self.runner(self._az("update", "--image", image, "--revision-suffix", suffix, "--output", "none", app=app))
        except subprocess.CalledProcessError as e:
            raise DeployError(e.stderr or str(e)) from e
        name = f"{app}--{suffix}"
        revisions = json.loads(self.runner(self._az("revision", "list", "--output", "json", app=app)))
        if name not in {r.get("name") for r in revisions}:
            raise DeployError(f"deployed, but there is no revision named {name!r}")
        return name

    def set_label(self, app: str, label: str, revision: str) -> None:
        self.runner(self._az("revision", "label", "add", "--label", label, "--revision", revision, app=app))

    def remove_label(self, app: str, label: str) -> None:
        self.runner(self._az("revision", "label", "remove", "--label", label, app=app))

    def label_url(self, app: str, label: str) -> str:
        fqdn = ((self._show(app).get("properties", {}).get("configuration", {}).get("ingress")) or {}).get("fqdn")
        if not fqdn:
            raise DeployError("the app has no ingress address")
        return label_url(fqdn, app, label)

    def set_traffic(self, app: str, targets: list[Target]) -> None:
        self.runner(self._az("ingress", "traffic", "set", "--revision-weight", *[f"{t.revision}={t.percent}" for t in targets], app=app))

    def activate(self, app: str, revision: str) -> None:
        self.runner(self._az("revision", "activate", "--revision", revision, app=app))

    def deactivate(self, app: str, revision: str) -> None:
        self.runner(self._az("revision", "deactivate", "--revision", revision, app=app))
