"""A gradual rollout for Azure Container Apps that decides from evidence and always has a way back.

  0. refuse to start unless the app is in Multiple revision mode and no traffic rule says `latestRevision: true`
     (either would hand the new revision real traffic the moment it exists)
  1. create the new revision, wait until it is Running and Healthy, then give it a label (its own URL)
  2. check the deploy itself did not move any traffic; smoke-test the label URL (a failure ends the rollout, untouched)
  3. shift traffic in steps (default 10, 50, 100 percent), letting each bake and then comparing the new revision's
     5xx ratio with an absolute ceiling AND with the current revision's own ratio
  4. too little traffic to judge counts as a failure, not a pass: no evidence is not good news
  5. on any failure, make sure the previous revision is ACTIVE, send 100% back to it (retried), and clean up the candidate
Every traffic change is read back and checked, because a call that returned success but did not take effect is how a
rollback silently fails.
"""

from __future__ import annotations

import time
from typing import Callable

from .models import SLO, ContainerApp, DeployError, Metrics, RolloutResult, Target

LABEL = "candidate"


class Rollout:
    def __init__(self, client: ContainerApp, metrics: Metrics, smoke: Callable[[str], tuple[bool, str]],
                 steps: tuple[int, ...] = (10, 50, 100), bake_seconds: int = 300, max_extensions: int = 2,
                 slo: SLO | None = None, sleep: Callable[[float], None] = time.sleep, rollback_attempts: int = 3,
                 ready_attempts: int = 30, ready_poll_seconds: int = 10, deactivate_previous: bool = False):
        if not steps or steps[-1] != 100 or list(steps) != sorted(set(steps)) or steps[0] < 1:
            raise ValueError("steps must be increasing percentages, starting above 0 and ending at 100")
        self.client, self.metrics, self.smoke = client, metrics, smoke
        self.steps, self.bake, self.max_extensions = steps, bake_seconds, max_extensions
        self.slo = slo or SLO()
        self.sleep = sleep
        self.rollback_attempts = rollback_attempts
        self.ready_attempts, self.ready_poll = ready_attempts, ready_poll_seconds
        self.deactivate_previous = deactivate_previous

    # --- helpers ---------------------------------------------------------------------------------------

    def _apply(self, app: str, targets: list[Target]) -> None:
        """Set the traffic split and read it back; raise unless it is exactly what was asked for."""
        self.client.set_traffic(app, targets)
        want = {(t.revision, t.percent) for t in targets if t.percent > 0}
        state = self.client.describe(app)
        got = {(t.revision, t.percent) for t in state.traffic}
        if want != got or state.latest_weight:
            raise RuntimeError(f"traffic did not take effect: wanted {sorted(want)}, got {sorted(got)}"
                               + (f" plus {state.latest_weight}% on latestRevision" if state.latest_weight else ""))

    def _wait_ready(self, app: str, name: str) -> None:
        for _ in range(self.ready_attempts):
            rev = self.client.describe(app).revisions.get(name)
            if rev is not None:
                if rev.running == "Failed":
                    raise DeployError(f"revision {name} failed to start ({rev.running}/{rev.health})")
                if rev.ready:
                    return
            self.sleep(self.ready_poll)
        raise DeployError(f"revision {name} was not Running and Healthy after {self.ready_attempts * self.ready_poll}s")

    def _best_effort(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - cleanup must not hide the real outcome
            pass

    def _drop_label(self, app: str) -> None:
        if LABEL in self.client.describe(app).labels:
            self.client.remove_label(app, LABEL)

    def _clean_candidate(self, app: str, revision: str | None) -> None:
        self._best_effort(self._drop_label, app)
        if revision:
            self._best_effort(self.client.deactivate, app, revision)  # stop paying for a revision nobody will use

    def _ensure_active(self, app: str, revision: str) -> None:
        rev = self.client.describe(app).revisions.get(revision)
        if rev is None:
            raise RuntimeError(f"the previous revision {revision} no longer exists")
        if not rev.active:  # a deactivated revision cannot take traffic until it is activated again
            self.client.activate(app, revision)
            self._wait_ready(app, revision)

    def _send_back(self, app: str, previous: str) -> str | None:
        """Make `previous` active and give it all the traffic, retrying. None on success, else the last error."""
        last = ""
        for _ in range(self.rollback_attempts):
            try:
                self._ensure_active(app, previous)
                self._apply(app, [Target(previous, 100)])
                return None
            except Exception as e:  # noqa: BLE001 - keep trying; the situation is already bad
                last = str(e)
                self.sleep(1)
        return last

    def _rollback(self, app: str, previous: str, why: str, revision: str, done: list[int]) -> RolloutResult:
        failure = self._send_back(app, previous)
        if failure is None:
            self._clean_candidate(app, revision)
            return RolloutResult("rolled_back", why, revision, previous, done, self.client.describe(app).traffic)
        return RolloutResult("rollback_failed", f"{why}; AND the rollback failed after {self.rollback_attempts} attempts ({failure}): "
                             "traffic may still be on the new revision, intervene manually", revision, previous, done,
                             self.client.describe(app).traffic)

    def rollback_to(self, app: str, revision: str) -> RolloutResult:
        """Send all traffic back to an older revision after a release was promoted. Activates it first if it was deactivated."""
        state = self.client.describe(app)
        current = max(state.traffic, key=lambda t: t.percent).revision if state.traffic else None
        if revision not in state.revisions:
            return RolloutResult("aborted", f"there is no revision named {revision!r} to go back to", current, revision,
                                 final_traffic=state.traffic)
        failure = self._send_back(app, revision)
        if failure is not None:
            return RolloutResult("rollback_failed", f"could not send traffic back to {revision} ({failure}): intervene manually",
                                 current, revision, final_traffic=self.client.describe(app).traffic)
        return RolloutResult("rolled_back", f"traffic is back on {revision}", current, revision, final_traffic=self.client.describe(app).traffic)

    def _judge(self, app: str, revision: str, previous: str, pct: int) -> tuple[bool, str]:
        for extension in range(self.max_extensions + 1):
            window = self.bake * (extension + 1)
            self.sleep(self.bake)
            cand = self.metrics.observe(app, revision, window)
            if cand.requests >= self.slo.min_requests:
                break
        else:
            return False, f"only {cand.requests} requests at {pct}% (need {self.slo.min_requests}): not enough traffic to judge"
        base = self.metrics.observe(app, previous, window)
        # The candidate is held to the SLO ceiling. If the current revision is itself over it (a bad dependency is
        # hurting the whole app) the ceiling relaxes to a multiple of the current revision's ratio, so a canary is
        # not blamed for an outage it did not cause. A baseline with too little traffic is ignored.
        limit = self.slo.max_error_ratio
        if base.requests >= self.slo.min_requests:
            limit = max(limit, base.error_ratio * self.slo.relative_factor)
        if cand.error_ratio > limit:
            return False, f"error ratio {cand.error_ratio:.2%} at {pct}% is over the limit {limit:.2%} (current revision: {base.error_ratio:.2%})"
        return True, ""

    # --- the rollout -----------------------------------------------------------------------------------

    def run(self, app: str, image: str, suffix: str) -> RolloutResult:
        state = self.client.describe(app)
        if not state.traffic:
            return RolloutResult("aborted", "the app has no serving revision: create it (with its first revision) before rolling out")
        serving = max(state.traffic, key=lambda t: t.percent).revision
        if state.mode != "Multiple":
            return RolloutResult("aborted", f"the app is in {state.mode or 'unknown'} revision mode, where a new revision replaces the "
                                 "old one at once; switch it to Multiple first. Nothing was deployed.", None, serving, final_traffic=state.traffic)
        if state.latest_weight:
            return RolloutResult("aborted", f"a traffic rule sends {state.latest_weight}% to latestRevision, so any new revision would "
                                 "take that traffic the moment it exists; pin the traffic to revisions by name first. Nothing was deployed.",
                                 None, serving, final_traffic=state.traffic)
        self._best_effort(self._drop_label, app)  # a label left behind by an earlier aborted run would block this one

        try:
            revision = self.client.deploy(app, image, suffix)
        except DeployError as e:
            return RolloutResult("aborted", f"deploy failed, traffic untouched: {e}", None, serving, final_traffic=self.client.describe(app).traffic)

        after = self.client.describe(app)
        if after.traffic != state.traffic or after.latest_weight:
            # Defence in depth: whatever the cause, the deploy changed who gets traffic. Put it back, loudly.
            result = self._rollback(app, serving, "the deploy itself moved traffic to the new revision before it was verified", revision, [])
            return result

        try:
            self._wait_ready(app, revision)
            self.client.set_label(app, LABEL, revision)
        except Exception as e:  # noqa: BLE001
            self._clean_candidate(app, revision)
            return RolloutResult("aborted", f"the new revision never became usable, traffic untouched: {e}", revision, serving,
                                 final_traffic=self.client.describe(app).traffic)

        ok, detail = self.smoke(self.client.label_url(app, LABEL))
        if not ok:
            self._clean_candidate(app, revision)
            return RolloutResult("aborted", f"smoke test failed on the label URL, traffic untouched: {detail}", revision, serving,
                                 final_traffic=self.client.describe(app).traffic)

        done: list[int] = []
        for pct in self.steps:
            try:
                targets = [Target(revision, pct)] + ([Target(serving, 100 - pct)] if pct < 100 else [])
                self._apply(app, targets)
                if pct == 100:
                    done.append(pct)
                    break
                good, why = self._judge(app, revision, serving, pct)
            except Exception as e:  # noqa: BLE001
                return self._rollback(app, serving, f"error while shifting traffic to {pct}%: {e}", revision, done)
            if not good:
                return self._rollback(app, serving, why, revision, done)
            done.append(pct)
        self._best_effort(self._drop_label, app)
        note = ""
        if self.deactivate_previous:
            try:
                self.client.deactivate(app, serving)
                note = f"; previous revision {serving} deactivated (rolling back would need it activated again)"
            except Exception as e:  # noqa: BLE001
                note = f"; could not deactivate the previous revision ({e}), it keeps running"
        else:
            note = f"; previous revision {serving} left active at 0% for a fast rollback"
        return RolloutResult("promoted", f"healthy through {done}{note}", revision, serving, done, self.client.describe(app).traffic)
