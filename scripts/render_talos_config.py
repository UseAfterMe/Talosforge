#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path
from string import Template

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def run_command(args: list[str]) -> None:
    subprocess.run(args, check=True)


def endpoint_host(config: dict) -> str:
    controlplanes = sorted(
        [(name, node) for name, node in config["nodes"].items() if node["role"] == "controlplane"],
        key=lambda item: item[0],
    )
    if len(controlplanes) > 1:
        return config["cluster"]["api_vip"]
    return controlplanes[0][1]["ip"]


def controlplane_nodes(config: dict) -> list[tuple[str, dict]]:
    return sorted(
        [(name, node) for name, node in config["nodes"].items() if node["role"] == "controlplane"],
        key=lambda item: item[0],
    )


def worker_nodes(config: dict) -> list[tuple[str, dict]]:
    return sorted(
        [(name, node) for name, node in config["nodes"].items() if node["role"] == "worker"],
        key=lambda item: item[0],
    )


def installer_image(config: dict) -> str:
    image = config["image"]
    cluster = config["cluster"]
    return f"factory.talos.dev/installer/{image['schematic_id']}:{cluster['talos_version']}"


def node_hostname(cluster_name: str, node_name: str) -> str:
    suffix = re.sub(r"-0*", "", node_name)
    return f"{cluster_name}-{suffix}"


def additional_sans(config: dict) -> list[str]:
    values: list[str] = []
    endpoint = endpoint_host(config)
    values.append(endpoint)
    values.extend(node["ip"] for _, node in controlplane_nodes(config))
    values.extend(config["cluster"].get("additional_sans", []))
    deduped: list[str] = []
    for item in values:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def write_global_patch(config: dict, out_dir: Path) -> Path:
    patch_path = out_dir / "patches" / "global.yaml"
    lines: list[str] = []

    if config.get("addons", {}).get("cilium_enabled", True):
        lines.extend(
            [
                "cluster:",
                "  network:",
                "    cni:",
                "      name: none",
                "  proxy:",
                "    disabled: true",
            ]
        )

    if not lines:
        patch_path.write_text("{}\n", encoding="utf-8")
    else:
        patch_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return patch_path


def render_node_patch(
    *,
    node_name: str,
    node: dict,
    cluster: dict,
    proxmox: dict,
    template_path: Path,
    installer: str,
    vip_enabled: bool,
    vip_ip: str | None,
) -> str:
    template = Template(template_path.read_text(encoding="utf-8"))
    nameservers = cluster.get("nameservers", [])
    nameservers_block = ""
    if nameservers:
        nameservers_block = "    nameservers:\n" + "".join(f"      - {value}\n" for value in nameservers)

    vip_block = ""
    if vip_enabled and vip_ip is not None:
        vip_block = f"        vip:\n          ip: {vip_ip}\n"

    network_interface = node.get("network_interface") or cluster.get("network_interface", "eth0")

    rendered = template.substitute(
        hostname=node_hostname(cluster["name"], node_name),
        network_interface=network_interface,
        address_cidr=f"{node['ip']}/{cluster['prefix']}",
        gateway=cluster["gateway"],
        nameservers_block=nameservers_block,
        vip_block=vip_block,
        install_disk=cluster["install_disk"],
        installer_image=installer,
        topology_region=proxmox.get("cluster_name") or node.get("host_node") or "proxmox",
        topology_zone=node.get("host_node") or proxmox.get("node_default", "proxmox"),
    )
    return rendered


def write_node_patches(config: dict, out_dir: Path) -> tuple[list[dict], list[dict]]:
    cluster = config["cluster"]
    proxmox = config.get("proxmox", {})
    installer = installer_image(config)
    controlplanes: list[dict] = []
    workers: list[dict] = []
    ha = len(controlplane_nodes(config)) > 1
    vip_ip = cluster.get("api_vip")

    for node_name, node in controlplane_nodes(config):
        patch_path = out_dir / "patches" / f"{node_name}.yaml"
        patch_path.write_text(
            render_node_patch(
                node_name=node_name,
                node=node,
                cluster=cluster,
                proxmox=proxmox,
                template_path=REPO_ROOT / "templates/controlplane.yaml.tftpl",
                installer=installer,
                vip_enabled=ha,
                vip_ip=vip_ip,
            ),
            encoding="utf-8",
        )
        controlplanes.append({"name": node_name, "ip": node["ip"], "patch_path": str(patch_path)})

    for node_name, node in worker_nodes(config):
        patch_path = out_dir / "patches" / f"{node_name}.yaml"
        patch_path.write_text(
            render_node_patch(
                node_name=node_name,
                node=node,
                cluster=cluster,
                proxmox=proxmox,
                template_path=REPO_ROOT / "templates/worker.yaml.tftpl",
                installer=installer,
                vip_enabled=False,
                vip_ip=None,
            ),
            encoding="utf-8",
        )
        workers.append({"name": node_name, "ip": node["ip"], "patch_path": str(patch_path)})

    return controlplanes, workers


def write_cilium_values(config: dict, out_dir: Path) -> Path | None:
    if not config.get("addons", {}).get("cilium_enabled", True):
        return None

    cp_count = len(controlplane_nodes(config))
    operator_replicas = 1 if cp_count == 1 else 2
    values_path = out_dir / "addons" / "cilium-values.yaml"
    values_path.write_text(
        "\n".join(
            [
                "ipam:",
                "  mode: kubernetes",
                "kubeProxyReplacement: true",
                "k8sServiceHost: localhost",
                "k8sServicePort: 7445",
                "k8sClientRateLimit:",
                "  qps: 20",
                "  burst: 40",
                "l2announcements:",
                "  enabled: true",
                "externalIPs:",
                "  enabled: true",
                "l7Proxy: false",
                "envoy:",
                "  enabled: false",
                "bpf:",
                "  hostLegacyRouting: true",
                "cgroup:",
                "  autoMount:",
                "    enabled: false",
                "  hostRoot: /sys/fs/cgroup",
                "operator:",
                f"  replicas: {operator_replicas}",
                "securityContext:",
                "  capabilities:",
                "    ciliumAgent:",
                "      - CHOWN",
                "      - KILL",
                "      - NET_ADMIN",
                "      - NET_RAW",
                "      - IPC_LOCK",
                "      - SYS_ADMIN",
                "      - SYS_RESOURCE",
                "      - DAC_OVERRIDE",
                "      - FOWNER",
                "      - SETGID",
                "      - SETUID",
                "    cleanCiliumState:",
                "      - NET_ADMIN",
                "      - SYS_ADMIN",
                "      - SYS_RESOURCE",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return values_path


def expand_load_balancer_pool(pool: str) -> str:
    value = pool.strip()
    if "-" not in value:
        return value

    start_raw, end_raw = [item.strip() for item in value.split("-", 1)]
    start_ip = ipaddress.ip_address(start_raw)

    if "." in end_raw or ":" in end_raw:
        end_ip = ipaddress.ip_address(end_raw)
    else:
        octets = start_raw.split(".")
        octets[-1] = end_raw
        end_ip = ipaddress.ip_address(".".join(octets))

    return f"{start_ip}-{end_ip}"


def write_cilium_load_balancer_resources(config: dict, out_dir: Path) -> Path | None:
    addons = config.get("addons", {})
    pools = [expand_load_balancer_pool(pool) for pool in addons.get("load_balancer_ip_pools", [])]
    if not pools:
        return None

    pool_name = addons.get("cilium_lb_pool_name", "default")
    policy_name = addons.get("cilium_l2_policy_name", "default-l2")
    resources_path = out_dir / "addons" / "cilium-load-balancer-resources.yaml"
    blocks: list[str] = []

    for pool in pools:
        if "/" in pool and "-" not in pool:
            blocks.extend(
                [
                    f"  - cidr: {pool}",
                ]
            )
            continue
        start_ip, stop_ip = pool.split("-", 1) if "-" in pool else (pool, pool)
        blocks.extend(
            [
                f"  - start: {start_ip}",
                f"    stop: {stop_ip}",
            ]
        )

    resources_path.write_text(
        "\n".join(
            [
                "apiVersion: cilium.io/v2alpha1",
                "kind: CiliumLoadBalancerIPPool",
                "metadata:",
                f"  name: {pool_name}",
                "spec:",
                "  blocks:",
                *blocks,
                "---",
                "apiVersion: cilium.io/v2alpha1",
                "kind: CiliumL2AnnouncementPolicy",
                "metadata:",
                f"  name: {policy_name}",
                "spec:",
                "  externalIPs: true",
                "  loadBalancerIPs: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return resources_path


def write_traefik_values(config: dict, out_dir: Path) -> Path | None:
    if not config.get("addons", {}).get("traefik_enabled", False):
        return None

    worker_count = len(worker_nodes(config))
    replicas = 2 if worker_count > 1 else 1
    values_path = out_dir / "addons" / "traefik-values.yaml"
    values_path.write_text(
        "\n".join(
            [
                "deployment:",
                f"  replicas: {replicas}",
                "service:",
                "  enabled: true",
                "  type: LoadBalancer",
                "providers:",
                "  kubernetesCRD:",
                "    enabled: true",
                "  kubernetesIngress:",
                "    enabled: true",
                "ingressRoute:",
                "  dashboard:",
                "    enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return values_path


def write_proxmox_csi_values(config: dict, out_dir: Path) -> Path | None:
    addons = config.get("addons", {})
    if not addons.get("proxmox_csi_enabled", False):
        return None

    storage = addons.get("proxmox_csi_storage", "")
    values_path = out_dir / "addons" / "proxmox-csi-values.yaml"
    values_path.write_text(
        "\n".join(
            [
                "existingConfigSecret: proxmox-csi-plugin",
                "existingConfigSecretKey: config.yaml",
                "",
                "storageClass:",
                "  - name: proxmox",
                f"    storage: {storage}",
                "    reclaimPolicy: Delete",
                "    fstype: ext4",
                "    cache: writethrough",
                "    ssd: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return values_path


def write_bootstrap_plan(controlplanes: list[dict], workers: list[dict], out_dir: Path) -> None:
    bootstrap_lines = [
        f"controlplane\t{node['name']}\t{node['ip']}\t{node['patch_path']}"
        for node in controlplanes
    ]
    bootstrap_lines.extend(
        f"worker\t{node['name']}\t{node['ip']}\t{node['patch_path']}"
        for node in workers
    )
    (out_dir / "bootstrap-nodes.tsv").write_text("\n".join(bootstrap_lines) + "\n", encoding="utf-8")
    (out_dir / "worker-nodes.txt").write_text(
        "".join(f"{node['ip']}\n" for node in workers),
        encoding="utf-8",
    )


def write_metadata(
    *,
    config: dict,
    out_dir: Path,
    controlplanes: list[dict],
    workers: list[dict],
    cilium_values_path: Path | None,
    cilium_load_balancer_resources_path: Path | None,
    traefik_values_path: Path | None,
    proxmox_csi_values_path: Path | None,
) -> None:
    cluster_endpoint = f"https://{endpoint_host(config)}:6443"
    metadata = {
        "cluster_name": config["cluster"]["name"],
        "bootstrap_controlplane_ip": controlplanes[0]["ip"],
        "controlplane_ips": [node["ip"] for node in controlplanes],
        "worker_ips": [node["ip"] for node in workers],
        "controlplane_csv": ",".join(node["ip"] for node in controlplanes),
        "kubernetes_endpoint": cluster_endpoint,
        "talosconfig_path": str(out_dir / "talosconfig"),
        "controlplane_config_path": str(out_dir / "controlplane.yaml"),
        "worker_config_path": str(out_dir / "worker.yaml"),
        "kubeconfig_path": str(out_dir / "kubeconfig"),
        "cilium_enabled": config.get("addons", {}).get("cilium_enabled", True),
        "cilium_chart_version": config.get("addons", {}).get("cilium_chart_version", "1.19.1"),
        "cilium_values_path": str(cilium_values_path) if cilium_values_path else "",
        "load_balancer_ip_pools": config.get("addons", {}).get("load_balancer_ip_pools", []),
        "cilium_lb_pool_name": config.get("addons", {}).get("cilium_lb_pool_name", "default"),
        "cilium_l2_policy_name": config.get("addons", {}).get("cilium_l2_policy_name", "default-l2"),
        "cilium_load_balancer_resources_path": str(cilium_load_balancer_resources_path) if cilium_load_balancer_resources_path else "",
        "traefik_enabled": config.get("addons", {}).get("traefik_enabled", False),
        "traefik_chart_version": config.get("addons", {}).get("traefik_chart_version", "39.0.7"),
        "traefik_values_path": str(traefik_values_path) if traefik_values_path else "",
        "proxmox_csi_enabled": config.get("addons", {}).get("proxmox_csi_enabled", False),
        "proxmox_csi_chart_version": config.get("addons", {}).get("proxmox_csi_chart_version", "0.5.4"),
        "proxmox_csi_storage": config.get("addons", {}).get("proxmox_csi_storage", ""),
        "proxmox_csi_values_path": str(proxmox_csi_values_path) if proxmox_csi_values_path else "",
        "proxmox_endpoint": config.get("proxmox", {}).get("endpoint", ""),
        "proxmox_insecure": config.get("proxmox", {}).get("insecure", False),
        "proxmox_username": config.get("proxmox", {}).get("username", ""),
        "proxmox_cluster_name": config.get("proxmox", {}).get("cluster_name", ""),
    }
    (out_dir / "cluster-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def gen_secrets(config: dict, out_dir: Path, force: bool) -> Path:
    secrets_path = out_dir / "secrets.yaml"
    if secrets_path.exists() and not force:
        return secrets_path
    if secrets_path.exists() and force:
        secrets_path.unlink()

    args = ["talosctl", "gen", "secrets", "--output-file", str(secrets_path)]
    if config["cluster"].get("talos_version"):
        args.extend(["--talos-version", config["cluster"]["talos_version"]])
    run_command(args)
    return secrets_path


def gen_base_configs(config: dict, out_dir: Path, secrets_path: Path, global_patch_path: Path) -> None:
    cluster = config["cluster"]
    args = [
        "talosctl",
        "gen",
        "config",
        cluster["name"],
        f"https://{endpoint_host(config)}:6443",
        "--with-secrets",
        str(secrets_path),
        "--install-disk",
        cluster["install_disk"],
        "--output",
        str(out_dir),
        "--output-types",
        "controlplane,worker,talosconfig",
        "--force",
        "--config-patch",
        f"@{global_patch_path}",
        "--talos-version",
        cluster["talos_version"],
    ]

    if cluster.get("dns_domain"):
        args.extend(["--dns-domain", cluster["dns_domain"]])

    if cluster.get("kubernetes_version"):
        args.extend(["--kubernetes-version", cluster["kubernetes_version"]])

    for san in additional_sans(config):
        args.extend(["--additional-sans", san])

    run_command(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Talos machine configs for Talosforge.")
    parser.add_argument("--file", default="terraform.tfvars.json")
    parser.add_argument("--out-dir", default="out")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.file)
    if not config_path.exists():
        print(f"Missing {config_path}", file=sys.stderr)
        return 1

    if not shutil_which("talosctl"):
        print("Missing talosctl in PATH.", file=sys.stderr)
        return 1

    config = load_json(config_path)
    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)
    ensure_dir(out_dir / "patches")
    ensure_dir(out_dir / "addons")

    secrets_path = gen_secrets(config, out_dir, force=args.force)
    global_patch_path = write_global_patch(config, out_dir)
    gen_base_configs(config, out_dir, secrets_path, global_patch_path)
    controlplanes, workers = write_node_patches(config, out_dir)
    cilium_values_path = write_cilium_values(config, out_dir)
    cilium_load_balancer_resources_path = write_cilium_load_balancer_resources(config, out_dir)
    traefik_values_path = write_traefik_values(config, out_dir)
    proxmox_csi_values_path = write_proxmox_csi_values(config, out_dir)
    write_bootstrap_plan(controlplanes, workers, out_dir)
    write_metadata(
        config=config,
        out_dir=out_dir,
        controlplanes=controlplanes,
        workers=workers,
        cilium_values_path=cilium_values_path,
        cilium_load_balancer_resources_path=cilium_load_balancer_resources_path,
        traefik_values_path=traefik_values_path,
        proxmox_csi_values_path=proxmox_csi_values_path,
    )

    print(f"Rendered Talos artifacts into {out_dir}")
    return 0


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


if __name__ == "__main__":
    raise SystemExit(main())
