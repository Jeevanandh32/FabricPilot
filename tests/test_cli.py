"""CLI guards around a live run."""
from conftest import ROOT  # noqa: F401  (puts the repo on sys.path)
import cli


def test_confirm_timer_must_outlast_health_gate():
    assert cli.timer_problem(5, 300)            # old default: Junos would undo a good change
    assert not cli.timer_problem(10, 300)       # new default
    assert not cli.timer_problem(15, 300) and not cli.timer_problem(25, 900)   # README lab values
    assert "at least 17 minutes" in cli.timer_problem(16, 900)


def test_live_deploy_refuses_unsafe_timer_before_touching_anything(capsys):
    assert cli.main(["deploy", "--live", "--yes", "--confirm-minutes", "5", "--settle-seconds", "300"]) == 1
    assert "too short" in capsys.readouterr().out


def test_email_failure_does_not_crash_the_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(cli, "BUILD", tmp_path / "build")
    monkeypatch.setattr(cli, "state_dir", lambda live: tmp_path / "state")

    def boom(*a, **k):
        raise OSError("smtp down")
    monkeypatch.setattr(cli, "send_report", boom)
    assert cli.main(["deploy", "--simulate"]) == 0
    assert "email failed (smtp down)" in capsys.readouterr().out


def _fake_netbox(monkeypatch, nb):
    from netbox import sync
    monkeypatch.setattr(sync, "api", lambda: nb)


def test_netbox_source_gives_same_plan_as_file(monkeypatch, tmp_path, capsys):
    from test_netbox_pull import INTENT, fake_netbox
    monkeypatch.setattr(cli, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(cli, "BUILD", tmp_path / "build")
    monkeypatch.setattr(cli, "state_dir", lambda live: tmp_path / "state")
    _fake_netbox(monkeypatch, fake_netbox(INTENT))
    assert cli.main(["deploy", "--simulate"]) == 0                  # deployed from the YAML file
    capsys.readouterr()
    assert cli.main(["plan", "--netbox"]) == 0                      # NetBox describes the same fabric
    assert "0 of 6 devices change" in capsys.readouterr().out
    assert (tmp_path / "build" / "intent-from-netbox.yml").exists()  # what was pulled is kept


def test_netbox_change_reaches_only_affected_devices(monkeypatch, tmp_path, capsys):
    import copy
    from test_netbox_pull import INTENT, fake_netbox
    monkeypatch.setattr(cli, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(cli, "BUILD", tmp_path / "build")
    monkeypatch.setattr(cli, "state_dir", lambda live: tmp_path / "state")
    assert cli.main(["deploy", "--simulate"]) == 0
    edited = copy.deepcopy(INTENT)   # someone adds VLAN 30 in NetBox
    edited["vnis"].append({"name": "DB", "vlan": 30, "vni": 10030, "vrf": "TENANT1", "gateway": "10.30.30.1/24"})
    _fake_netbox(monkeypatch, fake_netbox(edited))
    capsys.readouterr()
    assert cli.main(["deploy", "--simulate", "--netbox"]) == 0
    out = capsys.readouterr().out
    assert "4 of 6 devices change" in out and "SUCCESS" in out


def test_netbox_unreachable_blocks_the_run(monkeypatch, tmp_path, capsys):
    from netbox import sync
    monkeypatch.setattr(cli, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(cli, "BUILD", tmp_path / "build")

    def down():
        raise ConnectionError("NetBox at http://127.0.0.1:8000 refused the connection")
    monkeypatch.setattr(sync, "api", down)
    assert cli.main(["deploy", "--simulate", "--netbox"]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "refused" in out


def test_netbox_without_token_says_so(monkeypatch, capsys):
    monkeypatch.delenv("NETBOX_TOKEN", raising=False)
    assert cli.main(["validate", "--netbox"]) == 1
    assert "NETBOX_TOKEN is not set" in capsys.readouterr().out
