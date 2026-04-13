#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from proxmox_api import ProxmoxClient


def replace_all(value: str, target: str, replacement: str) -> str:
    return value.replace(target, replacement)


def talos_image_name(config: dict) -> str:
    image = config["image"]
    cluster = config["cluster"]
    schematic_id = image.get("schematic_id")
    if not isinstance(schematic_id, str) or not schematic_id.strip():
        schematic_file = image.get("schematic_file")
        if isinstance(schematic_file, str) and schematic_file.strip():
            schematic_id = Path(schematic_file).read_text(encoding="utf-8").strip()
        else:
            raise ValueError("Missing image.schematic_id and image.schematic_file.")

    talos_version = str(cluster["talos_version"])
    platform = str(image.get("platform", "nocloud"))
    arch = str(image.get("arch", "amd64"))
    return f"talos-{replace_all(talos_version, '.', '-')}-{platform}-{arch}-{schematic_id[:12]}.img"


def main() -> int:
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else "terraform.tfvars.json")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    proxmox = data["proxmox"]
    nodes = data["nodes"]
    proxmox_password = os.environ.get("PROXMOX_PASSWORD") or os.environ.get("TF_VAR_proxmox_password")

    if not proxmox_password:
        print("Missing Proxmox password. Set PROXMOX_PASSWORD or TF_VAR_proxmox_password.", file=sys.stderr)
        return 1

    image_datastore = str(proxmox["image_datastore"])
    image_name = talos_image_name(data)
    planned_hosts = sorted({str(node.get("host_node") or proxmox["node_default"]) for node in nodes.values()})

    client = ProxmoxClient(
        api_url=proxmox["endpoint"],
        username=proxmox["username"],
        password=proxmox_password,
        insecure=bool(proxmox.get("insecure", False)),
    )

    try:
        client.login()
    except Exception as exc:  # noqa: BLE001
        print(f"Unable to log into Proxmox API: {exc}", file=sys.stderr)
        return 1

    results: list[tuple[str, str, str | None]] = []
    for node_name in planned_hosts:
        try:
            contents = client.get(f"/nodes/{node_name}/storage/{image_datastore}/content").get("data", [])
        except Exception as exc:  # noqa: BLE001
            print(f"Unable to inspect image datastore '{image_datastore}' on {node_name}: {exc}", file=sys.stderr)
            return 1

        matched = None
        for entry in contents:
            haystack = " ".join(
                str(entry.get(field, ""))
                for field in ("volid", "text", "path", "notes")
            )
            if image_name in haystack:
                matched = str(entry.get("volid") or entry.get("path") or entry.get("text") or image_name)
                break

        if matched:
            results.append((node_name, "reuse", matched))
        else:
            results.append((node_name, "download", None))

    print(f"Talos image cache status for {image_name}:")
    for node_name, status, detail in results:
        if status == "reuse":
            print(f"  - {node_name}: reuse cached image ({detail})")
        else:
            print(f"  - {node_name}: download required to datastore '{image_datastore}'")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
