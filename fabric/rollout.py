"""Staged rollout with health gates.

Wave order:
  1. spines that have never been deployed (greenfield: leaves depend on them), one at a time
  2. canary: one changed leaf per platform, so both vendors are proven before going wide
  3. remaining leaves in batches
  4. already-deployed spines, one at a time (largest blast radius goes last)

Each device: snapshot -> apply (with device-side rollback timer where supported)
-> snapshot -> health gate -> confirm or roll back. Any failure halts the rollout;
devices in later waves are never touched.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from fabric.models import Device, Fabric
from fabric.render import device_context


class Driver(Protocol):
    def snapshot(self, dev: Device) -> dict: ...
    def apply(self, dev: Device, config: str, confirm_minutes: int) -> None: ...
    def confirm(self, dev: Device) -> None: ...
    def rollback(self, dev: Device) -> None: ...


@dataclass
class StepResult:
    device: str
    platform: str
    wave: str
    status: str  # confirmed | rolled_back | not_started
    pre: dict = field(default_factory=dict)
    post: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


@dataclass
class RolloutResult:
    steps: list[StepResult]
    halted: bool
    halt_reason: str = ""
    verify_problems: dict[str, list[str]] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.halted:
            return "HALTED"
        if self.verify_problems:
            return "VERIFY_FAILED"
        return "SUCCESS" if any(s.status == "confirmed" for s in self.steps) else "NO_CHANGES"


def build_waves(fab: Fabric, changed: list[str], deployed: set[str], batch_size: int = 2) -> list[tuple[str, list[Device]]]:
    devs = [d for d in fab.devices if d.name in changed]
    new_spines = [d for d in devs if d.role == "spine" and d.name not in deployed]
    old_spines = [d for d in devs if d.role == "spine" and d.name in deployed]
    leaves = [d for d in devs if d.role == "leaf"]

    canaries, seen_platforms = [], set()
    for d in leaves:
        if d.platform not in seen_platforms:
            canaries.append(d)
            seen_platforms.add(d.platform)
    rest = [d for d in leaves if d not in canaries]

    waves = [(f"spine-new-{d.name}", [d]) for d in new_spines]
    if canaries:
        waves.append(("canary", canaries))
    waves += [(f"leaves-{i // batch_size + 1}", rest[i:i + batch_size]) for i in range(0, len(rest), batch_size)]
    waves += [(f"spine-{d.name}", [d]) for d in old_spines]
    return waves


def expected_state(fab: Fabric, dev: Device, live: set[str]) -> dict:
    """What a healthy device should report, counting only peers that are already configured."""
    ctx = device_context(fab, dev)
    live_leaves = [d.name for d in fab.devices if d.role == "leaf" and d.name in live]
    return {
        "bgp_established": sum(1 for n in ctx["underlay"] + ctx["overlay"] if n["peer"] in live),
        "vtep_peers": len([n for n in live_leaves if n != dev.name]) if dev.role == "leaf" else 0,
    }


def health_problems(pre: dict, post: dict, expected: dict) -> list[str]:
    problems = []
    for key, want in expected.items():
        got = post.get(key, 0)
        if got < want:
            problems.append(f"{key}: expected >= {want}, got {got}")
        elif got < pre.get(key, 0):
            problems.append(f"{key}: regressed from {pre[key]} to {got}")
    return problems


def run_rollout(fab: Fabric, configs: dict[str, str], changed: list[str], deployed: set[str],
                driver: Driver, batch_size: int = 2, confirm_minutes: int = 5,
                settle_seconds: int = 0, poll_seconds: int = 15,
                on_confirmed=None, log=print) -> RolloutResult:
    """settle_seconds: how long a device may take to converge (BGP up, VTEPs learned)
    after a commit before the health gate fails it. 0 = check once (simulation)."""
    waves = build_waves(fab, changed, deployed, batch_size)
    live = set(deployed)
    steps: list[StepResult] = []
    halted, reason = False, ""

    for wave_name, wave_devs in waves:
        if halted:
            log(f"== wave {wave_name}: skipped (rollout halted)")
            steps += [StepResult(d.name, d.platform, wave_name, "not_started") for d in wave_devs]
            continue
        log(f"== wave {wave_name}: {', '.join(d.name for d in wave_devs)}")
        for dev in wave_devs:
            step = StepResult(dev.name, dev.platform, wave_name, "rolled_back")
            steps.append(step)
            try:
                step.pre = driver.snapshot(dev)
                driver.apply(dev, configs[dev.name], confirm_minutes)
                step.expected = expected_state(fab, dev, live | {dev.name})
                deadline = time.monotonic() + settle_seconds
                while True:
                    step.post = driver.snapshot(dev)
                    step.problems = health_problems(step.pre, step.post, step.expected)
                    if not step.problems or time.monotonic() >= deadline:
                        break
                    log(f"   {dev.name}: converging {step.post}, want {step.expected}")
                    time.sleep(poll_seconds)
            except Exception as exc:  # connection loss, commit error, parse error
                step.problems = [f"error: {exc}"]

            if step.problems:
                log(f"   {dev.name}: FAILED health gate -> rolling back ({'; '.join(step.problems)})")
                try:
                    driver.rollback(dev)
                except Exception as exc:
                    step.problems.append(f"rollback error: {exc} (device-side commit timer is the backstop)")
                halted, reason = True, f"{dev.name} failed in wave {wave_name}"
                break
            else:
                try:
                    driver.confirm(dev)
                except Exception as exc:
                    # A failed confirm must not crash the run: treat it as a failed device.
                    # (Junos also rolls back by itself when its commit-confirmed timer expires.)
                    step.problems = [f"confirm failed: {exc}"]
                    log(f"   {dev.name}: confirm FAILED -> rolling back ({exc})")
                    try:
                        driver.rollback(dev)
                    except Exception as rb_exc:
                        step.problems.append(f"rollback error: {rb_exc} (device-side commit timer is the backstop)")
                    halted, reason = True, f"{dev.name} could not be confirmed in wave {wave_name}"
                    break
                step.status = "confirmed"
                live.add(dev.name)
                if on_confirmed:
                    on_confirmed(dev.name)
                log(f"   {dev.name}: healthy {step.post} -> confirmed")
        # Devices later in a failed wave are skipped too.
        if halted:
            done = {s.device for s in steps}
            for d in wave_devs:
                if d.name not in done:
                    steps.append(StepResult(d.name, d.platform, wave_name, "not_started"))
    result = RolloutResult(steps, halted, reason)
    if not halted:
        result.verify_problems = verify_fabric(fab, driver, live, log)
    return result


def verify_fabric(fab: Fabric, driver: Driver, live: set[str], log=print) -> dict[str, list[str]]:
    """Final fabric-wide check: every configured device, against the full set of live peers.
    Catches what per-device gates cannot, e.g. a spine that was healthy before leaves existed."""
    log("== verify: fabric-wide post-check")
    problems = {}
    for dev in fab.devices:
        if dev.name not in live:
            continue
        try:
            found = health_problems({}, driver.snapshot(dev), expected_state(fab, dev, live))
        except Exception as exc:
            found = [f"error: {exc}"]
        if found:
            problems[dev.name] = found
            log(f"   {dev.name}: {'; '.join(found)}")
    if not problems:
        log(f"   all {len(live)} devices healthy")
    return problems
