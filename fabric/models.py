"""Intent schema and fabric-wide validation.

Field-level checks live on the models; cross-object rules (topology, uniqueness,
addressing) live in Fabric.check_topology so a bad change is rejected before
any config is rendered.
"""
from __future__ import annotations

import ipaddress
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class FabricSettings(BaseModel):
    name: str
    spine_asn: int = Field(ge=64512, le=65534)
    rt_asn: int = Field(ge=1, le=65535)
    loopback_pool: ipaddress.IPv4Network
    p2p_pool: ipaddress.IPv4Network
    anycast_mac: str = Field(pattern=r"^[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}$")
    mtu: int = Field(ge=1500, le=9216)
    # leaf-pair: two leaves cabled back to back, no spines (small labs, vendor interop tests)
    topology: Literal["spine-leaf", "leaf-pair"] = "spine-leaf"

    @property
    def anycast_mac_colon(self) -> str:
        raw = self.anycast_mac.replace(".", "").lower()
        return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


class HostPort(BaseModel):
    interface: str
    vlan: int


class Device(BaseModel):
    name: str
    id: int = Field(ge=1, le=250)
    role: Literal["spine", "leaf"]
    platform: Literal["nxos", "junos"]
    mgmt_ip: ipaddress.IPv4Address
    asn: int | None = Field(default=None, ge=64512, le=65534)
    host_ports: list[HostPort] = []

    @model_validator(mode="after")
    def leaf_needs_asn(self):
        if self.role == "leaf" and self.asn is None:
            raise ValueError(f"{self.name}: leaf requires an asn")
        if self.role == "spine" and self.host_ports:
            raise ValueError(f"{self.name}: spines must not have host ports")
        return self


class Link(BaseModel):
    id: int = Field(ge=0)
    a: str
    b: str

    @field_validator("a", "b")
    @classmethod
    def endpoint_format(cls, v: str) -> str:
        if v.count(":") != 1:
            raise ValueError(f"endpoint {v!r} must be 'device:interface'")
        return v

    def ends(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return tuple(self.a.split(":")), tuple(self.b.split(":"))  # type: ignore[return-value]


class Vrf(BaseModel):
    name: str
    l3vni: int = Field(ge=1, le=16777214)
    l3vni_vlan: int = Field(ge=2, le=4094)


class Vni(BaseModel):
    name: str
    vlan: int = Field(ge=2, le=4094)
    vni: int = Field(ge=1, le=16777214)
    vrf: str
    gateway: ipaddress.IPv4Interface


class Fabric(BaseModel):
    fabric: FabricSettings
    devices: list[Device]
    links: list[Link]
    vrfs: list[Vrf]
    vnis: list[Vni]

    @model_validator(mode="after")
    def check_topology(self):
        errors: list[str] = []
        by_name = {d.name: d for d in self.devices}

        _dupes(errors, "device name", [d.name for d in self.devices])
        _dupes(errors, "device id", [d.id for d in self.devices])
        _dupes(errors, "mgmt ip", [str(d.mgmt_ip) for d in self.devices])
        _dupes(errors, "leaf asn", [d.asn for d in self.devices if d.role == "leaf"])
        if any(d.asn == self.fabric.spine_asn for d in self.devices if d.role == "leaf"):
            errors.append("a leaf reuses the spine ASN")

        # Cabling: every link is spine<->leaf (or leaf<->leaf in a leaf pair), no interface
        # used twice, full spine/leaf mesh.
        leaf_pair = self.fabric.topology == "leaf-pair"
        _dupes(errors, "link id", [l.id for l in self.links])
        max_links = self.fabric.p2p_pool.num_addresses // 2
        used_ifaces: list[str] = []
        pairs: set[tuple[str, str]] = set()
        for link in self.links:
            if link.id >= max_links:
                errors.append(f"link {link.id}: outside p2p_pool ({max_links} /31s available)")
            (a_dev, a_if), (b_dev, b_if) = link.ends()
            for dev in (a_dev, b_dev):
                if dev not in by_name:
                    errors.append(f"link {link.id}: unknown device {dev!r}")
            if a_dev in by_name and b_dev in by_name:
                want = ("leaf", "leaf") if leaf_pair else ("spine", "leaf")
                if (by_name[a_dev].role, by_name[b_dev].role) != want:
                    errors.append(f"link {link.id}: must be {want[0]} (a) -> {want[1]} (b)")
                pairs.add((a_dev, b_dev))
            used_ifaces += [link.a, link.b]
        for d in self.devices:
            used_ifaces += [f"{d.name}:{p.interface}" for p in d.host_ports]
        _dupes(errors, "interface", used_ifaces)

        spines = [d.name for d in self.devices if d.role == "spine"]
        leaves = [d.name for d in self.devices if d.role == "leaf"]
        if leaf_pair:
            if spines or len(leaves) != 2:
                errors.append("leaf-pair topology needs exactly 2 leaves and no spines")
            if not self.links:
                errors.append("leaf-pair topology needs at least one link between the leaves")
        elif not spines or not leaves:
            errors.append("fabric needs at least one spine and one leaf")
        for leaf in ([] if leaf_pair else leaves):
            missing = [s for s in spines if (s, leaf) not in pairs]
            if missing:
                errors.append(f"{leaf}: no uplink to {', '.join(missing)}")

        # Addressing
        loopback_hosts = self.fabric.loopback_pool.num_addresses
        for d in self.devices:
            if d.id >= loopback_hosts:
                errors.append(f"{d.name}: id {d.id} outside loopback_pool")
        if self.fabric.loopback_pool.overlaps(self.fabric.p2p_pool):
            errors.append("loopback_pool overlaps p2p_pool")

        # Overlay: VNI/VLAN uniqueness across L2 and L3, VRF references, gateway subnets.
        vrf_names = {v.name for v in self.vrfs}
        _dupes(errors, "vrf name", [v.name for v in self.vrfs])
        _dupes(errors, "vni", [v.vni for v in self.vnis] + [v.l3vni for v in self.vrfs])
        _dupes(errors, "vlan", [v.vlan for v in self.vnis] + [v.l3vni_vlan for v in self.vrfs])
        nets = []
        for v in self.vnis:
            if v.vrf not in vrf_names:
                errors.append(f"vni {v.vni}: unknown vrf {v.vrf!r}")
            if v.gateway.ip in (v.gateway.network.network_address, v.gateway.network.broadcast_address):
                errors.append(f"vni {v.vni}: gateway {v.gateway} is not a usable host address")
            nets.append((v.vni, v.gateway.network))
        for i, (vni_a, net_a) in enumerate(nets):
            for vni_b, net_b in nets[i + 1:]:
                if net_a.overlaps(net_b):
                    errors.append(f"vni {vni_a} subnet {net_a} overlaps vni {vni_b} subnet {net_b}")
            for pool in (self.fabric.loopback_pool, self.fabric.p2p_pool):
                if net_a.overlaps(pool):
                    errors.append(f"vni {vni_a} subnet {net_a} overlaps infrastructure pool {pool}")

        vlans = {v.vlan for v in self.vnis}
        for d in self.devices:
            for p in d.host_ports:
                if p.vlan not in vlans:
                    errors.append(f"{d.name}:{p.interface}: vlan {p.vlan} has no VNI")

        if errors:
            raise ValueError("intent rejected:\n  - " + "\n  - ".join(errors))
        return self


def _dupes(errors: list[str], label: str, values: list) -> None:
    seen, dup = set(), set()
    for v in values:
        (dup if v in seen else seen).add(v)
    for v in sorted(dup, key=str):
        errors.append(f"duplicate {label}: {v}")
