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
