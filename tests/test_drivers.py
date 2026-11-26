"""Parsing tests against output captured from real devices (tests/fixtures/)."""
from conftest import ROOT
from fabric.drivers import _find, _json

FIXTURES = ROOT / "tests" / "fixtures"


def test_junos_bgp_json_with_commit_confirmed_banner():
    # Captured from vJunos-switch 26.2R1.7 (spine2) right after `commit confirmed 15`:
    # 2 peers configured, neither up yet, followed by the rollback-timer banner.
    data = _json((FIXTURES / "junos_bgp_summary_commit_pending.txt").read_text())
    states = list(_find(data, {"peer-state"}))
    assert states == ["Connect", "Active"]
    assert sum(s == "Established" for s in states) == 0


def test_json_ignores_leading_echo_and_trailing_prompt():
    text = 'show bgp summary | display json\n{"a": [{"peer-state": [{"data": "Established"}]}]}\n\nadmin@leaf3> '
    assert list(_find(_json(text), {"peer-state"})) == ["Established"]


def test_json_prompt_waits_for_output_not_stale_prompt():
    import re
    from fabric.drivers import NetmikoDriver
    pat = NetmikoDriver.JSON_PROMPT
    assert not re.search(pat, "\nadmin@spine2> ")                       # stale prompt alone
    assert re.search(pat, '{\n "a": 1\n}\n\nadmin@spine2> ')               # output + prompt
    real = (FIXTURES / "junos_bgp_summary_commit_pending.txt").read_text()
    assert re.search(pat, real)                                           # banner between } and prompt
    assert re.search(pat, '{"TABLE_vrf": []}\nleaf1# ')                  # NX-OS prompt


def test_nxos_feature_not_enabled_counts_as_nothing():
    import re
    from fabric.drivers import NetmikoDriver, _json_or_empty
    out = "show bgp ipv4 unicast summary | json\n                ^\n% Invalid command at '^' marker.\n\nleaf1# "
    assert re.search(NetmikoDriver.JSON_PROMPT, out)
    assert _json_or_empty(out) == {}
    assert not re.search(NetmikoDriver.JSON_PROMPT, "\nleaf1# ")   # stale prompt still rejected


def test_nxos_bgp_summary_counts_established():
    text = ('{"TABLE_vrf": {"ROW_vrf": {"TABLE_af": {"ROW_af": {"TABLE_saf": {"ROW_saf": '
            '{"TABLE_neighbor": {"ROW_neighbor": [{"neighborid": "10.0.0.1", "state": "Established"}]}}}}}}}}\nleaf1# ')
    assert [v for v in _find(_json(text), {"state", "peer-state"})] == ["Established"]


def test_junos_remote_vteps_by_key_pattern():
    from fabric.drivers import _junos_remote_vteps
    data = {"vxlan-source-vtep-information": [{
        "source-vtep-address": [{"data": "10.255.0.3"}],
        "vxlan-remote-vtep-information": [
            {"remote-vtep-address": [{"data": "10.255.0.1"}]},
            {"remote-vtep-address": [{"data": "10.255.0.2"}]},
        ]}]}
    assert _junos_remote_vteps(data) == {"10.255.0.1", "10.255.0.2"}   # source VTEP excluded


def test_nxos_empty_json_table_ends_read_and_counts_zero():
    import re
    from fabric.drivers import NetmikoDriver, _json_or_empty
    out = "show nve peers | json\n\nleaf1# "                  # captured live: no peers -> no output
    assert re.search(NetmikoDriver.JSON_PROMPT, out)
    assert _json_or_empty(out) == {}


def test_nxos_checkpoint_name_is_valid_token():
    import os, re
    from fabric.drivers import NetmikoDriver
    os.environ.setdefault("FABRIC_PASSWORD", "x")
    name = NetmikoDriver("20261011T004758.484737Z").checkpoint
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name), name      # '.' was rejected live


class FakeChannel:
    """Minimal Netmiko stand-in: replays the text a device prints after a command."""

    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def clear_buffer(self):
        pass

    def write_channel(self, text):
        self.sent.append(text)

    def read_until_pattern(self, pattern, read_timeout):
        import re
        assert re.search(pattern, self.reply), "pattern would time out"
        return self.reply


def _driver():
    import os
    from fabric.drivers import NetmikoDriver
    os.environ.setdefault("FABRIC_PASSWORD", "x")
    return NetmikoDriver("20261011T175431.672770Z")


def test_junos_commit_confirmed_accepted_before_commit_complete():
    # Captured live (leaf3): config active, "commit complete" printed >30 min later.
    reply = ("commit confirmed 45 \n[edit protocols]\n  'bgp'\n    warning: requires 'BGP' license\n"
             "commit confirmed will be automatically rolled back in 45 minutes unless confirmed\n")
    out = _driver()._junos_commit(FakeChannel(reply), "commit confirmed 45")
    assert "automatically rolled back" in out


def test_junos_plain_commit_still_requires_commit_complete():
    import pytest
    reply = "commit confirmed will be automatically rolled back in 45 minutes unless confirmed\n"
    with pytest.raises(AssertionError):            # plain commit (confirm step) keeps waiting
        _driver()._junos_commit(FakeChannel(reply), "commit")
    assert _driver()._junos_commit(FakeChannel("commit complete\n\n[edit]\nadmin@leaf3# "), "commit")


def test_junos_commit_error_is_reported():
    import pytest
    reply = "error: configuration check-out failed\n\n[edit]\nadmin@leaf3# "
    with pytest.raises(RuntimeError):
        _driver()._junos_commit(FakeChannel(reply), "commit confirmed 45")


class FakeConn:
    """Records what the NX-OS driver sends; answers every command with a prompt."""
    def __init__(self, replies=None):
        self.sent, self.replies = [], replies or {}

    def clear_buffer(self): pass
    def config_mode(self): pass
    def write_channel(self, text): self.sent.append(text)
    def read_until_pattern(self, *a, **k): return "(config)# end\nleaf1# "

    def send_command(self, cmd, **k):
        self.sent.append(cmd)
        for prefix, reply in self.replies.items():
            if cmd.startswith(prefix):
                return reply
        return "leaf1# "


def nxos_driver(monkeypatch, conn):
    from conftest import ROOT
    from fabric.drivers import NetmikoDriver
    from fabric.render import load_intent
    monkeypatch.setenv("FABRIC_PASSWORD", "x")
    drv = NetmikoDriver("20261126T101500.123456Z")
    monkeypatch.setattr(drv, "_conn", lambda dev: conn)
    leaf1 = next(d for d in load_intent(ROOT / "intent" / "fabric.yml").devices if d.name == "leaf1")
    return drv, leaf1


def test_nxos_checkpoint_deleted_after_confirm(monkeypatch):
    # NX-OS keeps at most 10 user checkpoints; without cleanup every deploy fails after ~10 runs.
    conn = FakeConn()
    drv, leaf1 = nxos_driver(monkeypatch, conn)
    drv.apply(leaf1, "hostname leaf1", 10)
    drv.confirm(leaf1)
    assert conn.sent[0] == f"checkpoint {drv.checkpoint}"
    assert conn.sent[-1] == f"no checkpoint {drv.checkpoint}"
    assert drv.warnings == []


def test_nxos_checkpoint_cleanup_failure_is_only_a_warning(monkeypatch):
    conn = FakeConn({"no checkpoint": "% Checkpoint not found\nleaf1# "})
    drv, leaf1 = nxos_driver(monkeypatch, conn)
    drv.apply(leaf1, "hostname leaf1", 10)
    drv.confirm(leaf1)
    assert len(drv.warnings) == 1 and drv.checkpoint in drv.warnings[0]


def test_nxos_rejected_config_raises_without_rolling_back_twice(monkeypatch):
    import pytest
    conn = FakeConn()
    conn.read_until_pattern = lambda *a, **k: "% Invalid command at '^' marker.\n(config)# end\nleaf1# "
    drv, leaf1 = nxos_driver(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="rejected"):
        drv.apply(leaf1, "bogus", 10)
    assert not any(s.startswith("rollback") for s in conn.sent)   # rollout engine does it once
    drv.rollback(leaf1)
    assert conn.sent[-1] == f"rollback running-config checkpoint {drv.checkpoint}"


def test_nxos_rollback_is_noop_when_checkpoint_was_refused(monkeypatch):
    import pytest
    conn = FakeConn({"checkpoint": "% Max checkpoints reached\nleaf1# "})
    drv, leaf1 = nxos_driver(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="change not applied"):
        drv.apply(leaf1, "hostname leaf1", 10)
    drv.rollback(leaf1)   # nothing was applied, so nothing to undo (and no bogus rollback error)
    assert conn.sent == [f"checkpoint {drv.checkpoint}"]
