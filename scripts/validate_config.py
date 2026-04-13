#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path


HOSTNAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DNS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
URL_RE = re.compile(r"^https://")


def validate_ip(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append(f"{label} must be a string IP address.")
        return
    try:
        ipaddress.ip_address(value)
    except ValueError:
        errors.append(f"{label} '{value}' is not a valid IP address.")


def validate_hostname(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not HOSTNAME_RE.fullmatch(value):
        errors.append(f"{label} must be a lowercase DNS-safe label.")


def validate_metallb_pool(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty CIDR or IP range string.")
        return

    raw = value.strip()
    if "-" not in raw:
        try:
            ipaddress.ip_network(raw, strict=False)
        except ValueError:
            errors.append(f"{label} '{raw}' is not a valid CIDR.")
        return

    start_raw, end_raw = [part.strip() for part in raw.split("-", 1)]
    try:
        start_ip = ipaddress.ip_address(start_raw)
    except ValueError:
        errors.append(f"{label} '{raw}' is not a valid IP range.")
        return

    try:
        if "." in end_raw:
            end_ip = ipaddress.ip_address(end_raw)
        else:
            octets = start_raw.split(".")
            octets[-1] = end_raw
            end_ip = ipaddress.ip_address(".".join(octets))
    except ValueError:
        errors.append(f"{label} '{raw}' is not a valid IP range.")
        return

    if start_ip.version != end_ip.version or int(end_ip) < int(start_ip):
        errors.append(f"{label} '{raw}' must be an ascending IP range.")


def validate_data(data: dict) -> list[str]:
    errors: list[str] = []

    proxmox = data.get("proxmox")
    if not isinstance(proxmox, dict):
        return ["Missing required object: proxmox"]

    cluster = data.get("cluster")
    if not isinstance(cluster, dict):
        return ["Missing required object: cluster"]

    nodes = data.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return ["At least one node must be defined in nodes."]

    endpoint = proxmox.get("endpoint")
    if not isinstance(endpoint, str) or not URL_RE.match(endpoint):
        errors.append("proxmox.endpoint must be an https:// URL.")

    for field in ("username", "node_default", "image_datastore", "vm_datastore", "network_bridge"):
        if not isinstance(proxmox.get(field), str) or not proxmox.get(field):
            errors.append(f"proxmox.{field} must be set.")
    if proxmox.get("cluster_name") is not None and (not isinstance(proxmox.get("cluster_name"), str) or not proxmox.get("cluster_name")):
        errors.append("proxmox.cluster_name must be a non-empty string when set.")

    validate_hostname(cluster.get("name"), "cluster.name", errors)
    validate_ip(cluster.get("gateway"), "cluster.gateway", errors)

    prefix = cluster.get("prefix")
    if not isinstance(prefix, int) or prefix < 1 or prefix > 32:
        errors.append("cluster.prefix must be an integer between 1 and 32.")

    dns_domain = cluster.get("dns_domain")
    if dns_domain is not None and (not isinstance(dns_domain, str) or not DNS_RE.fullmatch(dns_domain)):
        errors.append("cluster.dns_domain must be a lowercase DNS suffix when set.")

    nameservers = cluster.get("nameservers", [])
    if nameservers is not None:
        if not isinstance(nameservers, list):
            errors.append("cluster.nameservers must be a list of IP addresses.")
        else:
            for index, item in enumerate(nameservers):
                validate_ip(item, f"cluster.nameservers[{index}]", errors)

    install_disk = cluster.get("install_disk")
    if not isinstance(install_disk, str) or not install_disk.startswith("/dev/"):
        errors.append("cluster.install_disk must look like a device path such as /dev/vda.")

    network_interface = cluster.get("network_interface", "eth0")
    if not isinstance(network_interface, str) or not network_interface:
        errors.append("cluster.network_interface must be a non-empty string.")

    for field in ("talos_version",):
        if not isinstance(cluster.get(field), str) or not cluster.get(field):
            errors.append(f"cluster.{field} must be set.")

    if cluster.get("kubernetes_version") is not None and not isinstance(cluster.get("kubernetes_version"), str):
        errors.append("cluster.kubernetes_version must be a string when set.")

    image = data.get("image", {})
    if image and not isinstance(image, dict):
        errors.append("image must be an object when set.")
        image = {}

    if not isinstance(image.get("schematic_id"), str) or not image.get("schematic_id"):
        errors.append("image.schematic_id must be set for v0.1.0.")

    if image.get("factory_base_url") is not None and (
        not isinstance(image.get("factory_base_url"), str) or not URL_RE.match(image.get("factory_base_url"))
    ):
        errors.append("image.factory_base_url must be an https:// URL.")

    if image.get("platform") is not None and image.get("platform") != "nocloud":
        errors.append("image.platform must be nocloud for v0.1.0.")

    if image.get("arch") is not None and image.get("arch") not in {"amd64", "arm64"}:
        errors.append("image.arch must be either amd64 or arm64.")

    extensions = image.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, list):
            errors.append("image.extensions must be a list of Image Factory extension names when set.")
        else:
            for index, extension in enumerate(extensions):
                if not isinstance(extension, str) or not extension.strip():
                    errors.append(f"image.extensions[{index}] must be a non-empty string.")

    controlplane_count = 0
    seen_ips: set[str] = set()
    seen_vmids: set[int] = set()

    network = None
    if isinstance(cluster.get("gateway"), str) and isinstance(prefix, int):
        try:
            network = ipaddress.ip_network(f"{cluster['gateway']}/{prefix}", strict=False)
        except ValueError:
            errors.append("cluster.gateway and cluster.prefix do not form a valid subnet.")

    for node_name, node in nodes.items():
        validate_hostname(node_name, "node name", errors)
        if not isinstance(node, dict):
            errors.append(f"Node '{node_name}' must be an object.")
            continue

        if "mac_address" in node:
            errors.append(f"Node '{node_name}' must not define mac_address. Proxmox handles NIC MAC assignment.")

        role = node.get("role")
        if role not in {"controlplane", "worker"}:
            errors.append(f"Node '{node_name}' has invalid role '{role}'.")
        if role == "controlplane":
            controlplane_count += 1

        ip_value = node.get("ip")
        validate_ip(ip_value, f"nodes.{node_name}.ip", errors)
        if isinstance(ip_value, str):
            if ip_value in seen_ips:
                errors.append(f"Duplicate node IP detected: {ip_value}")
            seen_ips.add(ip_value)
            if network is not None:
                try:
                    if ipaddress.ip_address(ip_value) not in network:
                        errors.append(
                            f"Node '{node_name}' IP '{ip_value}' is outside the cluster subnet '{network}'."
                        )
                except ValueError:
                    pass

        vm_id = node.get("vm_id")
        if not isinstance(vm_id, int):
            errors.append(f"Node '{node_name}' must have an integer vm_id.")
        elif vm_id in seen_vmids:
            errors.append(f"Duplicate VM ID detected: {vm_id}")
        else:
            seen_vmids.add(vm_id)

        for field in ("cpu", "memory_mb", "disk_gb"):
            value = node.get(field)
            if not isinstance(value, int) or value < 1:
                errors.append(f"Node '{node_name}' must define {field} as a positive integer.")

        for field in ("host_node", "bridge", "network_interface"):
            value = node.get(field)
            if value is not None and (not isinstance(value, str) or not value):
                errors.append(f"Node '{node_name}' field '{field}' must be a non-empty string when set.")

    if controlplane_count < 1:
        errors.append("At least one controlplane node is required.")

    api_vip = cluster.get("api_vip")
    if controlplane_count > 1:
        if not isinstance(api_vip, str):
            errors.append("cluster.api_vip must be set for HA control-plane clusters.")
        else:
            validate_ip(api_vip, "cluster.api_vip", errors)
            if api_vip in seen_ips:
                errors.append("cluster.api_vip must be reserved and must not match a node IP.")
    elif api_vip is not None and not isinstance(api_vip, str):
        errors.append("cluster.api_vip must be a string IP address when set.")

    if "haproxy_node" in data:
        errors.append("haproxy_node is not supported in Talosforge.")

    for deprecated_key in ("os_family", "os_version", "ssh_username", "cloud_image_url", "cloud_image_file_name"):
        if deprecated_key in data:
            errors.append(f"Deprecated key '{deprecated_key}' is not part of Talosforge.")

    addons = data.get("addons", {})
    if addons and not isinstance(addons, dict):
        errors.append("addons must be an object when set.")
    else:
        if addons.get("cilium_chart_version") is not None and not isinstance(addons.get("cilium_chart_version"), str):
            errors.append("addons.cilium_chart_version must be a string when set.")
        if addons.get("metallb_chart_version") is not None and not isinstance(addons.get("metallb_chart_version"), str):
            errors.append("addons.metallb_chart_version must be a string when set.")
        if addons.get("traefik_chart_version") is not None and not isinstance(addons.get("traefik_chart_version"), str):
            errors.append("addons.traefik_chart_version must be a string when set.")
        if addons.get("proxmox_csi_chart_version") is not None and not isinstance(addons.get("proxmox_csi_chart_version"), str):
            errors.append("addons.proxmox_csi_chart_version must be a string when set.")
        if addons.get("proxmox_csi_storage") is not None and not isinstance(addons.get("proxmox_csi_storage"), str):
            errors.append("addons.proxmox_csi_storage must be a string when set.")
        if addons.get("metallb_enabled") is True:
            pools = addons.get("metallb_pools", [])
            if not isinstance(pools, list) or not pools:
                errors.append("addons.metallb_pools must contain at least one CIDR or IP range when MetalLB is enabled.")
            else:
                for index, pool in enumerate(pools):
                    validate_metallb_pool(pool, f"addons.metallb_pools[{index}]", errors)
        if addons.get("proxmox_csi_enabled") is True:
            storage = addons.get("proxmox_csi_storage", "")
            if not isinstance(storage, str) or not storage.strip():
                errors.append("addons.proxmox_csi_storage must be set when Proxmox CSI is enabled.")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Talosforge terraform.tfvars.json.")
    parser.add_argument("--file", default="terraform.tfvars.json")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"Missing {path}", file=sys.stderr)
        return 1

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON in {path}: {exc}", file=sys.stderr)
        return 1

    errors = validate_data(data)
    if errors:
        for error in errors:
            print(f"Config error: {error}", file=sys.stderr)
        return 1

    print(f"Validated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
