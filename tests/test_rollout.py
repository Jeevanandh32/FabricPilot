from conftest import ROOT
from fabric.drivers import SimDriver
from fabric.render import load_intent, render_all
from fabric.rollout import build_waves, run_rollout

FAB = load_intent(ROOT / "intent" / "fabric.yml")
CFG = render_all(FAB)
ALL = [d.name for d in FAB.devices]
LEAVES = ["leaf1", "leaf2", "leaf3", "leaf4"]


def rollout(changed, deployed=(), **sim):
    driver = SimDriver(FAB, set(deployed), **sim)
    return run_rollout(FAB, CFG, changed, set(deployed), driver, log=lambda *_: None), driver


def statuses(result):
    return {s.device: s.status for s in result.steps}


def test_greenfield_waves_spines_first_then_canary_per_vendor():
    waves = [(n, [d.name for d in ds]) for n, ds in build_waves(FAB, ALL, deployed=set())]
    assert waves == [("spine-new-spine1", ["spine1"]), ("spine-new-spine2", ["spine2"]),
                     ("canary", ["leaf1", "leaf3"]), ("leaves-1", ["leaf2", "leaf4"])]


def test_brownfield_waves_spines_last():
    waves = [n for n, _ in build_waves(FAB, ALL, deployed=set(ALL))]
    assert waves[0] == "canary" and waves[-2:] == ["spine-spine1", "spine-spine2"]


def test_greenfield_success():
    result, _ = rollout(ALL)
    assert result.status == "SUCCESS"
    assert set(statuses(result).values()) == {"confirmed"}


def test_canary_failure_rolls_back_and_halts():
    result, driver = rollout(LEAVES, deployed=ALL, fail_on={"leaf1"})
    assert result.status == "HALTED"
    assert statuses(result) == {"leaf1": "rolled_back", "leaf3": "not_started",
                                "leaf2": "not_started", "leaf4": "not_started"}
    touched = {dev for dev, _ in driver.events}
    assert touched == {"leaf1"}, "nothing beyond the failed canary may be touched"
    assert ("leaf1", "rollback") in driver.events


def test_failure_in_later_wave_keeps_earlier_confirmations():
    result, _ = rollout(LEAVES, deployed=ALL, fail_on={"leaf2"})
    s = statuses(result)
    assert s["leaf1"] == s["leaf3"] == "confirmed"
    assert s["leaf2"] == "rolled_back" and s["leaf4"] == "not_started"


def test_unreachable_device_halts_safely():
    result, _ = rollout(LEAVES, deployed=ALL, unreachable={"leaf3"})
    assert result.halted and "timed out" in result.steps[1].problems[0]


def test_rollback_restores_previous_health():
    _, driver = rollout(LEAVES, deployed=ALL, fail_on={"leaf1"})
    leaf1 = next(d for d in FAB.devices if d.name == "leaf1")
    assert driver.snapshot(leaf1)["bgp_established"] == 4


class SlowConvergingDriver(SimDriver):
    """BGP takes a few polls to come up after a commit, like a real device."""

    def __init__(self, *a, polls_needed=2, **kw):
        super().__init__(*a, **kw)
        self.polls_needed, self.polls = polls_needed, {}

    def snapshot(self, dev):
        n = self.polls.get(dev.name, 0)
        self.polls[dev.name] = n + 1
        if dev.name in self.live and n < self.polls_needed:
            return {"bgp_established": 0, "vtep_peers": 0}
        return super().snapshot(dev)


def test_gate_waits_for_convergence_instead_of_rolling_back():
    driver = SlowConvergingDriver(FAB, set(ALL))
    result = run_rollout(FAB, CFG, ["leaf1"], set(ALL), driver, settle_seconds=5, poll_seconds=0,
                         log=lambda *_: None)
    assert statuses(result) == {"leaf1": "confirmed"}


def test_gate_still_fails_when_device_never_converges():
    driver = SimDriver(FAB, set(ALL), fail_on={"leaf1"})
    result = run_rollout(FAB, CFG, ["leaf1"], set(ALL), driver, settle_seconds=0.05, poll_seconds=0,
                         log=lambda *_: None)
    assert statuses(result) == {"leaf1": "rolled_back"} and result.halted


class ConfirmFailsDriver(SimDriver):
    def confirm(self, dev):
        if dev.name == "leaf1":
            raise TimeoutError("Pattern not detected: Entering configuration mode")
        super().confirm(dev)


def test_failed_confirm_halts_and_reports_instead_of_crashing():
    # Found live: a confirm-step timeout crashed the whole run and no report was written.
    driver = ConfirmFailsDriver(FAB, set(ALL))
    result = run_rollout(FAB, CFG, LEAVES, set(ALL), driver, log=lambda *_: None)
    assert result.halted and "could not be confirmed" in result.halt_reason
    assert statuses(result)["leaf1"] == "rolled_back"
    assert ("leaf1", "rollback") in driver.events
    assert {d for d, _ in driver.events} == {"leaf1"}
