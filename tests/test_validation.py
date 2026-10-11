import copy

import pytest
import yaml
from pydantic import ValidationError

from conftest import ROOT
from fabric.models import Fabric

BASE = yaml.safe_load((ROOT / "intent" / "fabric.yml").read_text())


def check(mutate):
    data = copy.deepcopy(BASE)
    mutate(data)
    with pytest.raises(ValidationError) as exc:
        Fabric.model_validate(data)
    return str(exc.value)


def test_base_intent_is_valid():
    Fabric.model_validate(BASE)


@pytest.mark.parametrize("name", ["add_vni"])
def test_valid_scenarios(name):
    Fabric.model_validate(yaml.safe_load((ROOT / "scenarios" / f"{name}.yml").read_text()))


@pytest.mark.parametrize("name", ["bad_duplicate_vni", "bad_missing_uplink", "bad_overlapping_subnet"])
def test_bad_scenarios_are_blocked(name):
    with pytest.raises(ValidationError):
        Fabric.model_validate(yaml.safe_load((ROOT / "scenarios" / f"{name}.yml").read_text()))


def test_duplicate_vni_across_l2_and_l3():
    assert "duplicate vni: 50001" in check(lambda d: d["vnis"][0].update(vni=50001))


def test_duplicate_leaf_asn():
    assert "duplicate leaf asn: 65001" in check(lambda d: d["devices"][3].update(asn=65001))


def test_leaf_reusing_spine_asn():
    assert "reuses the spine ASN" in check(lambda d: d["devices"][2].update(asn=65000))


def test_missing_uplink():
    assert "leaf4: no uplink to spine1" in check(lambda d: d.update(links=[l for l in d["links"] if l["id"] != 3]))


def test_interface_used_twice():
    assert "duplicate interface: leaf1:Ethernet1/5" in check(
        lambda d: d["devices"][2]["host_ports"].append({"interface": "Ethernet1/5", "vlan": 20}))


def test_leaf_to_leaf_link_rejected():
    assert "must be spine (a) -> leaf (b)" in check(
        lambda d: d["links"].append({"id": 9, "a": "leaf1:Ethernet1/9", "b": "leaf2:Ethernet1/9"}))


def test_gateway_must_be_host_address():
    assert "not a usable host address" in check(lambda d: d["vnis"][0].update(gateway="10.10.10.0/24"))


def test_host_port_vlan_without_vni():
    assert "vlan 99 has no VNI" in check(lambda d: d["devices"][2]["host_ports"][0].update(vlan=99))


def test_unknown_vrf():
    assert "unknown vrf" in check(lambda d: d["vnis"][0].update(vrf="NOPE"))


def test_overlay_subnet_overlapping_infrastructure():
    assert "overlaps infrastructure pool" in check(lambda d: d["vnis"][0].update(gateway="10.0.0.200/24"))


def test_leaf_pair_intent_is_valid():
    Fabric.model_validate(yaml.safe_load((ROOT / "intent" / "multivendor_pair.yml").read_text()))


def test_leaf_pair_rejects_spines_and_wrong_size():
    def mutate(d):
        d["fabric"]["topology"] = "leaf-pair"
    msg = check(mutate)  # base fabric has 2 spines, 4 leaves
    assert "exactly 2 leaves and no spines" in msg


def test_leaf_pair_needs_leaf_to_leaf_links():
    data = yaml.safe_load((ROOT / "intent" / "multivendor_pair.yml").read_text())
    data["links"] = []
    with pytest.raises(ValidationError) as exc:
        Fabric.model_validate(data)
    assert "at least one link" in str(exc.value)
