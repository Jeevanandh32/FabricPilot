#!/usr/bin/env python3
"""NetBox <-> intent sync (NetBox 4.x, pynetbox).

  push: seed NetBox from intent/fabric.yml (one-time bootstrap, idempotent)
  pull: export NetBox inventory to an intent file the pipeline consumes

After bootstrap NetBox is the source of truth: edit there, then
  python netbox/sync.py pull > intent/from_netbox.yml
  python cli.py plan --intent intent/from_netbox.yml

Fabric-wide policy (pools, spine ASN, anycast MAC, MTU) stays in the `fabric:`
section of the intent file; NetBox holds the inventory (devices, cables,
addresses, ASNs, VLANs, VNIs, VRFs).

Env: NETBOX_URL (default http://127.0.0.1:8000), NETBOX_TOKEN.
Validation: README "Status" records the NetBox 4.7.2 lab push/pull results.
tests/test_netbox_pull.py checks pull_intent() against an in-memory fake of the
pynetbox API (round trip to identical configs, site scoping); it does not talk
to a real NetBox, so push and API-version behavior are covered by the lab only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SITE = "evpn-lab"
CUSTOM_FIELDS = [  # (name, type, object types)
    ("fabric_id", "integer", ["dcim.device"]),
    ("bgp_asn", "integer", ["dcim.device"]),
    ("fabric_link_id", "integer", ["dcim.cable"]),
    ("l3vni", "integer", ["ipam.vrf"]),
    ("l3vni_vlan", "integer", ["ipam.vrf"]),
]
DEVICE_TYPES = {"nxos": ("Cisco", "Nexus 9000v"), "junos": ("Juniper", "vJunos-switch")}


def api():
    token = os.environ.get("NETBOX_TOKEN")
    if not token:
        raise SystemExit("NETBOX_TOKEN is not set")
    import pynetbox
    nb = pynetbox.api(os.environ.get("NETBOX_URL", "http://127.0.0.1:8000"), token=token)
    return nb


def ensure(endpoint, lookup: dict, **extra):
    obj = endpoint.get(**lookup)
    return obj if obj else endpoint.create(**lookup, **extra)


def slug(s: str) -> str:
    return s.lower().replace(" ", "-").replace("/", "-")


def push(intent_path: Path) -> None:
    nb = api()
    data = yaml.safe_load(intent_path.read_text())
    for name, kind, types in CUSTOM_FIELDS:
        ensure(nb.extras.custom_fields, {"name": name}, type=kind, object_types=types)

    site = ensure(nb.dcim.sites, {"slug": SITE}, name=SITE)
    roles = {r: ensure(nb.dcim.device_roles, {"slug": r}, name=r, color="2196f3") for r in ("spine", "leaf")}
    platforms, dtypes = {}, {}
    for plat, (vendor, model) in DEVICE_TYPES.items():
        mfr = ensure(nb.dcim.manufacturers, {"slug": slug(vendor)}, name=vendor)
        platforms[plat] = ensure(nb.dcim.platforms, {"slug": plat}, name=plat, manufacturer=mfr.id)
        dtypes[plat] = ensure(nb.dcim.device_types, {"slug": slug(model)}, model=model, manufacturer=mfr.id)

    devices, ifaces = {}, {}

    def iface(dev_name: str, if_name: str, **extra):
        key = (dev_name, if_name)
        if key not in ifaces:
            ifaces[key] = ensure(nb.dcim.interfaces, {"device_id": devices[dev_name].id, "name": if_name},
                                 device=devices[dev_name].id, type=extra.pop("type", "10gbase-x-sfpp"), **extra)
        return ifaces[key]

    for d in data["devices"]:
        dev = ensure(nb.dcim.devices, {"name": d["name"]}, site=site.id, role=roles[d["role"]].id,
                     device_type=dtypes[d["platform"]].id, platform=platforms[d["platform"]].id,
                     custom_fields={"fabric_id": d["id"], "bgp_asn": d.get("asn")})
        devices[d["name"]] = dev
        mgmt = iface(d["name"], "mgmt0", type="1000base-t", mgmt_only=True)
        ip = ensure(nb.ipam.ip_addresses, {"address": f"{d['mgmt_ip']}/24"},
                    assigned_object_type="dcim.interface", assigned_object_id=mgmt.id)
        dev.primary_ip4 = ip.id
        dev.save()

    vlans = {}
    for v in data["vnis"]:
        vlans[v["vlan"]] = ensure(nb.ipam.vlans, {"vid": v["vlan"], "site_id": site.id}, name=v["name"], site=site.id)
        l2 = ensure(nb.vpn.l2vpns, {"slug": f"vni-{v['vni']}"}, name=f"{v['name']}-{v['vni']}",
                    type="vxlan-evpn", identifier=v["vni"])
        ensure(nb.vpn.l2vpn_terminations, {"l2vpn_id": l2.id, "vlan_id": vlans[v["vlan"]].id},
               l2vpn=l2.id, assigned_object_type="ipam.vlan", assigned_object_id=vlans[v["vlan"]].id)
    vrfs = {r["name"]: ensure(nb.ipam.vrfs, {"name": r["name"]},
                              custom_fields={"l3vni": r["l3vni"], "l3vni_vlan": r["l3vni_vlan"]})
            for r in data["vrfs"]}
    for v in data["vnis"]:
        ensure(nb.ipam.ip_addresses, {"address": v["gateway"], "vrf_id": vrfs[v["vrf"]].id},
               vrf=vrfs[v["vrf"]].id, role="anycast", description=f"anycast gw vlan {v['vlan']}")

    for d in data["devices"]:
        for p in d.get("host_ports", []):
            i = iface(d["name"], p["interface"], mode="access", untagged_vlan=vlans[p["vlan"]].id)
            i.mode, i.untagged_vlan = "access", vlans[p["vlan"]].id
            i.save()

    for link in data["links"]:
        (a, ai), (b, bi) = (e.split(":") for e in (link["a"], link["b"]))
        ia, ib = iface(a, ai), iface(b, bi)
        if ia.cable or ib.cable:
            continue
        nb.dcim.cables.create(
            a_terminations=[{"object_type": "dcim.interface", "object_id": ia.id}],
            b_terminations=[{"object_type": "dcim.interface", "object_id": ib.id}],
            custom_fields={"fabric_link_id": link["id"]})
    print(f"pushed {len(devices)} devices, {len(data['links'])} cables, {len(data['vnis'])} VNIs to NetBox")


def pull_intent(nb, policy: dict) -> dict:
    """Build an intent dict from NetBox. Only objects at the fabric's site are read, so other
    sites' VLANs and L2VPNs in a shared NetBox never leak into this fabric."""
    site = nb.dcim.sites.get(slug=SITE)
    if site is None:
        raise SystemExit(f"NetBox has no site {SITE!r}; run `sync.py push` first")

    devices = []
    for dev in nb.dcim.devices.filter(site_id=site.id):
        if dev.primary_ip4 is None:
            raise SystemExit(f"{dev.name}: no primary IPv4 (management) address in NetBox")
        entry = {"name": dev.name, "id": dev.custom_fields["fabric_id"], "role": dev.role.slug,
                 "platform": dev.platform.slug,
                 "mgmt_ip": str(dev.primary_ip4.address).split("/")[0]}
        if dev.custom_fields.get("bgp_asn"):
            entry["asn"] = dev.custom_fields["bgp_asn"]
        ports = [{"interface": i.name, "vlan": i.untagged_vlan.vid}
                 for i in nb.dcim.interfaces.filter(device_id=dev.id, mode="access") if i.untagged_vlan]
        if ports:
            entry["host_ports"] = ports
        devices.append(entry)

    roles = {d["name"]: d["role"] for d in devices}
    links = []
    for cable in nb.dcim.cables.filter(site_id=site.id):
        a, b = cable.a_terminations[0], cable.b_terminations[0]
        ends = [f"{t.device.name}:{t.name}" for t in (a, b)]
        ends.sort(key=lambda e: roles[e.split(":")[0]] != "spine")  # spine side first
        links.append({"id": cable.custom_fields["fabric_link_id"], "a": ends[0], "b": ends[1]})

    vrf_by_id = {vrf.id: vrf for vrf in nb.ipam.vrfs.all() if vrf.custom_fields.get("l3vni")}
    gateways = {}
    for ip in nb.ipam.ip_addresses.filter(role="anycast"):
        if ip.vrf and ip.vrf.id in vrf_by_id:
            vid = (ip.description or "").rsplit(" ", 1)[-1]
            gateways[vid] = (str(ip.address), vrf_by_id[ip.vrf.id].name)

    vnis, used_vrfs = [], set()
    for term in nb.vpn.l2vpn_terminations.filter(assigned_object_type="ipam.vlan"):
        vlan = nb.ipam.vlans.get(term.assigned_object_id)
        if vlan is None or vlan.site is None or vlan.site.id != site.id:
            continue  # VLAN belongs to another site
        l2 = nb.vpn.l2vpns.get(term.l2vpn.id)
        if str(vlan.vid) not in gateways:
            raise SystemExit(f"vlan {vlan.vid}: no anycast gateway IP (role anycast, description "
                             f"'anycast gw vlan {vlan.vid}') in a VRF with an l3vni")
        gw, vrf = gateways[str(vlan.vid)]
        used_vrfs.add(vrf)
        vnis.append({"name": vlan.name, "vlan": vlan.vid, "vni": l2.identifier, "vrf": vrf, "gateway": gw})

    vrfs = [{"name": v.name, "l3vni": v.custom_fields["l3vni"], "l3vni_vlan": v.custom_fields["l3vni_vlan"]}
            for v in vrf_by_id.values() if v.name in used_vrfs]
    return {"fabric": policy, "devices": sorted(devices, key=lambda d: (d["role"] != "spine", d["name"])),
            "links": sorted(links, key=lambda l: l["id"]), "vrfs": sorted(vrfs, key=lambda v: v["name"]),
            "vnis": sorted(vnis, key=lambda v: v["vlan"])}


def pull(policy_path: Path) -> None:
    policy = yaml.safe_load(policy_path.read_text())["fabric"]
    print("# GENERATED from NetBox by netbox/sync.py pull")
    print(yaml.safe_dump(pull_intent(api(), policy), sort_keys=False))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("push", "pull"):
        sys.exit(__doc__)
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "intent" / "fabric.yml"
    (push if sys.argv[1] == "push" else pull)(path)
