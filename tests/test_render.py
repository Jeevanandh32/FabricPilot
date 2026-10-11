from conftest import ROOT
from fabric.plan import build_plan, record_deployed
from fabric.render import load_intent, render_all

FAB = load_intent(ROOT / "intent" / "fabric.yml")
CFG = render_all(FAB)
# The Junos spine template is exercised through the Juniper-only lab (spine2 is NX-OS in fabric.yml).
JCFG = render_all(load_intent(ROOT / "intent" / "junos_mini.yml"))


def test_every_device_rendered():
    assert set(CFG) == {d.name for d in FAB.devices}


def test_rendering_is_deterministic():
    assert render_all(FAB) == CFG


def test_p2p_addressing_matches_on_both_ends():
    # link 2: spine1:Ethernet1/3 <-> leaf3:ge-0/0/0 gets 10.0.0.4/31
    assert "ip address 10.0.0.4/31" in CFG["spine1"]
    assert "set interfaces ge-0/0/0 unit 0 family inet address 10.0.0.5/31" in CFG["leaf3"]
    assert "neighbor 10.0.0.5\n    remote-as 65003" in CFG["spine1"]
    assert "neighbor 10.0.0.4 peer-as 65000" in CFG["leaf3"]


def test_route_targets_explicit_and_identical_across_vendors():
    # NX-OS and Junos auto-derived RTs differ; both must use <rt_asn>:<vni>.
    assert "route-target import 65000:10010" in CFG["leaf1"]
    assert "MACVRF-10010 vrf-target target:65000:10010" in CFG["leaf3"]
    assert "route-target import 65000:50001 evpn" in CFG["leaf1"]
    assert "route-target export 65000:50001 evpn" in CFG["leaf1"]
    assert "vrf-target target:65000:50001" in CFG["leaf3"]
    assert "vrf-target auto" not in CFG["leaf3"]


def test_same_anycast_gateway_on_both_vendors():
    assert "fabric forwarding anycast-gateway-mac 0000.2222.3333" in CFG["leaf1"]
    assert "set interfaces irb unit 10 mac 00:00:22:22:33:33" in CFG["leaf3"]
    assert "ip address 10.10.10.1/24" in CFG["leaf2"]
    assert "irb unit 10 family inet address 10.10.10.1/24" in CFG["leaf4"]


def test_spines_carry_no_tenant_config():
    for spine in ("spine1", "spine2"):
        assert "10010" not in CFG[spine] and "TENANT1" not in CFG[spine]
    assert "retain route-target all" in CFG["spine1"] and "retain route-target all" in CFG["spine2"]
    assert "keep all" in JCFG["spine2"]


def test_overlay_preserves_next_hop():
    assert "route-map NH-UNCHANGED out" in CFG["spine1"]
    assert "multihop no-nexthop-change" in JCFG["spine2"]


def test_plan_is_idempotent(tmp_path):
    for dev, text in CFG.items():
        record_deployed(tmp_path, dev, text)
    assert not any(p.changed for p in build_plan(CFG, tmp_path).values())


def test_plan_limits_blast_radius(tmp_path):
    for dev, text in CFG.items():
        record_deployed(tmp_path, dev, text)
    new = render_all(load_intent(ROOT / "scenarios" / "add_vni.yml"))
    changed = {d for d, p in build_plan(new, tmp_path).items() if p.changed}
    assert changed == {"leaf1", "leaf2", "leaf3", "leaf4"}


def test_mtu_within_each_platform_limit():
    # Found on vJunos 26.2: "Value 9216 is not within range (256..9192)".
    assert "mtu 9192" in CFG["leaf3"] and "mtu 9216" not in CFG["leaf3"]
    assert "mtu 9192" in JCFG["spine2"]
    assert "mtu 9216" in CFG["leaf1"]


def test_leaf_pair_peers_leaves_directly():
    cfg = render_all(load_intent(ROOT / "intent" / "multivendor_pair.yml"))
    # link 0 -> 10.0.0.0/31: leaf1 .0, leaf3 .1; underlay and overlay peer each other's ASN
    assert "ip address 10.0.0.0/31" in cfg["leaf1"]
    assert "neighbor 10.0.0.1\n    remote-as 65003" in cfg["leaf1"]
    assert "neighbor 10.255.0.3\n    remote-as 65003" in cfg["leaf1"]
    assert "neighbor 10.0.0.0 peer-as 65001" in cfg["leaf3"]
    assert "neighbor 10.255.0.1 peer-as 65001" in cfg["leaf3"]


def test_nxos_features_enabled_before_use():
    lines = CFG["leaf1"].splitlines()
    assert lines.index("feature nv overlay") < lines.index("nv overlay evpn")
    assert lines.index("feature fabric forwarding") < lines.index("fabric forwarding anycast-gateway-mac 0000.2222.3333")


def test_junos_uses_vlan_based_mac_vrf_for_nxos_interop():
    # Found live: default-switch (VLAN-aware) sends Ethernet Tag = VNI; NX-OS drops those routes.
    cfg = CFG["leaf3"]
    assert "set routing-instances MACVRF-10010 service-type vlan-based" in cfg
    assert "set routing-instances MACVRF-10010 vlans WEB vxlan vni 10010" in cfg
    assert "set routing-instances MACVRF-10010 interface ge-0/0/5.0" in cfg
    assert "set protocols evpn extended-vni-list" not in cfg
    assert cfg.index("delete routing-instances") < cfg.index("set routing-instances")


def test_junos_tenant_vrf_uses_symmetric_irb_like_nxos():
    assert "set routing-instances TENANT1 protocols evpn irb-symmetric-routing vni 50001" in CFG["leaf3"]
    assert "member vni 50001 associate-vrf" in CFG["leaf1"]   # NX-OS side of the same L3VNI


def test_nxos_lines_match_running_config_form():
    # Found by Ansible check mode on a live Nexus 9000v: NX-OS expands `route-target both`
    # and hides access-port defaults, so those forms always showed as changed.
    assert "route-target both" not in CFG["leaf1"]
    port = CFG["leaf1"].split("interface Ethernet1/5\n", 1)[1].split("\ninterface", 1)[0]
    assert "switchport access vlan 10" in port and "  switchport\n" not in port and "no shutdown" not in port
