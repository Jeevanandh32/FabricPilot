#!/usr/bin/env python3
"""Fabric change pipeline: validate -> render -> plan -> staged rollout -> verify -> report -> email.

  python cli.py validate [--intent FILE] [--netbox]
  python cli.py render   [--intent FILE] [--netbox]
  python cli.py plan     [--intent FILE] [--netbox] [--live]
  python cli.py deploy   [--intent FILE] [--netbox] (--simulate [--fail-on DEV] | --live [--yes])

--netbox reads devices, cables, VLANs, VNIs and VRFs straight from NetBox (NETBOX_URL,
NETBOX_TOKEN); only the fabric-wide policy (`fabric:` section) comes from --intent.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from fabric.models import Fabric
from fabric.plan import build_plan, orphaned, record_deployed
from fabric.render import ROOT, load_intent, render_all, write_build
from fabric.report import build_report, send_report, write_report
from fabric.rollout import run_rollout

BUILD = ROOT / "build"
REPORTS = ROOT / "reports"


def state_dir(live: bool) -> Path:
    # Simulated runs keep their own state so they never mask real pending changes.
    return ROOT / "state" / ("deployed" if live else "simulated")


def load_from_netbox(policy_path: str) -> Fabric:
    """NetBox as the source of truth: inventory from NetBox, fabric-wide policy from the
    intent file. The pulled intent is saved to build/ so every run shows what it deployed."""
    import yaml
    from netbox import sync
    policy = yaml.safe_load(Path(policy_path).read_text())["fabric"]
    data = sync.pull_intent(sync.api(), policy)
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "intent-from-netbox.yml").write_text(
        "# Pulled from NetBox by cli.py --netbox\n" + yaml.safe_dump(data, sort_keys=False))
    return Fabric.model_validate(data)


def load_or_errors(args):
    try:
        if getattr(args, "netbox", False):
            try:
                return load_from_netbox(args.intent), []
            except ValidationError:
                raise
            except (Exception, SystemExit) as exc:  # no token, unreachable, missing site or field
                return None, [f"netbox: {exc!r}" if isinstance(exc, KeyError) else f"netbox: {exc}"]
        return load_intent(args.intent), []
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            msg = str(err["msg"]).removeprefix("Value error, ")
            loc = ".".join(str(x) for x in err["loc"])
            errors += [f"{loc}: {line.strip(' -')}" if loc else line.strip(" -")
                       for line in msg.splitlines() if line.strip(" -") and line != "intent rejected:"]
        return None, errors


def cmd_validate(args) -> int:
    fab, errors = load_or_errors(args)
    if errors:
        print("⛔ intent rejected:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"✅ intent valid: {len(fab.devices)} devices, {len(fab.links)} links, {len(fab.vnis)} VNIs")
    return 0


def cmd_render(args) -> int:
    fab, errors = load_or_errors(args)
    if errors:
        return cmd_validate(args)
    configs = render_all(fab)
    write_build(configs, BUILD)
    for name, text in configs.items():
        print(f"  {BUILD.name}/{name}.cfg  ({len(text.splitlines())} lines)")
    return 0


def warn_orphans(sdir: Path, configs: dict[str, str]) -> None:
    for d in orphaned(sdir, configs):
        print(f"⚠️  {d} is recorded as deployed but is not in the intent. FabricPilot does not "
              f"decommission devices: clean it up by hand, then delete {sdir.name}/{d}.cfg")


def print_plan(plan, show_diff: bool) -> list[str]:
    changed = [d for d, p in plan.items() if p.changed]
    for d, p in plan.items():
        tag = "NEW" if p.new_device else ("CHANGED" if p.changed else "no change")
        print(f"  {d:8} {tag:9} +{p.added} -{p.removed}")
        if show_diff and p.changed and not p.new_device:
            print("\n".join("      " + l for l in p.diff))
    print(f"{len(changed)} of {len(plan)} devices change")
    return changed


def cmd_plan(args) -> int:
    fab, errors = load_or_errors(args)
    if errors:
        return cmd_validate(args)
    configs = render_all(fab)
    print_plan(build_plan(configs, state_dir(args.live)), show_diff=True)
    warn_orphans(state_dir(args.live), configs)
    return 0


# Margin between the end of the health gate and the Junos switch-side rollback timer, for the
# confirm commit itself. The README's lab values (300/15, 900/25) all pass this check.
CONFIRM_MARGIN_SECONDS = 120


def timer_problem(confirm_minutes: int, settle_seconds: int) -> str:
    """Junos `commit confirmed N` rolls the switch back after N minutes unless confirmed. If
    the health gate can run longer than that, the switch undoes a good change on its own."""
    if confirm_minutes * 60 < settle_seconds + CONFIRM_MARGIN_SECONDS:
        return (f"--confirm-minutes {confirm_minutes} is too short for --settle-seconds {settle_seconds}: "
                f"Junos would roll back before the health gate can confirm. Use at least "
                f"{-(-(settle_seconds + CONFIRM_MARGIN_SECONDS) // 60)} minutes.")
    return ""


def cmd_deploy(args) -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    mode = "live" if args.live else "simulate"
    source = "netbox" if args.netbox else args.intent

    def finish(report: dict) -> int:
        report["source"] = source
        out = write_report(report, REPORTS)
        print(f"report: {out / 'report.html'}")
        try:
            print(send_report(report, out))
        except Exception as exc:
            # The rollout is over and the report is on disk; a mail failure must not turn
            # into a crash that hides the run's real status.
            print(f"⚠️  email failed ({exc}); report kept at {out}")
        return 0 if report["status"] in ("SUCCESS", "NO_CHANGES") else 1

    if args.live:
        problem = timer_problem(args.confirm_minutes, args.settle_seconds)
        if problem:
            print(f"⛔ {problem}")
            return 1

    # 1. validate
    fab, errors = load_or_errors(args)
    if errors:
        print("⛔ BLOCKED: intent failed validation, nothing deployed")
        for e in errors:
            print(f"  - {e}")
        return finish(build_report(run_id, mode, "BLOCKED", errors=errors))

    # 2-3. render + plan
    configs = render_all(fab)
    write_build(configs, BUILD)
    sdir = state_dir(args.live)
    plan = build_plan(configs, sdir)
    print("change plan:")
    changed = print_plan(plan, show_diff=False)
    warn_orphans(sdir, configs)
    if args.limit:
        limit = set(args.limit.split(","))
        unknown = limit - set(configs)
        if unknown:
            print(f"unknown device(s) in --limit: {', '.join(sorted(unknown))}")
            return 1
        skipped = [d for d in changed if d not in limit]
        changed = [d for d in changed if d in limit]
        if skipped:
            print(f"--limit: leaving {', '.join(skipped)} for a later run")
    if not changed:
        return finish(build_report(run_id, mode, "NO_CHANGES", plan=plan))
    deployed = {p.name.removesuffix(".cfg") for p in sdir.glob("*.cfg")}

    # 4-5. staged rollout with health gates
    if args.live:
        if not args.yes and input("deploy to LIVE devices? type 'yes': ").strip() != "yes":
            print("aborted")
            return 1
        from fabric.drivers import NetmikoDriver
        driver = NetmikoDriver(run_id)
    else:
        from fabric.drivers import SimDriver
        driver = SimDriver(fab, deployed, fail_on=set(args.fail_on), unreachable=set(args.unreachable))

    result = run_rollout(fab, configs, changed, deployed, driver,
                         batch_size=args.batch_size, confirm_minutes=args.confirm_minutes,
                         settle_seconds=args.settle_seconds if args.live else 0,
                         on_confirmed=lambda d: record_deployed(sdir, d, configs[d]))
    print(f"{'❌' if result.halted else '✅'} {result.status}" + (f": {result.halt_reason}" if result.halted else ""))
    for w in getattr(driver, "warnings", []):
        print(f"⚠️  {w}")

    # 6-7. report + email
    return finish(build_report(run_id, mode, result.status, plan=plan, result=result,
                               halt_reason=result.halt_reason))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "render", "plan", "deploy"):
        p = sub.add_parser(name)
        p.add_argument("--intent", default=str(ROOT / "intent" / "fabric.yml"),
                       help="intent file (with --netbox: only its fabric-wide policy is used)")
        p.add_argument("--netbox", action="store_true",
                       help="read the inventory from NetBox (NETBOX_URL, NETBOX_TOKEN) instead of the file")
        if name == "plan":
            p.add_argument("--live", action="store_true", help="compare against live deployed state")
        if name == "deploy":
            m = p.add_mutually_exclusive_group(required=True)
            m.add_argument("--simulate", action="store_true")
            m.add_argument("--live", action="store_true")
            p.add_argument("--yes", action="store_true", help="skip live confirmation prompt")
            p.add_argument("--fail-on", nargs="*", default=[], help="simulate: devices that break after change")
            p.add_argument("--unreachable", nargs="*", default=[], help="simulate: devices that drop SSH")
            p.add_argument("--batch-size", type=int, default=2)
            p.add_argument("--limit", help="comma-separated devices to deploy now (others wait)")
            p.add_argument("--confirm-minutes", type=int, default=10,
                           help="Junos auto-rollback timer if the change is not confirmed "
                                "(must outlast --settle-seconds; checked before a live run)")
            p.add_argument("--settle-seconds", type=int, default=300,
                           help="live: time a device may take to converge before failing its gate")
    args = ap.parse_args(argv)
    return {"validate": cmd_validate, "render": cmd_render, "plan": cmd_plan, "deploy": cmd_deploy}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
