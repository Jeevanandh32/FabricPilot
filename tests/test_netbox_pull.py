"""NetBox pull against an in-memory fake of the pynetbox API.

The fake is built from intent/fabric.yml the same way `sync.py push` lays objects out in
NetBox, so a pull must give back an intent that renders identical configs. It does not talk
to a real NetBox: API behavior is covered by the lab runs recorded in the README.
"""
import sys
from types import SimpleNamespace as NS

import pytest
import yaml

from conftest import ROOT
from fabric.models import Fabric
from fabric.render import render_all

sys.path.insert(0, str(ROOT / "netbox"))
import sync  # noqa: E402

INTENT = yaml.safe_load((ROOT / "intent" / "fabric.yml").read_text())


class Endpoint:
    def __init__(self, objs, match=None):
        self.objs, self.match = objs, match or {}

    def all(self):
        return list(self.objs)

    def get(self, key=None, **kw):
        if key is not None:  # pynetbox: get(<id>)
            kw["id"] = key
        found = self.filter(**kw)
        return found[0] if found else None

    def filter(self, **kw):
        return [o for o in self.objs if all(self.match.get(k, lambda o, v, k=k: getattr(o, k) == v)(o, v)
                                            for k, v in kw.items())]


def fake_netbox(data: dict, extra_site: bool = False):
    site, other = NS(id=1, slug=sync.SITE), NS(id=2, slug="other-site")
    devices, interfaces, cables, vlans, l2vpns, terms, ips = [], [], [], [], [], [], []
    by_name = {}
    for i, d in enumerate(data["devices"], start=1):
        dev = NS(id=i, name=d["name"], site=site, role=NS(slug=d["role"]), platform=NS(slug=d["platform"]),
                 primary_ip4=NS(address=f"{d['mgmt_ip']}/24"),
                 custom_fields={"fabric_id": d["id"], "bgp_asn": d.get("asn")})
        devices.append(dev)
        by_name[d["name"]] = dev
    vrfs = [NS(id=i, name=v["name"], custom_fields={"l3vni": v["l3vni"], "l3vni_vlan": v["l3vni_vlan"]})
            for i, v in enumerate(data["vrfs"], start=1)]
    vrf_by_name = {v.name: v for v in vrfs}

    def add_vni(v, at_site, n):
        vlan = NS(id=n, vid=v["vlan"], name=v["name"], site=at_site)
        l2 = NS(id=n, identifier=v["vni"])
        vlans.append(vlan)
        l2vpns.append(l2)
        terms.append(NS(l2vpn=NS(id=l2.id), assigned_object_type="ipam.vlan", assigned_object_id=vlan.id))
        if at_site is site:
            ips.append(NS(address=v["gateway"], role="anycast", vrf=NS(id=vrf_by_name[v["vrf"]].id),
                          description=f"anycast gw vlan {v['vlan']}"))
        return vlan

    vlan_by_vid = {v["vlan"]: add_vni(v, site, n) for n, v in enumerate(data["vnis"], start=1)}
    if extra_site:  # another site's VLAN/L2VPN in the same NetBox must not leak in
        add_vni({"vlan": 999, "name": "OTHER", "vni": 99999, "vrf": data["vrfs"][0]["name"],
                 "gateway": "10.99.99.1/24"}, other, 99)

    for d in data["devices"]:
        for p in d.get("host_ports", []):
            interfaces.append(NS(device_id=by_name[d["name"]].id, name=p["interface"], mode="access",
                                 untagged_vlan=vlan_by_vid[p["vlan"]]))
    for link in data["links"]:
        (a, ai), (b, bi) = (e.split(":") for e in (link["a"], link["b"]))
        # NetBox does not keep cable ends in intent order; put the leaf side first.
        cables.append(NS(site_id=site.id, custom_fields={"fabric_link_id": link["id"]},
                         a_terminations=[NS(device=NS(name=b), name=bi)],
                         b_terminations=[NS(device=NS(name=a), name=ai)]))

    return NS(
        dcim=NS(sites=Endpoint([site, other]),
                devices=Endpoint(devices, {"site_id": lambda o, v: o.site.id == v}),
                interfaces=Endpoint(interfaces),
                cables=Endpoint(cables)),
        ipam=NS(vrfs=Endpoint(vrfs), ip_addresses=Endpoint(ips),
                vlans=Endpoint(vlans, {"id": lambda o, v: o.id == v})),
        vpn=NS(l2vpns=Endpoint(l2vpns, {"id": lambda o, v: o.id == v}), l2vpn_terminations=Endpoint(terms)),
    )


def test_pull_round_trip_renders_identical_configs():
    pulled = sync.pull_intent(fake_netbox(INTENT), INTENT["fabric"])
    assert render_all(Fabric.model_validate(pulled)) == render_all(Fabric.model_validate(INTENT))


def test_pull_puts_spine_side_first_and_keeps_link_ids():
    pulled = sync.pull_intent(fake_netbox(INTENT), INTENT["fabric"])
    assert [(l["id"], l["a"], l["b"]) for l in pulled["links"]] == \
        [(l["id"], l["a"], l["b"]) for l in sorted(INTENT["links"], key=lambda l: l["id"])]


def test_pull_ignores_other_sites():
    pulled = sync.pull_intent(fake_netbox(INTENT, extra_site=True), INTENT["fabric"])
    assert 999 not in [v["vlan"] for v in pulled["vnis"]]
    Fabric.model_validate(pulled)


def test_pull_without_site_explains_what_to_do():
    nb = fake_netbox(INTENT)
    nb.dcim.sites = Endpoint([])
    with pytest.raises(SystemExit, match="push"):
        sync.pull_intent(nb, INTENT["fabric"])


def test_pull_names_vlan_with_missing_gateway():
    nb = fake_netbox(INTENT)
    nb.ipam.ip_addresses.objs.pop()
    with pytest.raises(SystemExit, match=r"vlan \d+: no anycast gateway"):
        sync.pull_intent(nb, INTENT["fabric"])
