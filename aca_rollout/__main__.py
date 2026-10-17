"""python -m aca_rollout simulate [scenario] : watch a rollout against the fake Container App.

(`deploy` drives a REAL app through the Azure CLI. It exists but was never run against a live subscription.)
"""

from __future__ import annotations

import argparse
import time

from . import FakeContainerApp, Observation, Rollout, StaticMetrics

SCENARIOS = {
    "good": "a healthy release: 10%, 50%, 100%, promoted",
    "bad-at-50": "fine at 10%, but errors appear at 50%: rolled back",
    "quiet": "not enough traffic to judge: rolled back rather than promoted on no evidence",
    "smoke-fails": "the label URL fails its smoke test: traffic is never touched",
    "unhealthy": "the new revision fails to start: traffic is never touched",
    "deploy-fails": "the revision cannot be created: traffic is never touched",
    "single-mode": "the app is in Single revision mode: refused before anything is deployed",
    "latest-revision": "a traffic rule says latestRevision: refused before anything is deployed",
    "leaky-deploy": "the deploy itself moves traffic: put back at once",
    "late-rollback": "promoted with the old revision deactivated, then rolled back by hand: it is activated first",
}
PREV, NEW = "orders--v1", "orders--v2"


def build(name: str):
    ok, bad = Observation(1000, 2), Observation(1000, 120)
    cand = {"bad-at-50": [ok, bad], "quiet": [Observation(5, 0)]}.get(name, [ok])
    fake = FakeContainerApp(serving=PREV, new_revision=NEW, mode="Single" if name == "single-mode" else "Multiple",
                            latest_weight=100 if name == "latest-revision" else 0,
                            outcome="failed" if name == "unhealthy" else "healthy", fail_deploy=(name == "deploy-fails"),
                            leak_traffic_on_deploy=(name == "leaky-deploy"))
    metrics = StaticMetrics({NEW: cand, PREV: [Observation(1000, 1)]})

    def smoke(url: str):
        return (False, "GET /healthz returned 503") if name == "smoke-fails" else (True, "")

    return fake, Rollout(fake, metrics, smoke, bake_seconds=300, sleep=lambda s: None)


def main() -> int:
    p = argparse.ArgumentParser(prog="aca_rollout")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate")
    s.add_argument("scenario", nargs="?", default="good", choices=sorted(SCENARIOS))
    d = sub.add_parser("deploy", help="roll out to a REAL Container App (never run live by the author)")
    d.add_argument("--resource-group", required=True)
    d.add_argument("--app", required=True)
    d.add_argument("--image", required=True)
    d.add_argument("--resource-id", required=True, help="the container app's full Azure resource id, for the metrics")
    d.add_argument("--suffix", default=f"r{int(time.time())}", help="the new revision's name suffix")
    d.add_argument("--bake-seconds", type=int, default=300)
    a = p.parse_args()
    if a.cmd == "deploy":
        from .azcli import AzContainerApp
        from .live import AzMonitorMetrics, http_smoke

        ro = Rollout(AzContainerApp(a.resource_group), AzMonitorMetrics(a.resource_id), http_smoke, bake_seconds=a.bake_seconds)
        result = ro.run(a.app, a.image, a.suffix)
        print(f"{result.status.upper()}: {result.reason}")
        return 0 if result.status == "promoted" else 1
    fake, ro = build(a.scenario)
    ro.deactivate_previous = a.scenario == "late-rollback"
    print(f"scenario: {SCENARIOS[a.scenario]}")
    result = ro.run("orders", "myregistry.azurecr.io/orders:2", "v2")
    for call in fake.calls:
        if call[0] == "set_traffic":
            print("  traffic ->", ", ".join(f"{t.revision}={t.percent}%" for t in call[1]))
        else:
            print("  ", call[0], *call[1:])
    print(f"result: {result.status.upper()}: {result.reason}")
    print("final traffic:", ", ".join(f"{t.revision}={t.percent}%" for t in result.final_traffic) or "(none)")
    if a.scenario == "late-rollback":
        print()
        print(f"later, someone rolls back to {PREV}:")
        mark = len(fake.calls)
        back = ro.rollback_to("orders", PREV)
        for call in fake.calls[mark:]:
            print("  ", call[0], *(f"{t.revision}={t.percent}%" for t in call[1]) if call[0] == "set_traffic" else call[1:])
        print(f"result: {back.status.upper()}: {back.reason}")
        return 0 if back.status == "rolled_back" else 1
    return 0 if result.status == "promoted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
