"""Device drivers.

SimDriver   - in-memory fabric model for demos and tests; can inject failures.
NetmikoDriver - real NX-OS / Junos over SSH. Written against documented CLI/JSON
                output but NOT yet validated on a live lab; see README "Status".
"""
from __future__ import annotations

import json
import os
import re
import time

from fabric.models import Device, Fabric


class SimDriver:
    """Simulates a perfect network: a configured device has every session to every
    configured neighbor up. Devices in `fail_on` come up broken after a change
    (as if a bad route-target or MTU was pushed)."""

    def __init__(self, fab: Fabric, deployed: set[str], fail_on: set[str] | None = None,
                 unreachable: set[str] | None = None):
        self.fab = fab
        self.live = set(deployed)
        self.broken: set[str] = set()
        self.fail_on = fail_on or set()
        self.unreachable = unreachable or set()
        self.events: list[tuple[str, str]] = []
        self._was_live: dict[str, bool] = {}

    def snapshot(self, dev: Device) -> dict:
        from fabric.rollout import expected_state
        if dev.name not in self.live or dev.name in self.broken:
            return {"bgp_established": 0, "vtep_peers": 0}
        healthy_live = self.live - self.broken
        return expected_state(self.fab, dev, healthy_live)

    def apply(self, dev: Device, config: str, confirm_minutes: int) -> None:
        if dev.name in self.unreachable:
            raise ConnectionError(f"SSH to {dev.mgmt_ip} timed out")
        self._was_live[dev.name] = dev.name in self.live
        self.live.add(dev.name)
        if dev.name in self.fail_on:
            self.broken.add(dev.name)
        self.events.append((dev.name, f"apply ({len(config.splitlines())} lines, auto-rollback in {confirm_minutes}m)"))

    def confirm(self, dev: Device) -> None:
        self.events.append((dev.name, "confirm"))

    def rollback(self, dev: Device) -> None:
        self.broken.discard(dev.name)
        if not self._was_live.get(dev.name, False):
            self.live.discard(dev.name)
        self.events.append((dev.name, "rollback"))


class NetmikoDriver:
    """Credentials come from the environment, never from the repo:
    FABRIC_USER, FABRIC_PASSWORD, with optional per-platform overrides
    FABRIC_PASSWORD_NXOS / FABRIC_PASSWORD_JUNOS (lab images ship different defaults)."""

    DEVICE_TYPES = {"nxos": "cisco_nxos", "junos": "juniper_junos"}
    COMMANDS = {
        "nxos": {
            "bgp": ["show bgp ipv4 unicast summary | json", "show bgp l2vpn evpn summary | json"],
            "vtep": "show nve peers | json",
        },
        "junos": {
            "bgp": ["show bgp summary | display json"],
            "vtep": "show ethernet-switching vxlan-tunnel-end-point remote | display json",
        },
    }

    # Virtual switches answer slowly right after boot; Netmiko's default prompt
    # detection then loses sync. Match the prompt explicitly and wait longer.
    PROMPT = r"[>#] ?$"
    # JSON output is complete only at a prompt that follows the closing brace (Junos may
    # print "# commit confirmed ..." banner lines in between). Without this, a stale prompt
    # left in the buffer ends the read before the output arrives.
    # NX-OS answers "% Invalid command" (no JSON) while a feature such as bgp is still off.
    # NX-OS prints nothing at all for an empty table (`show nve peers | json` with no peers),
    # so the echoed command followed directly by the prompt also ends the read.
    JSON_PROMPT = r"(?:[}\]]|%[^\n]*|\| *json\s*)\s*(?:#[^\n]*\s*)*\S+[>#] ?$"
    READ_TIMEOUT = 180
    # A starved lab vJunos took >15 min for a MAC-VRF commit; real switches take seconds.
    COMMIT_TIMEOUT = int(os.environ.get("FABRIC_COMMIT_TIMEOUT", "900"))

    def __init__(self, run_id: str):
        self.user = os.environ.get("FABRIC_USER", "admin")
        self.passwords = {p: os.environ.get(f"FABRIC_PASSWORD_{p.upper()}") or os.environ["FABRIC_PASSWORD"]
                          for p in self.DEVICE_TYPES}
        # NX-OS checkpoint names allow only letters, digits, '-' and '_' (a '.' was rejected
        # live with "String failed to match token pattern").
        self.checkpoint = "fab-" + re.sub(r"[^A-Za-z0-9]", "", run_id)[:15]
        self._conns = {}

    # A just-booted vJunos reports healthy before sshd answers, and a vJunos still finishing a
    # long commit refuses new logins for many minutes. Lab runs raise FABRIC_CONNECT_ATTEMPTS.
    CONNECT_ATTEMPTS = int(os.environ.get("FABRIC_CONNECT_ATTEMPTS", "4"))
    CONNECT_BACKOFF = 30  # seconds

    def _conn(self, dev: Device):
        if dev.name not in self._conns:
            for attempt in range(1, self.CONNECT_ATTEMPTS + 1):
                try:
                    self._conns[dev.name] = self._connect(dev)
                    break
                except Exception:  # banner/auth/timeout errors while the device is still starting
                    if attempt == self.CONNECT_ATTEMPTS:
                        raise
                    time.sleep(self.CONNECT_BACKOFF)
        return self._conns[dev.name]

    def _drop(self, dev: Device) -> None:
        conn = self._conns.pop(dev.name, None)
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass

    def _connect(self, dev: Device):
        from netmiko import ConnectHandler
        return ConnectHandler(
                device_type=self.DEVICE_TYPES[dev.platform], host=str(dev.mgmt_ip),
                username=self.user, password=self.passwords[dev.platform], conn_timeout=30,
                # Overloaded lab VMs can take >10 s to echo a command; Netmiko's echo check
                # has a fixed 10 s limit. Explicit prompt matching (PROMPT) keeps us in sync.
                global_cmd_verify=False,
                session_log=self._session_log(dev))

    @staticmethod
    def _session_log(dev: Device) -> str | None:
        """Set FABRIC_SESSION_LOGS=<dir> to record every SSH session (for debugging)."""
        log_dir = os.environ.get("FABRIC_SESSION_LOGS")
        if not log_dir:
            return None
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f"{dev.name}.log")

    def _junos_configure(self, conn) -> None:
        """Enter exclusive config mode. Netmiko's config_mode() has a short fixed wait that a
        loaded vJunos exceeded right after a commit (crashed the confirm step live)."""
        conn.clear_buffer()
        conn.write_channel("configure exclusive\n")
        out = conn.read_until_pattern(r"\[edit\][\s\S]*#\s*$", read_timeout=self.READ_TIMEOUT)
        if re.search(r"error|locked|cannot", out, re.I):
            raise RuntimeError(f"Junos configure exclusive failed: {out.strip()[-500:]}")

    def _junos_exit(self, conn) -> None:
        conn.clear_buffer()
        conn.write_channel("exit configuration-mode\n")
        conn.read_until_pattern(r">\s*$", read_timeout=self.READ_TIMEOUT)

    def _junos_commit(self, conn, command: str) -> str:
        """Send a commit and wait for its outcome. Netmiko's commit() gave up while a large
        commit (MAC-VRF restructure) was still running on a loaded vJunos.

        For `commit confirmed`, the "will be automatically rolled back" notice means the new
        config is active: a starved vJunos applied it within seconds but then took >30 min to
        print "commit complete". The health gate decides whether the change is good, and the
        switch's own timer rolls it back if it is never confirmed."""
        conn.clear_buffer()
        conn.write_channel(command + "\n")
        accepted = r"commit complete[\s\S]*#\s*$|\berror\b[\s\S]*#\s*$|failed[\s\S]*#\s*$"
        if command.startswith("commit confirmed"):
            accepted += r"|will be automatically rolled back in \d+ minutes unless confirmed"
        out = conn.read_until_pattern(accepted, read_timeout=self.COMMIT_TIMEOUT)
        if re.search(r"\berror\b|failed", out, re.I) and "commit complete" not in out:
            raise RuntimeError(f"Junos {command!r} failed:\n{out.strip()[-1500:]}")
        if "commit complete" not in out and "automatically rolled back" not in out:
            raise RuntimeError(f"Junos {command!r} gave no result:\n{out.strip()[-1500:]}")
        return out

    def _show(self, conn, cmd: str) -> str:
        conn.clear_buffer()
        expect = self.JSON_PROMPT if "json" in cmd else self.PROMPT
        return conn.send_command(cmd, expect_string=expect, read_timeout=self.READ_TIMEOUT)

    def snapshot(self, dev: Device) -> dict:
        conn, cmds = self._conn(dev), self.COMMANDS[dev.platform]
        established = 0
        for cmd in cmds["bgp"]:
            data = _json_or_empty(self._show(conn, cmd))
            established += sum(1 for v in _find(data, {"state", "peer-state"}) if v == "Established")
        if dev.role == "spine":
            return {"bgp_established": established, "vtep_peers": 0}
        vtep = _json_or_empty(self._show(conn, cmds["vtep"]))
        if dev.platform == "nxos":
            peers = {ip for ip, st in zip(_find(vtep, {"peer-ip"}), _find(vtep, {"peer-state"})) if st == "Up"}
        else:
            peers = _junos_remote_vteps(vtep)
        return {"bgp_established": established, "vtep_peers": len(peers)}

    def apply(self, dev: Device, config: str, confirm_minutes: int) -> None:
        conn, lines = self._conn(dev), [l for l in config.splitlines() if l.strip()]
        conn.clear_buffer()
        if dev.platform == "junos":
            # Paste the whole config into `load set terminal` in one write: Junos validates
            # every line and reports all errors at once, independent of how fast the CLI
            # echoes input (line-by-line pushes and Netmiko's SCP checks desynced on a slow device).
            self._junos_configure(conn)
            conn.clear_buffer()
            conn.write_channel("load set terminal\n")
            conn.read_until_pattern(r"\[Type \^D", read_timeout=self.READ_TIMEOUT)
            conn.write_channel("\n".join(lines) + "\n")
            conn.write_channel("\x04")
            out = conn.read_until_pattern(r"load complete[^\n]*\n[\s\S]*#\s*$", read_timeout=self.READ_TIMEOUT)
            # Junos reports problems inline ("terminal:12:(30) syntax error ...") and in the
            # summary ("load complete (2 errors)"); our config text never contains these words.
            if re.search(r"\berrors?\b|syntax|not within range|unknown command|invalid", out, re.I):
                conn.send_command("rollback 0", expect_string=r"#\s*$", read_timeout=self.READ_TIMEOUT)
                self._junos_exit(conn)  # exclusive mode discards the uncommitted load
                raise RuntimeError(f"Junos rejected config:\n{out.strip()[-1500:]}")
            # commit confirmed: the switch rolls itself back unless we confirm in time,
            # even if this automation host dies mid-change.
            out = self._junos_commit(conn, f"commit confirmed {confirm_minutes}")
            if "commit complete" in out:
                self._junos_exit(conn)
            else:
                # Commit still finishing: use a fresh session for health checks and confirm.
                self._drop(dev)
        else:
            # Same one-write approach as Junos; the checkpoint is the rollback point.
            conn.clear_buffer()
            out = conn.send_command(f"checkpoint {self.checkpoint}", expect_string=self.PROMPT,
                                    read_timeout=self.READ_TIMEOUT)
            if re.search(r"%|error|fail", out, re.I):
                # Fail closed: without a rollback point the change must not be applied.
                raise RuntimeError(f"NX-OS checkpoint failed, change not applied: {out.strip()[-300:]}")
            conn.config_mode()
            conn.clear_buffer()
            conn.write_channel("\n".join(lines) + "\nend\n")
            out = conn.read_until_pattern(r"\(config[^)]*\)#\s*end\s*[\r\n]+\S+#\s*$",
                                          read_timeout=self.READ_TIMEOUT)
            if re.search(r"% Invalid|% Incomplete|% Ambiguous|\bERROR:|\bError:", out):
                self.rollback(dev)
                raise RuntimeError(f"NX-OS rejected config:\n{out.strip()[-1500:]}")

    def confirm(self, dev: Device) -> None:
        conn = self._conn(dev)
        conn.clear_buffer()
        if dev.platform == "junos":
            self._junos_configure(conn)
            self._junos_commit(conn, "commit")
            self._junos_exit(conn)
        else:
            conn.send_command("copy running-config startup-config", expect_string=self.PROMPT,
                              read_timeout=self.READ_TIMEOUT)

    def rollback(self, dev: Device) -> None:
        conn = self._conn(dev)
        conn.clear_buffer()
        if dev.platform == "junos":
            self._junos_configure(conn)
            conn.send_command("rollback 1", expect_string=r"#", read_timeout=self.READ_TIMEOUT)
            self._junos_commit(conn, "commit")
            self._junos_exit(conn)
        else:
            conn.send_command(f"rollback running-config checkpoint {self.checkpoint}", expect_string=self.PROMPT,
                              read_timeout=self.READ_TIMEOUT)


def _json(text: str):
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"no JSON in device output: {text[:200]!r}")
    # raw_decode stops at the end of the first JSON document: Junos appends text after it,
    # e.g. "# commit confirmed will be rolled back in 15 minutes" while a rollback timer is armed.
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _junos_remote_vteps(data) -> set[str]:
    """Remote VTEP IPs from `show ethernet-switching vxlan-tunnel-end-point remote | display json`.
    Matches any key naming a remote VTEP address (e.g. remote-vtep-address, rvtep-ip) instead of
    one exact name, since the schema varies by Junos release."""
    found = set()

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = k.lower()
                if ("remote" in key or "rvtep" in key) and ("addr" in key or "ip" in key):
                    for val in _find({k: v}, {k}):
                        if isinstance(val, str) and _IPV4.match(val):
                            found.add(val)
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return found


def _json_or_empty(text: str):
    """No JSON at all (NX-OS `% Invalid command` while a feature is off, or an empty table)
    means nothing to count.
    Safe for the health gate: post-change checks require the expected minimum, so an empty
    answer after a change still fails."""
    if "{" not in text and "[" not in text:
        return {}
    return _json(text)


def _find(obj, keys: set[str]):
    """Yield values for any of `keys`, anywhere in nested JSON. Unwraps Junos {"data": x} leaves."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                if isinstance(v, list) and v and isinstance(v[0], dict) and "data" in v[0]:
                    v = v[0]["data"]
                if not isinstance(v, (dict, list)):
                    yield v
                    continue
            yield from _find(v, keys)
    elif isinstance(obj, list):
        for item in obj:
            yield from _find(item, keys)
