#!/usr/bin/env python3
"""Data-plane check: every lab host pings its gateway and every other host.

Hosts in the same VLAN prove L2 over VXLAN (EVPN type-2); hosts in different VLANs of
the same VRF prove routing over the L3VNI (symmetric IRB). Expected results come from
the intent file, so a pass means the fabric does what the source of truth says.

  sudo python lab/ping_matrix.py [intent.yml]      # on the lab VM
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fabric.render import ROOT, load_intent  # noqa: E402
from lab.gen_topology import lab_hosts  # noqa: E402


def ping(lab: str, src: str, dst_ip: str) -> bool:
    # A few tries: the first packets of a flow can be spent on ARP / EVPN MAC learning.
    for _ in range(3):
        r = subprocess.run(["docker", "exec", f"clab-{lab}-{src}", "ping", "-c", "2", "-W", "2", dst_ip],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True
    return False


def main(intent=ROOT / "intent" / "fabric.yml") -> int:
    fab = load_intent(intent)
    hosts = lab_hosts(fab)
    rows, failed = [], 0
    for src in hosts:
        targets = [("gateway", src["gateway"], "local gateway")]
        for dst in hosts:
            if dst is src:
                continue
            kind = "L2 VXLAN" if dst["vlan"] == src["vlan"] else "L3 VNI (IRB)"
            targets.append((dst["name"], dst["ip"], kind))
        for name, ip, kind in targets:
            ok = ping(fab.fabric.name, src["name"], ip)
            failed += not ok
            rows.append(f"{'PASS' if ok else 'FAIL'}  {src['name']} ({src['leaf']}/{src['platform']}, vlan {src['vlan']})"
                        f" -> {name} {ip}  [{kind}]")
    print("\n".join(rows))
    print(f"\n{len(rows) - failed}/{len(rows)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
