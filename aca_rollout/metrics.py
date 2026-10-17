"""Where the evidence comes from: Azure Monitor's `Requests` metric for a container app, split by status code category.

`monitor_command` builds the `az monitor metrics list` arguments and `parse_metrics` reads the JSON it prints. As I
understand Microsoft's documentation, the metric is `Requests` with the dimensions `Revision` and `StatusCodeCategory`
(values like 2xx, 4xx, 5xx); check them with `az monitor metrics list-definitions` before trusting this. Both are
tested against a hand-written sample of the documented output shape; a live query was never run. Azure Monitor also
has an ingestion delay of a minute or more, so a short bake can judge a window that is missing its newest requests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .models import Observation

METRIC = "Requests"
REVISION_DIMENSION = "Revision"
CATEGORY_DIMENSION = "StatusCodeCategory"


def monitor_command(resource_id: str, revision: str, start: str, end: str) -> list[str]:
    return ["az", "monitor", "metrics", "list", "--resource", resource_id, "--metric", METRIC,
            "--aggregation", "Total", "--start-time", start, "--end-time", end, "--interval", "PT1M",
            "--filter", f"{REVISION_DIMENSION} eq '{revision}' and {CATEGORY_DIMENSION} eq '*'", "--output", "json"]


def parse_metrics(text: str) -> Observation:
    """Sum the per-minute totals, and the part of them in the 5xx category."""
    doc = json.loads(text)
    total = errors = 0
    for metric in doc.get("value", []):
        for series in metric.get("timeseries", []):
            count = int(sum(p.get("total") or 0 for p in series.get("data", [])))
            category = next((m.get("value") for m in series.get("metadatavalues", [])
                             if str(m.get("name", {}).get("value", "")).lower() == CATEGORY_DIMENSION.lower()), None)
            total += count
            if str(category).lower() == "5xx":
                errors += count
    return Observation(total, errors)


@dataclass
class StaticMetrics:
    """Scripted observations: revision -> list of Observations, consumed in order (the last one repeats)."""

    script: dict[str, list[Observation]]
    seen: list[tuple[str, int]] = field(default_factory=list)
    _i: dict[str, int] = field(default_factory=dict)

    def observe(self, app: str, revision: str, window_seconds: int) -> Observation:
        self.seen.append((revision, window_seconds))
        seq = self.script[revision]
        i = self._i.get(revision, 0)
        self._i[revision] = i + 1
        return seq[min(i, len(seq) - 1)]
