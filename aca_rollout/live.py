"""The parts that talk to the outside world: an HTTP smoke test and an Azure Monitor metrics reader.

`http_smoke` is tested against a real local HTTP server. `AzMonitorMetrics` is NOT tested live (it needs an Azure
subscription); only the command it runs and the parsing of a sample result are tested.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from .azcli import _run
from .metrics import monitor_command, parse_metrics
from .models import Observation


def http_smoke(url: str, path: str = "/healthz", timeout: float = 10.0, attempts: int = 5, pause: float = 2.0,
               sleep=time.sleep) -> tuple[bool, str]:
    """GET url+path until it answers 200, a few times (a fresh revision may need a moment). Returns (ok, detail)."""
    last = "no attempt made"
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as resp:  # noqa: S310 - the URL comes from the app
                if resp.status == 200:
                    return True, ""
                last = f"GET {path} returned {resp.status}"
        except urllib.error.HTTPError as e:
            last = f"GET {path} returned {e.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"GET {path} failed: {e}"
        if i < attempts - 1:
            sleep(pause)
    return False, last


class AzMonitorMetrics:
    """Requests and 5xx for a revision over the last `window_seconds`, from Azure Monitor. Untested live."""

    def __init__(self, resource_id: str, runner: Callable[[list[str]], str] = _run, now: Callable[[], float] = time.time):
        self.resource_id, self.runner, self.now = resource_id, runner, now

    def observe(self, app: str, revision: str, window_seconds: int) -> Observation:
        end = self.now()
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        return parse_metrics(self.runner(monitor_command(self.resource_id, revision, iso(end - window_seconds), iso(end))))
