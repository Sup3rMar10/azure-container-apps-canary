"""An in-memory Azure Container App that enforces the rules a rollout controller must respect.

It models, from Microsoft's documented behaviour as I understand it (never from a live app):
  - in Single revision mode a new revision REPLACES the old one: it takes 100% and the old one is deactivated
  - in Multiple mode a new revision gets no traffic, UNLESS a traffic rule says `latestRevision: true`, in which case it
    takes that weight the moment it is created
  - a new revision is Provisioning for a while and then Running/Healthy, Failed, or stays stuck
  - traffic weights are whole numbers that sum to exactly 100, over revisions that exist, are active and are ready
  - a label points at one revision; labelling a second revision with a label already in use is refused; removing a
    label that is not there is refused; the label URL exists only while the label does
  - an inactive revision cannot receive traffic until it is activated
Two rules are my own safety guard, stricter than Azure may be: a revision that still has traffic cannot be deactivated.
If the controller asks for something refused here, this raises, so a controller bug shows up in a test, not in
production. It is a model: a real app has more rules and more failure modes than are listed.
"""

from __future__ import annotations

from .models import DeployError, Revision, ServiceState, Target


class ContainerAppRefused(Exception):
    """What a real API call would reject (or what my guard forbids)."""


class FakeContainerApp:
    def __init__(self, serving: str | None = None, mode: str = "Multiple", latest_weight: int = 0, outcome: str = "healthy",
                 provision_polls: int = 2, fail_deploy: bool = False, new_revision: str | None = None,
                 leak_traffic_on_deploy: bool = False, flaky_traffic_calls: int = 0, domain: str = "kind-sea-1a2b3c.westeurope.azurecontainerapps.io"):
        self.mode, self.latest_weight, self.outcome = mode, latest_weight, outcome  # outcome: healthy | failed | stuck
        self.provision_polls, self.fail_deploy, self.new_revision = provision_polls, fail_deploy, new_revision
        self.leak_traffic_on_deploy = leak_traffic_on_deploy  # a deploy that moves traffic although the rules say it should not
        self.flaky_traffic_calls = flaky_traffic_calls
        self.domain = domain
        self.revisions: dict[str, dict] = {}
        self.traffic: tuple[Target, ...] = ()
        self.labels: dict[str, str] = {}
        self.calls: list[tuple] = []
        if serving:
            self.revisions[serving] = {"image": "image:" + serving, "active": True, "running": "Running", "health": "Healthy", "polls": 0}
            self.traffic = (Target(serving, 100),)

    # --- reading ------------------------------------------------------------------------------------------

    def describe(self, app: str) -> ServiceState:
        for r in self.revisions.values():  # time passes between polls
            if r["running"] == "Provisioning" and r["active"] and self.outcome != "stuck":
                r["polls"] -= 1
                if r["polls"] <= 0:
                    r["running"], r["health"] = ("Running", "Healthy") if self.outcome == "healthy" else ("Failed", "Unhealthy")
        revs = {n: Revision(n, r["active"], r["running"], r["health"]) for n, r in self.revisions.items()}
        return ServiceState(self.mode, self.traffic, self.latest_weight, revs, dict(self.labels))

    # --- acting -------------------------------------------------------------------------------------------

    def deploy(self, app: str, image: str, suffix: str) -> str:
        self.calls.append(("deploy", image, suffix))
        if self.fail_deploy:
            raise DeployError("the revision could not be created")
        name = self.new_revision or f"{app}--{suffix}"
        self.revisions[name] = {"image": image, "active": True, "running": "Provisioning", "health": "None", "polls": self.provision_polls}
        if not self.traffic:
            self.traffic = (Target(name, 100),)
        elif self.mode == "Single":
            for other in self.revisions:
                if other != name:
                    self.revisions[other].update(active=False, running="Stopped", health="None")
            self.traffic = (Target(name, 100),)
        elif self.latest_weight or self.leak_traffic_on_deploy:
            self.traffic = (Target(name, 100),)
        return name

    def set_label(self, app: str, label: str, revision: str) -> None:
        self.calls.append(("set_label", label, revision))
        if revision not in self.revisions:
            raise ContainerAppRefused(f"revision {revision!r} does not exist")
        if self.labels.get(label, revision) != revision:
            raise ContainerAppRefused(f"label {label!r} is already used by {self.labels[label]!r}; remove it first")
        self.labels[label] = revision

    def remove_label(self, app: str, label: str) -> None:
        self.calls.append(("remove_label", label))
        if label not in self.labels:
            raise ContainerAppRefused(f"there is no label {label!r} to remove")
        del self.labels[label]

    def label_url(self, app: str, label: str) -> str:
        if label not in self.labels:
            raise ContainerAppRefused(f"no label {label!r}")
        host, _, rest = f"{app}.{self.domain}".partition(".")
        return f"https://{host}---{label}.{rest}"

    def set_traffic(self, app: str, targets: list[Target]) -> None:
        self.calls.append(("set_traffic", tuple(targets)))
        if self.flaky_traffic_calls > 0:
            self.flaky_traffic_calls -= 1
            raise RuntimeError("503 backend error")
        if not targets:
            raise ContainerAppRefused("traffic must name at least one revision")
        if any(not isinstance(t.percent, int) or isinstance(t.percent, bool) or not 0 <= t.percent <= 100 for t in targets):
            raise ContainerAppRefused("weights must be whole numbers from 0 to 100")
        if sum(t.percent for t in targets) != 100:
            raise ContainerAppRefused(f"traffic weights must sum to 100, got {sum(t.percent for t in targets)}")
        for t in targets:
            rev = self.revisions.get(t.revision)
            if rev is None:
                raise ContainerAppRefused(f"revision {t.revision!r} does not exist")
            if not rev["active"]:
                raise ContainerAppRefused(f"revision {t.revision!r} is inactive; activate it first")
            if t.percent > 0 and not (rev["running"] == "Running" and rev["health"] == "Healthy"):
                raise ContainerAppRefused(f"revision {t.revision!r} is not running and healthy")
        self.latest_weight = 0  # an explicit split by revision name replaces any latestRevision rule
        self.traffic = tuple(t for t in targets if t.percent > 0)

    def activate(self, app: str, revision: str) -> None:
        self.calls.append(("activate", revision))
        if revision not in self.revisions:
            raise ContainerAppRefused(f"revision {revision!r} does not exist")
        r = self.revisions[revision]
        if not r["active"]:
            r.update(active=True, running="Provisioning", health="None", polls=1)

    def deactivate(self, app: str, revision: str) -> None:
        self.calls.append(("deactivate", revision))
        if revision not in self.revisions:
            raise ContainerAppRefused(f"revision {revision!r} does not exist")
        if any(t.revision == revision for t in self.traffic):
            raise ContainerAppRefused(f"revision {revision!r} still has traffic (a guard of this fake, not necessarily Azure's)")
        self.revisions[revision].update(active=False, running="Stopped", health="None")

    # helpers for assertions
    def percent(self, revision: str) -> int:
        return sum(t.percent for t in self.traffic if t.revision == revision)
