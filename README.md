# azure-container-apps-canary

![ci](https://github.com/Sup3rMar10/azure-container-apps-canary/actions/workflows/ci.yml/badge.svg)

**A gradual rollout controller for Azure Container Apps that decides from evidence and always has a way back.** It creates a new revision with no traffic, waits for it to be healthy, tests it on its own label URL, shifts 10% / 50% / 100% of traffic, judges each step by the new revision's 5xx ratio, and sends everything back by itself if it looks bad. Most of the code is about what Container Apps does *differently* from a plain deploy: revision modes, `latestRevision` traffic rules, revision health, and inactive revisions.

> **What is verified, and what is not.** 110 tests pass locally on Python 3.13 (one more, the real-process SIGTERM test, runs on Linux only). The controller runs against a **fake Container App that enforces the API rules**, and also through the real Azure CLI wrapper against an **emulated `az`** that prints the JSON shapes the wrapper parses, so the controller, the wrapper and the parsing are exercised together. The app is a real container whose contract (port, health, non-root, clean exit on SIGTERM, vulnerability scan) CI checks on the actual image. I changed the controller, wrapper and metrics code on purpose sixteen times (refusals removed, read-back removed, readiness weakened, rollback skipping activation or cleanup, the wrong error category, and so on) and tests failed each time. **No Azure subscription is involved.** The fake, the emulator and every JSON sample are *my model of documented behaviour*; the `az` flags and output shapes were never run against Azure, `AzMonitorMetrics` never queried Azure Monitor, and `deploy.yml` was never run.

## The rollout (`aca_rollout/controller.py`)

```
refuse unless Multiple revision mode and no `latestRevision` traffic rule
  └▶ create revision (0% traffic) ─▶ wait Running + Healthy ─▶ label it ─▶ check the deploy moved no traffic
       └▶ smoke test the label URL ─▶ 10% ─▶ bake, judge ─▶ 50% ─▶ bake, judge ─▶ 100% ─▶ remove label
            any failure ─▶ make sure the previous revision is ACTIVE ─▶ 100% back (retried, read back) ─▶ clean up the candidate
```

Run `python -m aca_rollout simulate <scenario>` for any of: `good`, `bad-at-50`, `quiet`, `smoke-fails`, `unhealthy`, `deploy-fails`, `single-mode`, `latest-revision`, `leaky-deploy`, `late-rollback`.

## What is specific to Container Apps

| Situation | What the controller does | Tested with |
|---|---|---|
| **Single revision mode.** A new revision replaces the old one at once and the old one is switched off. | Refuses before deploying anything. | the fake models the replacement; a test shows what would have happened |
| **A `latestRevision: true` traffic rule.** Any new revision takes that traffic the moment it exists. | Refuses before deploying anything; also refuses if such a rule *reappears* mid-rollout (read-back). | both cases |
| **A revision that is created but not healthy.** It is Provisioning, then Running/Healthy, Failed, or stuck. | Waits (bounded), aborts on Failed or timeout with traffic untouched, deactivates the broken revision. | slow start, failed, stuck |
| **A deploy that moves traffic although it should not.** | Detects it after the deploy and puts traffic back before doing anything else. | `leaky-deploy` |
| **Labels.** One label, one revision; a label left by an aborted run would block the next. | Clears a stale label first; removes its own on every exit path. | stale label, every outcome |
| **An inactive revision cannot take traffic.** | A rollback (automatic or `rollback_to`) activates the target first and waits for it. | deactivated-then-rolled-back, never-comes-back |
| **Paying for old revisions.** | The previous revision is left active at 0% for a fast rollback; `deactivate_previous=True` stops it, and `rollback_to` knows to re-activate. | both |

The judging policy is the one from my Cloud Run canary: the candidate's 5xx ratio must stay under 1%, unless the *current* revision is itself worse (then up to twice its ratio, so a canary is not blamed for an outage it did not cause); **fewer than 50 requests at a step is a failure, not a pass**, after waiting longer twice.

## The fake, and the checks that keep it honest

`aca_rollout/fake.py` raises whenever the controller asks for something the API would refuse: weights that are not whole numbers or do not sum to 100, a revision that does not exist, is inactive or is not running and healthy, a label already used by another revision, removing a label that is not there. `tests/test_fake_azcli_metrics.py` covers each rule, the exact `az containerapp ...` flags the wrapper builds, the parsing of hand-written samples (including a `latestRevision` rule, an app with no ingress, an app that does not exist, zero-weight label entries), and the metrics query and its parsing.

## The container (`app/`)

A small service that honours the container contract as I understand it: listens on `$PORT` (default 8080, the ingress `targetPort` in `containerapp.yaml`), serves `/healthz`, logs JSON, runs as non-root, and on SIGTERM drains and exits 0. **CI builds the real image** and checks all of that, including that `docker stop` finishes in under 6 seconds with exit code 0, and runs a Trivy scan (critical, fixed vulnerabilities only).

## Things I got wrong or hit

- My first idea for the scenario "the previous revision is inactive" was unrealistic: the serving revision always has traffic, so it is never inactive mid-canary. The realistic case is a **manual rollback after promotion** with `deactivate_previous=True`, so I built `rollback_to` and made that the scenario.
- A mutation of mine that "swapped in" a different exception survived because it replaced the handler with the very exception the test injected (in a sibling project); here the mutation script reports "no change" when its pattern does not match, and one pattern did not match until I fixed the wording.
- `deactivate` is refused in the fake while a revision has traffic. That is a safety rule of my own, not a claim about Azure; the docstring says so.

## Honest limits

- **A model, not Azure.** Every rule in the fake, every `az` flag, every JSON field name (`activeRevisionsMode`, `ingress.traffic[].weight/label/latestRevision`, `properties.runningState/healthState`), the label URL form `app---label.<domain>` and the `Requests` metric with its `Revision` and `StatusCodeCategory` dimensions come from my reading of the documentation. Check them against a real app (`az containerapp show`, `az monitor metrics list-definitions`) before trusting any of it.
- **Metrics lag.** Azure Monitor takes a minute or more to ingest; a short bake can judge a window missing its newest requests, so keep bakes well above that.
- **Only 5xx ratio is judged.** No latency, saturation or business metric, and it needs enough traffic to mean anything (that is why a quiet step fails).
- **Revision names are assumed to be `<app>--<suffix>`**, which is how I understand `--revision-suffix` to work; the wrapper checks the revision exists after deploying and fails if not.
- The app must already exist, in Multiple mode, with traffic pinned by name; this tool does not create it (`containerapp.yaml` shows the shape, never applied). Jobs, Dapr, secrets, managed identity for the registry and VNet integration are not covered.
- `deploy.yml` (the ACR build and the `az` commands in it) is a reference pipeline, never run.

## Run it

```
python -m pip install pytest pyyaml
python -m pytest
python -m aca_rollout simulate good           # promoted
python -m aca_rollout simulate bad-at-50      # rolled back
python -m aca_rollout simulate late-rollback  # a manual rollback that re-activates the old revision first
docker build -t orders app && docker run --rm -e PORT=9090 -p 9090:9090 orders
```
