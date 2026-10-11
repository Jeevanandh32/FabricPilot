"""Load intent, derive per-device data, render vendor configs."""
from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from fabric.models import Device, Fabric

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"


def load_intent(path: str | Path) -> Fabric:
    """Parse and validate intent. Raises pydantic.ValidationError on bad data."""
    with open(path) as fh:
        return Fabric.model_validate(yaml.safe_load(fh))


def loopback(fab: Fabric, dev: Device) -> ipaddress.IPv4Address:
    return fab.fabric.loopback_pool.network_address + dev.id


def device_context(fab: Fabric, dev: Device) -> dict:
    """Everything a template needs for one device; templates hold no logic beyond syntax."""
    by_name = {d.name: d for d in fab.devices}
    p2p = list(fab.fabric.p2p_pool.subnets(new_prefix=31))

    underlay, overlay = [], []
    for link in sorted(fab.links, key=lambda l: l.id):
        (a_dev, a_if), (b_dev, b_if) = link.ends()
        if dev.name not in (a_dev, b_dev):
            continue
        net = p2p[link.id]
        spine_ip, leaf_ip = net[0], net[1]
        if dev.name == a_dev:
            local_if, local_ip, peer, peer_ip = a_if, spine_ip, by_name[b_dev], leaf_ip
        else:
            local_if, local_ip, peer, peer_ip = b_if, leaf_ip, by_name[a_dev], spine_ip
        peer_asn = fab.fabric.spine_asn if peer.role == "spine" else peer.asn
        underlay.append({
            "interface": local_if, "ip": f"{local_ip}/31",
            "peer": peer.name, "peer_ip": str(peer_ip), "peer_asn": peer_asn,
        })
        overlay.append({"peer": peer.name, "peer_ip": str(loopback(fab, peer)), "peer_asn": peer_asn})

    is_leaf = dev.role == "leaf"
    rt = fab.fabric.rt_asn
    vni_by_vlan = {v.vlan: v for v in fab.vnis}
    return {
        "dev": dev,
        "fabric": fab.fabric,
        "asn": dev.asn if is_leaf else fab.fabric.spine_asn,
        "loopback": str(loopback(fab, dev)),
        "underlay": underlay,
        "overlay": overlay,
        "vrfs": [{**v.model_dump(), "rt": f"{rt}:{v.l3vni}"} for v in fab.vrfs] if is_leaf else [],
        "vnis": [{**v.model_dump(), "rt": f"{rt}:{v.vni}", "gateway": str(v.gateway)}
                 for v in fab.vnis] if is_leaf else [],
        "host_ports": [{"interface": p.interface, "vlan": p.vlan, "vlan_name": vni_by_vlan[p.vlan].name}
                       for p in dev.host_ports],
    }


def render_all(fab: Fabric) -> dict[str, str]:
    env = Environment(loader=FileSystemLoader(TEMPLATES), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    out = {}
    for dev in fab.devices:
        tmpl = env.get_template(f"{dev.platform}_{dev.role}.j2")
        out[dev.name] = tmpl.render(**device_context(fab, dev))
    return out


def write_build(configs: dict[str, str], build_dir: Path) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    for name, text in configs.items():
        (build_dir / f"{name}.cfg").write_text(text)
