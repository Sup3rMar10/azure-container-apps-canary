import pytest

from aca_rollout import (SLO, FakeContainerApp, Observation, Rollout, StaticMetrics, Target)

PREV, NEW = "orders--v1", "orders--v2"
OK = Observation(1000, 2)  # 0.2% errors over 1000 requests
BAD = Observation(1000, 120)  # 12%
POLL = 7  # a poll interval no other sleep uses, so waiting for readiness can be told apart from baking


def make(cand=(OK,), prev=(Observation(1000, 1),), fake=None, smoke=None, **kw):
    fake = fake or FakeContainerApp(serving=PREV, new_revision=NEW)
    metrics = StaticMetrics({NEW: list(cand), PREV: list(prev)})
    sleeps = []
    kw.setdefault("ready_poll_seconds", POLL)
    ro = Rollout(fake, metrics, smoke or (lambda url: (True, "")), sleep=sleeps.append, **kw)
    return fake, metrics, ro, sleeps


def run(ro):
    return ro.run("orders", "image:new", "v2")


def baked(sleeps, bake=300):
    return [s for s in sleeps if s == bake]


def set_traffic_calls(fake):
    return [c for c in fake.calls if c[0] == "set_traffic"]


# --- the healthy path -------------------------------------------------------------------------------------


def test_a_healthy_release_is_promoted_in_steps_and_ends_pinned_to_the_new_revision():
    fake, _, ro, _ = make()
    r = run(ro)
    assert r.status == "promoted" and r.steps_completed == [10, 50, 100]
    assert r.final_traffic == (Target(NEW, 100),) and r.revision == NEW and r.previous == PREV


def test_traffic_moves_exactly_10_then_50_then_100():
    fake, _, ro, _ = make()
    run(ro)
    assert [c[1] for c in set_traffic_calls(fake)] == [(Target(NEW, 10), Target(PREV, 90)), (Target(NEW, 50), Target(PREV, 50)), (Target(NEW, 100),)]


def test_the_new_revision_gets_no_traffic_until_after_the_smoke_test():
    order = []
    fake, _, ro, _ = make(smoke=lambda url: (order.append(("smoke", len(set_traffic_calls(fake)))) or True, ""))
    run(ro)
    assert order == [("smoke", 0)]


def test_the_smoke_test_hits_the_label_url_not_the_app_url():
    seen = []
    fake, _, ro, _ = make(smoke=lambda url: (seen.append(url) or True, ""))
    run(ro)
    assert seen == ["https://orders---candidate.kind-sea-1a2b3c.westeurope.azurecontainerapps.io"]


def test_each_step_bakes_before_it_is_judged_and_the_last_step_is_not_judged():
    fake, _, ro, sleeps = make(bake_seconds=120)
    run(ro)
    assert baked(sleeps, 120) == [120, 120]


def test_the_candidate_is_measured_before_the_previous_revision_at_each_step():
    fake, metrics, ro, _ = make()
    run(ro)
    assert [rev for rev, _ in metrics.seen] == [NEW, PREV, NEW, PREV]


def test_the_label_is_removed_and_the_previous_revision_is_left_active_for_a_fast_rollback():
    fake, _, ro, _ = make()
    r = run(ro)
    assert "candidate" not in fake.labels
    assert fake.revisions[PREV]["active"] and fake.percent(PREV) == 0
    assert "left active" in r.reason


def test_the_previous_revision_can_be_deactivated_after_promotion_to_stop_paying_for_it():
    fake, _, ro, _ = make(deactivate_previous=True)
    r = run(ro)
    assert r.status == "promoted" and not fake.revisions[PREV]["active"] and "deactivated" in r.reason


def test_a_failure_to_deactivate_the_previous_revision_does_not_undo_a_good_release():
    fake, _, ro, _ = make(deactivate_previous=True)
    fake.deactivate = lambda app, rev: (_ for _ in ()).throw(RuntimeError("busy"))
    r = run(ro)
    assert r.status == "promoted" and "could not deactivate" in r.reason and fake.percent(NEW) == 100


def test_custom_steps_are_honoured():
    fake, _, ro, sleeps = make(steps=(25, 100))
    r = run(ro)
    assert r.steps_completed == [25, 100] and len(baked(sleeps)) == 1


# --- refusing to start --------------------------------------------------------------------------------------


def test_single_revision_mode_is_refused_before_anything_is_deployed():
    fake, _, ro, _ = make(fake=FakeContainerApp(serving=PREV, mode="Single", new_revision=NEW))
    r = run(ro)
    assert r.status == "aborted" and "Single" in r.reason and "Nothing was deployed" in r.reason
    assert fake.calls == [] and fake.traffic == (Target(PREV, 100),)


def test_what_single_mode_would_have_done_if_the_controller_had_not_refused():
    fake = FakeContainerApp(serving=PREV, mode="Single", new_revision=NEW)
    fake.deploy("orders", "image:new", "v2")
    assert fake.percent(NEW) == 100 and not fake.revisions[PREV]["active"]  # instant cut-over, and the way back is switched off


def test_a_latest_revision_traffic_rule_is_refused_before_anything_is_deployed():
    fake, _, ro, _ = make(fake=FakeContainerApp(serving=PREV, latest_weight=100, new_revision=NEW))
    r = run(ro)
    assert r.status == "aborted" and "latestRevision" in r.reason and "Nothing was deployed" in r.reason
    assert fake.calls == []


def test_what_a_latest_revision_rule_would_have_done_if_the_controller_had_not_refused():
    fake = FakeContainerApp(serving=PREV, latest_weight=100, new_revision=NEW)
    fake.deploy("orders", "image:new", "v2")
    assert fake.percent(NEW) == 100  # real users, before any check


def test_an_app_with_no_serving_revision_is_refused():
    fake, _, ro, _ = make(fake=FakeContainerApp(serving=None, new_revision=NEW))
    r = run(ro)
    assert r.status == "aborted" and "no serving revision" in r.reason and fake.calls == []


def test_a_label_left_by_an_earlier_aborted_run_does_not_block_this_one():
    fake = FakeContainerApp(serving=PREV, new_revision=NEW)
    fake.revisions["orders--v0"] = {"image": "i", "active": False, "running": "Stopped", "health": "None", "polls": 0}
    fake.labels["candidate"] = "orders--v0"
    _, _, ro, _ = make(fake=fake)
    r = run(ro)
    assert r.status == "promoted" and "candidate" not in fake.labels


# --- the new revision has to become healthy first -----------------------------------------------------------


def test_a_slow_start_is_waited_for():
    fake, _, ro, sleeps = make(fake=FakeContainerApp(serving=PREV, new_revision=NEW, provision_polls=6))
    r = run(ro)
    assert r.status == "promoted" and sleeps.count(POLL) >= 3


def test_a_revision_that_fails_to_start_aborts_without_touching_traffic_and_is_deactivated():
    fake, _, ro, _ = make(fake=FakeContainerApp(serving=PREV, new_revision=NEW, outcome="failed"))
    r = run(ro)
    assert r.status == "aborted" and "failed to start" in r.reason
    assert fake.traffic == (Target(PREV, 100),) and not set_traffic_calls(fake) and not fake.revisions[NEW]["active"]


def test_a_revision_that_never_becomes_healthy_times_out_and_says_for_how_long():
    fake, _, ro, sleeps = make(fake=FakeContainerApp(serving=PREV, new_revision=NEW, outcome="stuck"), ready_attempts=3)
    r = run(ro)
    assert r.status == "aborted" and f"{3 * POLL}s" in r.reason
    assert fake.traffic == (Target(PREV, 100),) and not set_traffic_calls(fake)
    assert sleeps.count(POLL) == 3


def test_a_deploy_failure_leaves_traffic_exactly_as_it_was():
    fake, _, ro, _ = make(fake=FakeContainerApp(serving=PREV, fail_deploy=True, new_revision=NEW))
    r = run(ro)
    assert r.status == "aborted" and "could not be created" in r.reason
    assert r.final_traffic == (Target(PREV, 100),) and not set_traffic_calls(fake)


def test_a_failed_smoke_test_aborts_without_touching_traffic_and_cleans_up():
    fake, _, ro, _ = make(smoke=lambda url: (False, "GET /healthz returned 503"))
    r = run(ro)
    assert r.status == "aborted" and "503" in r.reason
    assert fake.traffic == (Target(PREV, 100),) and "candidate" not in fake.labels and not fake.revisions[NEW]["active"]
    assert not set_traffic_calls(fake)


def test_a_deploy_that_moves_traffic_by_itself_is_put_back_at_once():
    fake, _, ro, _ = make(fake=FakeContainerApp(serving=PREV, new_revision=NEW, leak_traffic_on_deploy=True))
    r = run(ro)
    assert r.status == "rolled_back" and "deploy itself moved traffic" in r.reason
    assert fake.traffic == (Target(PREV, 100),) and not fake.revisions[NEW]["active"]


# --- rolling back on evidence ----------------------------------------------------------------------------------


def test_errors_at_10_percent_roll_back_to_the_previous_revision():
    fake, _, ro, _ = make(cand=(BAD,))
    r = run(ro)
    assert r.status == "rolled_back" and r.steps_completed == [] and "10%" in r.reason
    assert fake.traffic == (Target(PREV, 100),)


def test_a_rollback_cleans_up_the_candidate_and_keeps_the_previous_revision_running():
    fake, _, ro, _ = make(cand=(BAD,))
    run(ro)
    assert "candidate" not in fake.labels and not fake.revisions[NEW]["active"] and fake.revisions[PREV]["active"]


def test_errors_that_only_appear_at_50_percent_still_roll_back_and_record_progress():
    fake, _, ro, _ = make(cand=(OK, BAD))
    r = run(ro)
    assert r.status == "rolled_back" and r.steps_completed == [10] and "50%" in r.reason
    assert fake.percent(PREV) == 100 and fake.percent(NEW) == 0


def test_not_enough_traffic_is_a_failure_not_a_pass():
    fake, metrics, ro, _ = make(cand=(Observation(5, 0),), bake_seconds=300)
    r = run(ro)
    assert r.status == "rolled_back" and "not enough traffic" in r.reason and "5 requests" in r.reason
    assert [w for rev, w in metrics.seen if rev == NEW] == [300, 600, 900], "it waits longer before giving up"


def test_a_quiet_start_that_picks_up_in_time_is_judged_on_the_larger_window():
    fake, _, ro, sleeps = make(cand=(Observation(10, 0), Observation(200, 0)))
    r = run(ro)
    assert r.status == "promoted" and baked(sleeps)[:2] == [300, 300]


@pytest.mark.parametrize("cand_errors,promoted", [(1, True), (10, True), (11, False), (80, False)])
def test_the_absolute_ceiling_is_one_percent_when_the_current_revision_is_healthy(cand_errors, promoted):
    fake, _, ro, _ = make(cand=(Observation(1000, cand_errors),), prev=(Observation(1000, 1),))
    assert run(ro).status == ("promoted" if promoted else "rolled_back")


def test_a_candidate_is_not_blamed_for_an_outage_the_whole_app_is_having():
    """Current revision at 5%: the ceiling relaxes to 2x that, so 8% passes and 12% does not."""
    unhealthy = Observation(1000, 50)
    fake, _, ro, _ = make(cand=(Observation(1000, 80),), prev=(unhealthy,))
    assert run(ro).status == "promoted"
    fake, _, ro, _ = make(cand=(Observation(1000, 120),), prev=(unhealthy,))
    assert run(ro).status == "rolled_back"


def test_a_baseline_with_too_little_traffic_is_ignored_not_trusted():
    fake, _, ro, _ = make(cand=(Observation(1000, 20),), prev=(Observation(5, 5),))
    assert run(ro).status == "rolled_back"  # 2% is over the 1% ceiling; the junk baseline did not excuse it


def test_a_stricter_slo_can_be_set():
    fake, _, ro, _ = make(cand=(Observation(1000, 5),), slo=SLO(max_error_ratio=0.001))
    assert run(ro).status == "rolled_back"


# --- when the rollback itself is in trouble ---------------------------------------------------------------------


class RollbackFlaky(FakeContainerApp):
    """Fails the first N attempts to send everything back to the previous revision."""

    def __init__(self, failures, **kw):
        super().__init__(**kw)
        self.failures = failures

    def set_traffic(self, app, targets):
        if targets == [Target(PREV, 100)] and self.failures > 0:
            self.failures -= 1
            raise RuntimeError("503 backend error")
        super().set_traffic(app, targets)


def test_a_transient_failure_during_rollback_is_retried():
    fake, _, ro, _ = make(cand=(BAD,), fake=RollbackFlaky(2, serving=PREV, new_revision=NEW))
    r = run(ro)
    assert r.status == "rolled_back" and fake.traffic == (Target(PREV, 100),)


def test_a_rollback_that_cannot_complete_is_reported_loudly_and_never_as_success():
    fake, _, ro, _ = make(cand=(BAD,), fake=RollbackFlaky(99, serving=PREV, new_revision=NEW))
    r = run(ro)
    assert r.status == "rollback_failed" and "intervene manually" in r.reason
    assert r.final_traffic and r.final_traffic[0].revision == NEW, "the report shows where traffic really is"


class Liar(FakeContainerApp):
    """Accepts a traffic change and silently does not apply it."""

    def set_traffic(self, app, targets):
        if any(t.revision == NEW and t.percent == 10 for t in targets):
            return
        super().set_traffic(app, targets)


def test_a_traffic_change_that_did_not_take_effect_is_caught_by_reading_it_back():
    fake, _, ro, _ = make(fake=Liar(serving=PREV, new_revision=NEW))
    r = run(ro)
    assert r.status == "rolled_back" and "did not take effect" in r.reason


class LatestComesBack(FakeContainerApp):
    """After a split is set, a latestRevision rule reappears (something else edited the app)."""

    def set_traffic(self, app, targets):
        super().set_traffic(app, targets)
        if any(t.revision == NEW and t.percent == 50 for t in targets):
            self.latest_weight = 30


def test_a_latest_revision_rule_that_reappears_mid_rollout_is_noticed_and_rolled_back():
    fake, _, ro, _ = make(fake=LatestComesBack(serving=PREV, new_revision=NEW))
    r = run(ro)
    assert r.status == "rolled_back" and "latestRevision" in r.reason and fake.percent(PREV) == 100


# --- going back after a release was promoted -------------------------------------------------------------------------


def promoted(deactivate_previous):
    fake, _, ro, _ = make(deactivate_previous=deactivate_previous)
    assert run(ro).status == "promoted"
    return fake, ro


def test_rolling_back_to_an_active_revision_just_moves_traffic():
    fake, ro = promoted(False)
    r = ro.rollback_to("orders", PREV)
    assert r.status == "rolled_back" and fake.percent(PREV) == 100 and ("activate", PREV) not in fake.calls


def test_rolling_back_to_a_deactivated_revision_activates_it_first_and_waits_for_it():
    fake, ro = promoted(True)
    assert not fake.revisions[PREV]["active"]
    fake.calls.clear()
    r = ro.rollback_to("orders", PREV)
    assert r.status == "rolled_back" and fake.percent(PREV) == 100
    assert [c[0] for c in fake.calls][:2] == ["activate", "set_traffic"]


def test_without_activating_the_fake_refuses_traffic_to_an_inactive_revision():
    fake, _ = promoted(True)
    with pytest.raises(Exception, match="inactive"):
        fake.set_traffic("orders", [Target(PREV, 100)])


def test_a_revision_that_cannot_be_reactivated_is_reported_as_a_failed_rollback():
    fake, ro = promoted(True)
    fake.outcome = "stuck"
    ro.ready_attempts = 2
    r = ro.rollback_to("orders", PREV)
    assert r.status == "rollback_failed" and "intervene manually" in r.reason and fake.percent(NEW) == 100


def test_rolling_back_to_a_revision_that_does_not_exist_is_refused_cleanly():
    fake, ro = promoted(False)
    r = ro.rollback_to("orders", "orders--nope")
    assert r.status == "aborted" and "no revision named" in r.reason and fake.percent(NEW) == 100


# --- the controller's own guards ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("steps", [(), (50,), (10, 100, 50), (0, 100), (10, 10, 100), (10, 90)])
def test_nonsense_step_lists_are_rejected(steps):
    with pytest.raises(ValueError):
        Rollout(FakeContainerApp(serving=PREV), StaticMetrics({}), lambda u: (True, ""), steps=steps)
