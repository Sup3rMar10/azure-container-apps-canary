from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Target:
    """One revision's share of an app's traffic."""

    revision: str
    percent: int


@dataclass(frozen=True)
class Revision:
    name: str
    active: bool  # an inactive revision runs nothing and cannot be sent traffic until it is activated
    running: str  # Running | Provisioning | Degraded | Failed | Stopped
    health: str  # Healthy | Unhealthy | None

    @property
    def ready(self) -> bool:
        return self.active and self.running == "Running" and self.health == "Healthy"


@dataclass
class ServiceState:
    mode: str  # "Single" or "Multiple" (the app's activeRevisionsMode)
    traffic: tuple[Target, ...] = ()  # revisions pinned BY NAME with a weight above zero
    latest_weight: int = 0  # weight on a `latestRevision: true` entry: new revisions take it automatically
    revisions: dict[str, Revision] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # label -> revision


@dataclass(frozen=True)
class Observation:
    """Requests a revision served in a window, and how many of them failed (5xx)."""

    requests: int
    errors: int

    @property
    def error_ratio(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


class DeployError(Exception):
    """The new revision could not be created, or never became healthy."""


class ContainerApp(Protocol):
    def describe(self, app: str) -> ServiceState: ...
    def deploy(self, app: str, image: str, suffix: str) -> str: ...  # creates a revision and returns its name
    def set_label(self, app: str, label: str, revision: str) -> None: ...
    def remove_label(self, app: str, label: str) -> None: ...
    def label_url(self, app: str, label: str) -> str: ...
    def set_traffic(self, app: str, targets: list[Target]) -> None: ...
    def activate(self, app: str, revision: str) -> None: ...
    def deactivate(self, app: str, revision: str) -> None: ...


class Metrics(Protocol):
    def observe(self, app: str, revision: str, window_seconds: int) -> Observation: ...


@dataclass
class SLO:
    max_error_ratio: float = 0.01  # absolute ceiling on the candidate's 5xx ratio
    relative_factor: float = 2.0  # and no worse than this multiple of the current revision's own ratio
    min_requests: int = 50  # fewer requests than this cannot prove anything


@dataclass
class RolloutResult:
    status: str  # promoted | rolled_back | aborted | rollback_failed
    reason: str
    revision: str | None = None
    previous: str | None = None
    steps_completed: list[int] = field(default_factory=list)
    final_traffic: tuple[Target, ...] = ()
