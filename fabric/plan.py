"""Change plan: what would change on each device versus what was last deployed."""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DevicePlan:
    device: str
    new_device: bool
    diff: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.diff)

    @property
    def added(self) -> int:
        return sum(1 for l in self.diff if l.startswith("+") and not l.startswith("+++"))

    @property
    def removed(self) -> int:
        return sum(1 for l in self.diff if l.startswith("-") and not l.startswith("---"))


def deployed_config(state_dir: Path, device: str) -> str | None:
    path = state_dir / f"{device}.cfg"
    return path.read_text() if path.exists() else None


def build_plan(configs: dict[str, str], state_dir: Path) -> dict[str, DevicePlan]:
    plan = {}
    for device, new in configs.items():
        old = deployed_config(state_dir, device)
        diff = list(difflib.unified_diff(
            (old or "").splitlines(), new.splitlines(),
            fromfile=f"{device} (deployed)", tofile=f"{device} (intended)", lineterm=""))
        plan[device] = DevicePlan(device, old is None, diff)
    return plan


def record_deployed(state_dir: Path, device: str, config: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{device}.cfg").write_text(config)
