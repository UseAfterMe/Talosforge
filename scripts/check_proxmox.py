#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from proxmox_api import ProxmoxClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate planned Talosforge VMIDs against Proxmox.")
    parser.add_argument("--file", default="terraform.tfvars.json")
    parser.add_argument("--allow-vmid", action="append", default=[])
    args = parser.parse_args()

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    proxmox = data["proxmox"]
    nodes = data["nodes"]
    proxmox_password = os.environ.get("PROXMOX_PASSWORD") or os.environ.get("TF_VAR_proxmox_password")

    if not proxmox_password:
        print(
            "Missing Proxmox password. Set PROXMOX_PASSWORD or TF_VAR_proxmox_password.",
            file=sys.stderr,
        )
        return 1

    planned_vmids = {
        name: node["vm_id"]
        for name, node in nodes.items()
    }
    planned_hosts = {
        node.get("host_node") or proxmox["node_default"]
        for node in nodes.values()
    }
    allow_vmids = {int(value) for value in args.allow_vmid}

    client = ProxmoxClient(
        api_url=proxmox["endpoint"],
        username=proxmox["username"],
        password=proxmox_password,
        insecure=bool(proxmox.get("insecure", False)),
    )

    try:
        client.login()
        resources = client.get("/cluster/resources?type=vm").get("data", [])
        cluster_nodes = client.get("/nodes").get("data", [])
    except Exception as exc:  # noqa: BLE001
        print(f"Unable to validate Proxmox API state: {exc}", file=sys.stderr)
        return 1

    known_hosts = {entry.get("node") for entry in cluster_nodes if entry.get("node")}
    missing_hosts = sorted(host for host in planned_hosts if host not in known_hosts)
    if missing_hosts:
        print(
            "Config error: planned host_node values are not present in Proxmox: "
            + ", ".join(missing_hosts),
            file=sys.stderr,
        )
        return 1

    used_vmids: dict[int, tuple[str, str]] = {}
    for entry in resources:
        if "vmid" not in entry or str(entry.get("template", 0)) == "1":
            continue
        vmid = int(entry["vmid"])
        used_vmids[vmid] = (str(entry.get("name", "")), str(entry.get("node", "")))

    conflicts: list[str] = []
    for node_name, vmid in sorted(planned_vmids.items()):
        if vmid in allow_vmids:
            continue
        if vmid in used_vmids:
            existing_name, existing_host = used_vmids[vmid]
            conflicts.append(f"{node_name} wants vm_id {vmid}, but Proxmox already uses it for {existing_name} on {existing_host}")

    if conflicts:
        for conflict in conflicts:
            print(f"Config error: {conflict}", file=sys.stderr)
        return 1

    print("Validated Proxmox VMID availability.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
