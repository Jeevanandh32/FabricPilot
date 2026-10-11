"""Run report (JSON for machines, HTML for humans) and email notification."""
from __future__ import annotations

import html
import json
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from fabric.plan import DevicePlan
from fabric.rollout import RolloutResult

ICONS = {"SUCCESS": "✅", "NO_CHANGES": "✅", "HALTED": "❌", "VERIFY_FAILED": "⚠️", "BLOCKED": "⛔"}


def build_report(run_id: str, mode: str, status: str, plan: dict[str, DevicePlan] | None = None,
                 result: RolloutResult | None = None, errors: list[str] | None = None,
                 halt_reason: str = "") -> dict:
    return {
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "halt_reason": halt_reason,
        "validation_errors": errors or [],
        "plan": {d: {"changed": p.changed, "new_device": p.new_device, "added": p.added, "removed": p.removed}
                 for d, p in (plan or {}).items()},
        "steps": [s.__dict__ for s in (result.steps if result else [])],
        "verify_problems": result.verify_problems if result else {},
    }


def write_report(report: dict, reports_dir: Path) -> Path:
    out = reports_dir / report["run_id"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (out / "report.html").write_text(render_html(report))
    return out


def render_html(r: dict) -> str:
    e = html.escape
    colors = {"confirmed": "#1a7f37", "rolled_back": "#cf222e", "not_started": "#6e7781"}
    rows = "".join(
        f"<tr><td>{e(s['wave'])}</td><td>{e(s['device'])}</td><td>{e(s['platform'])}</td>"
        f"<td style='color:{colors.get(s['status'], '#000')};font-weight:600'>{e(s['status'])}</td>"
        f"<td>{e(json.dumps(s['post']))}</td><td>{e('; '.join(s['problems']))}</td></tr>"
        for s in r["steps"])
    plan_rows = "".join(
        f"<tr><td>{e(d)}</td><td>{'new' if p['new_device'] else ('changed' if p['changed'] else 'no change')}</td>"
        f"<td>+{p['added']} / -{p['removed']}</td></tr>"
        for d, p in r["plan"].items())
    errors = "".join(f"<li>{e(x)}</li>" for x in r["validation_errors"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Fabric change {e(r['run_id'])}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;margin:24px;color:#1f2328;background:#fff}}
table{{border-collapse:collapse;margin:8px 0 20px}}td,th{{border:1px solid #d0d7de;padding:6px 10px;text-align:left;font-size:14px}}
th{{background:#f6f8fa}}</style></head><body>
<h2>{ICONS.get(r['status'], '')} Fabric change {e(r['run_id'])}: {e(r['status'])}</h2>
<p>Mode: <b>{e(r['mode'])}</b>{' | Halt reason: <b>' + e(r['halt_reason']) + '</b>' if r['halt_reason'] else ''}</p>
{'<h3>Fabric-wide verification</h3><ul>' + ''.join(f"<li>{e(d)}: {e('; '.join(p))}</li>" for d, p in r['verify_problems'].items()) + '</ul>' if r['verify_problems'] else ''}
{'<h3>Validation errors (nothing was deployed)</h3><ul>' + errors + '</ul>' if errors else ''}
{'<h3>Change plan</h3><table><tr><th>Device</th><th>Change</th><th>Lines</th></tr>' + plan_rows + '</table>' if plan_rows else ''}
{'<h3>Rollout</h3><table><tr><th>Wave</th><th>Device</th><th>Platform</th><th>Result</th><th>Post-check</th><th>Problems</th></tr>' + rows + '</table>' if rows else ''}
</body></html>"""


def send_report(report: dict, report_dir: Path) -> str:
    """Email the report. Without SMTP settings, save the message as .eml instead."""
    to = os.environ.get("REPORT_EMAIL_TO")
    msg = EmailMessage()
    msg["Subject"] = f"{ICONS.get(report['status'], '')} Fabric change {report['run_id']}: {report['status']}"
    msg["From"] = os.environ.get("REPORT_EMAIL_FROM", os.environ.get("SMTP_USER", "fabric-automation@localhost"))
    msg["To"] = to or "unset@localhost"
    summary = f"Status: {report['status']}\nMode: {report['mode']}\n"
    if report["halt_reason"]:
        summary += f"Halt reason: {report['halt_reason']}\n"
    msg.set_content(summary + "\nOpen the HTML part or attached report.json for details.")
    msg.add_alternative((report_dir / "report.html").read_text(), subtype="html")
    msg.add_attachment((report_dir / "report.json").read_bytes(), maintype="application",
                       subtype="json", filename="report.json")

    host = os.environ.get("SMTP_HOST")
    if not (host and to):
        path = report_dir / "report.eml"
        path.write_bytes(bytes(msg))
        return f"SMTP not configured; email saved to {path}"
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as smtp:
        smtp.starttls()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    return f"report emailed to {to}"
