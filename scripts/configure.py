#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from proxmox_api import ProxmoxClient, normalize_proxmox_api_url

try:
    import readline  # noqa: F401
except ImportError:
    readline = None


DEFAULT_TALOS_VERSION = "v1.12.6"
DEFAULT_KUBERNETES_VERSION = "1.35.3"
DEFAULT_CILIUM_CHART_VERSION = "1.19.1"
DEFAULT_TRAEFIK_CHART_VERSION = "39.0.7"
DEFAULT_PROXMOX_CSI_CHART_VERSION = "0.5.4"
DEFAULT_INSTALL_DISK = "/dev/vda"
DEFAULT_VANILLA_SCHEMATIC_ID = "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
DEFAULT_PROXMOX_SCHEMATIC_ID = "ce4c980550dd2ab1b17bbf2b08801c7eb59418eafe8f279833297925d67c7515"
PLACEHOLDER_SCHEMATIC_ID = "replace-with-image-factory-schematic-id"
DEFAULT_FACTORY_BASE_URL = "https://factory.talos.dev"
DEFAULT_PROXMOX_EXTENSIONS = ["siderolabs/qemu-guest-agent"]
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_MAGENTA = "\033[35m"
ANSI_DIM = "\033[2m"
HOSTNAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM", "").lower() != "dumb"


def colorize(text: str, color_code: str) -> str:
    if not supports_color():
        return text
    return f"{color_code}{text}{ANSI_RESET}"


def style_default_value(value: str | None) -> str:
    if value in (None, ""):
        return ""
    return colorize(str(value), ANSI_YELLOW)


def style_prompt_label(text: str) -> str:
    return colorize(text, f"{ANSI_CYAN}{ANSI_BOLD}")


def style_choice_label(choices: list[str], color_code: str = ANSI_MAGENTA) -> str:
    rendered = "/".join(choices)
    return colorize(rendered, color_code)


def style_inline_values(values: list[str], color_code: str = ANSI_CYAN) -> str:
    if not values:
        return "none"
    if not supports_color():
        return ", ".join(values)
    separator = f"{ANSI_DIM}, {ANSI_RESET}"
    return separator.join(colorize(value, color_code) for value in values)


def print_section(title: str) -> None:
    print()
    print(colorize(title, f"{ANSI_CYAN}{ANSI_BOLD}"))


def print_note(text: str) -> None:
    print(colorize(text, ANSI_DIM))


def read_prompt(rendered: str, *, secret: bool = False) -> str:
    if secret:
        return getpass.getpass(rendered)
    sys.stdout.write(rendered)
    sys.stdout.flush()
    value = sys.stdin.readline()
    if value == "":
        raise EOFError
    return value.rstrip("\n")


def prompt(
    text: str,
    default: str | None = None,
    *,
    secret: bool = False,
    show_default: bool = True,
    default_label: str | None = None,
) -> str:
    rendered_default = default if default_label is None else default_label
    if supports_color():
        suffix = f" [{style_default_value(str(rendered_default))}]" if show_default and rendered_default not in (None, "") else ""
        rendered = f"{style_prompt_label(text)}{suffix}: "
    else:
        suffix = f" [{rendered_default}]" if show_default and rendered_default not in (None, "") else ""
        rendered = f"{text}{suffix}: "
    while True:
        value = read_prompt(rendered, secret=secret)
        value = value.strip()
        if value:
            return value
        if default is not None:
            return default


def prompt_bool(text: str, default: bool) -> bool:
    label = "Y/n" if default else "y/N"
    while True:
        if supports_color():
            raw = read_prompt(f"{style_prompt_label(text)} [{style_default_value(label)}]: ").strip().lower()
        else:
            raw = input(f"{text} [{label}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print(colorize("Enter yes or no.", ANSI_YELLOW))


def prompt_int(text: str, default: int, minimum: int = 0) -> int:
    while True:
        raw = prompt(text, str(default))
        try:
            value = int(raw)
        except ValueError:
            print(colorize("Enter a whole number.", ANSI_YELLOW))
            continue
        if value < minimum:
            print(colorize(f"Enter a value >= {minimum}.", ANSI_YELLOW))
            continue
        return value


def prompt_optional_int_with_choices(text: str, default: int | None = None) -> int | None:
    rendered_default = "" if default is None else str(default)
    while True:
        raw = prompt(text, rendered_default)
        if raw == "":
            return default
        lowered = raw.lower()
        if lowered in {"none", "null"}:
            return None
        try:
            return int(raw)
        except ValueError:
            print(colorize("Enter a whole number, leave it blank, or type none.", ANSI_YELLOW))


def prompt_choice(text: str, choices: list[str], default: str) -> str:
    label = style_choice_label(choices) if supports_color() else "/".join(choices)
    while True:
        value = prompt(f"{text} ({label})", default).strip()
        if value in choices:
            return value
        print(colorize(f"Choose one of: {', '.join(choices)}", ANSI_YELLOW))


def prompt_ip(text: str, default: str) -> str:
    while True:
        raw = prompt(text, default)
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            print(colorize("Enter a valid IP address.", ANSI_YELLOW))


def expand_ipv4_range(raw: str) -> list[str]:
    value = raw.strip()
    if not value:
        raise ValueError("range cannot be empty")
    if "-" not in value:
        return [str(ipaddress.ip_address(value))]

    start_raw, end_raw = [part.strip() for part in value.split("-", 1)]
    start_ip = ipaddress.ip_address(start_raw)
    if start_ip.version != 4:
        raise ValueError("only IPv4 ranges are supported here")

    if "." in end_raw:
        end_ip = ipaddress.ip_address(end_raw)
    else:
        octets = start_raw.split(".")
        octets[-1] = end_raw
        end_ip = ipaddress.ip_address(".".join(octets))

    if end_ip.version != 4 or int(end_ip) < int(start_ip):
        raise ValueError("range end must be greater than or equal to the start")

    return [str(start_ip + offset) for offset in range(int(end_ip) - int(start_ip) + 1)]


def compact_ipv4_range(start_ip: str, end_ip: str) -> str:
    start = ipaddress.ip_address(start_ip)
    end = ipaddress.ip_address(end_ip)
    if int(end) < int(start):
        raise ValueError("range end must be greater than or equal to the start")
    if start == end:
        return str(start)
    start_parts = str(start).split(".")
    end_parts = str(end).split(".")
    if start.version == 4 and end.version == 4 and start_parts[:-1] == end_parts[:-1]:
        return f"{start}-{end_parts[-1]}"
    return f"{start}-{end}"


def are_contiguous_ips(values: list[str]) -> bool:
    if len(values) < 2:
        return True
    try:
        ips = [ipaddress.ip_address(value) for value in values]
    except ValueError:
        return False
    return all(int(ips[index]) == int(ips[index - 1]) + 1 for index in range(1, len(ips)))


def prompt_ip_range(text: str, default: str, required_count: int, *, exact_count: bool = True) -> list[str]:
    while True:
        try:
            ips = expand_ipv4_range(prompt(text, default))
        except ValueError as exc:
            print(colorize(f"Invalid IP range: {exc}", ANSI_YELLOW))
            continue
        if exact_count and len(ips) != required_count:
            print(colorize(f"Range must include exactly {required_count} IP(s).", ANSI_YELLOW))
            continue
        if not exact_count and len(ips) < required_count:
            print(colorize(f"Range must include at least {required_count} IP(s).", ANSI_YELLOW))
            continue
        return ips


def prompt_csv_ips(text: str, default: list[str]) -> list[str]:
    default_raw = ",".join(default)
    while True:
        raw = prompt(text, default_raw)
        values = [item.strip() for item in raw.split(",") if item.strip()]
        try:
            return [str(ipaddress.ip_address(item)) for item in values]
        except ValueError as exc:
            print(colorize(f"Invalid IP list: {exc}", ANSI_YELLOW))


def prompt_csv_ips_exact(text: str, default: list[str], required_count: int) -> list[str]:
    while True:
        values = prompt_csv_ips(text, default)
        if len(values) != required_count:
            print(colorize(f"Enter exactly {required_count} IP(s).", ANSI_YELLOW))
            continue
        return values


def prompt_csv_values(text: str, default: list[str]) -> list[str]:
    default_raw = ",".join(default)
    raw = prompt(text, default_raw)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    deduped: list[str] = []
    for item in values:
        if item not in deduped:
            deduped.append(item)
    return deduped


def normalize_load_balancer_pool(raw: str) -> str:
    value = raw.strip()
    if "-" not in value:
        ipaddress.ip_network(value, strict=False)
        return value

    ips = expand_ipv4_range(value)
    return compact_ipv4_range(ips[0], ips[-1])


def prompt_load_balancer_pools(text: str, default: list[str]) -> list[str]:
    default_raw = ",".join(default)
    while True:
        raw = prompt(text, default_raw)
        pools = [item.strip() for item in raw.split(",") if item.strip()]
        if not pools:
            print(colorize("Enter at least one LoadBalancer IP pool.", ANSI_YELLOW))
            continue
        try:
            deduped: list[str] = []
            for pool in pools:
                normalized = normalize_load_balancer_pool(pool)
                if normalized not in deduped:
                    deduped.append(normalized)
            return deduped
        except ValueError as exc:
            print(colorize(f"Invalid LoadBalancer pool: {exc}", ANSI_YELLOW))


def prompt_hostname_label(text: str, default: str) -> str:
    while True:
        value = prompt(text, default).strip().lower()
        if HOSTNAME_RE.fullmatch(value):
            return value
        print(colorize("Enter a lowercase DNS-safe label.", ANSI_YELLOW))


def suggest_range(start_ip: str, count: int) -> str:
    values = next_ips(start_ip, count)
    return compact_ipv4_range(values[0], values[-1])


def prompt_ip_assignments(
    role_label: str,
    count: int,
    existing_ips: list[str],
    suggested_start_ip: str,
) -> list[str]:
    if count == 0:
        return []

    existing_values = [str(value) for value in existing_ips if isinstance(value, str)]
    suggested_values = next_ips(suggested_start_ip, count)
    existing_matches_count = len(existing_values) == count
    default_mode = "range" if count > 1 and (not existing_values or (existing_matches_count and are_contiguous_ips(existing_values))) else "manual"
    mode = prompt_choice(f"{role_label} IP assignment mode", ["range", "manual"], default_mode)
    if mode == "range":
        default_range = (
            compact_ipv4_range(existing_values[0], existing_values[-1])
            if existing_matches_count and are_contiguous_ips(existing_values)
            else compact_ipv4_range(suggested_values[0], suggested_values[-1])
        )
        return prompt_ip_range(f"{role_label} IP range", default_range, count)

    if count == 1:
        default_ip = existing_values[0] if existing_matches_count else suggested_values[0]
        return [prompt_ip(f"{role_label} IP", default_ip)]
    default_values = existing_values if existing_matches_count else suggested_values
    return prompt_csv_ips_exact(f"{role_label} IPs (comma separated)", default_values, count)


def next_ips(start_ip: str, count: int) -> list[str]:
    start = ipaddress.ip_address(start_ip)
    return [str(start + offset) for offset in range(count)]


def next_available_vmids(used_vmids: set[int], count: int, start: int) -> list[int]:
    vmids: list[int] = []
    candidate = start
    while len(vmids) < count:
        if candidate not in used_vmids and candidate not in vmids:
            vmids.append(candidate)
        candidate += 1
    return vmids


def next_vmid_seed(used_vmids: set[int], minimum: int = 100) -> int:
    if not used_vmids:
        return minimum
    return max(minimum, max(used_vmids) + 1)


def shared_vlan_default(
    proxmox_existing: dict,
    existing_controlplane_vlans: list[int | None],
    existing_worker_vlans: list[int | None],
) -> tuple[bool, int | None, int | None]:
    proxmox_vlan = proxmox_existing.get("vlan_id")
    shared_value = int(proxmox_vlan) if isinstance(proxmox_vlan, int) else None

    cp_values = [value for value in existing_controlplane_vlans if value is not None]
    wk_values = [value for value in existing_worker_vlans if value is not None]
    cp_unique = set(existing_controlplane_vlans) if existing_controlplane_vlans else {None}
    wk_unique = set(existing_worker_vlans) if existing_worker_vlans else {None}

    use_shared = "vlan_id" in proxmox_existing or (len(cp_unique) <= 1 and len(wk_unique) <= 1 and cp_unique == wk_unique)
    if shared_value is None and len(cp_unique) == 1 and cp_unique == wk_unique:
        shared_value = next(iter(cp_unique))

    cp_default = shared_value if use_shared else (existing_controlplane_vlans[0] if existing_controlplane_vlans else None)
    wk_default = shared_value if use_shared else (existing_worker_vlans[0] if existing_worker_vlans else None)
    return use_shared, cp_default, wk_default


def default_api_vip(existing_api_vip: str | None, controlplane_ips: list[str], worker_ips: list[str]) -> str:
    if existing_api_vip:
        try:
            vip_ip = ipaddress.ip_address(existing_api_vip)
            planned_ips = [ipaddress.ip_address(value) for value in controlplane_ips + worker_ips]
            if not planned_ips or int(vip_ip) < min(int(value) for value in planned_ips):
                return str(vip_ip)
        except ValueError:
            pass

    if controlplane_ips:
        return controlplane_ips[0]

    return "192.168.1.20"


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "talosforge-configure/0.1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode().strip()


def fetch_json(url: str) -> object:
    return json.loads(fetch_text(url))


def discover_talos_version() -> str:
    try:
        data = fetch_json("https://api.github.com/repos/siderolabs/talos/releases/latest")
        tag = data.get("tag_name")
        if isinstance(tag, str) and tag.startswith("v"):
            return tag
    except Exception:
        pass
    return DEFAULT_TALOS_VERSION


def discover_kubernetes_version() -> str:
    try:
        stable = fetch_text("https://dl.k8s.io/release/stable.txt").lstrip("v")
        if stable:
            return stable
    except Exception:
        pass
    return DEFAULT_KUBERNETES_VERSION


def semver_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def discover_recent_talos_versions(limit: int = 3) -> list[str]:
    versions: list[str] = []
    try:
        releases = fetch_json("https://api.github.com/repos/siderolabs/talos/releases?per_page=12")
        if isinstance(releases, list):
            for release in releases:
                if not isinstance(release, dict) or release.get("prerelease"):
                    continue
                tag = release.get("tag_name")
                if isinstance(tag, str) and tag.startswith("v") and tag not in versions:
                    versions.append(tag)
                if len(versions) >= limit:
                    break
    except Exception:
        pass

    if not versions:
        versions.append(DEFAULT_TALOS_VERSION)

    return versions[:limit]


def discover_recent_kubernetes_versions(limit: int = 3) -> list[str]:
    versions: list[str] = []
    try:
        latest = discover_kubernetes_version()
        versions.append(latest)
        latest_parts = latest.split(".")
        if len(latest_parts) >= 2:
            major = int(latest_parts[0])
            minor = int(latest_parts[1])
            for offset in range(1, limit):
                if minor - offset < 0:
                    break
                candidate = fetch_text(f"https://dl.k8s.io/release/stable-{major}.{minor - offset}.txt").lstrip("v")
                if candidate and candidate not in versions:
                    versions.append(candidate)
    except Exception:
        pass

    if not versions:
        versions.append(DEFAULT_KUBERNETES_VERSION)

    return versions[:limit]


def discover_recent_cilium_versions(limit: int = 3) -> list[str]:
    versions: list[str] = []
    try:
        releases = fetch_json("https://api.github.com/repos/cilium/cilium/releases?per_page=12")
        if isinstance(releases, list):
            for release in releases:
                if not isinstance(release, dict) or release.get("prerelease"):
                    continue
                tag = release.get("tag_name")
                if isinstance(tag, str):
                    normalized = tag.lstrip("v")
                    if normalized not in versions:
                        versions.append(normalized)
                if len(versions) >= limit:
                    break
    except Exception:
        pass

    if not versions:
        versions.append(DEFAULT_CILIUM_CHART_VERSION)

    return versions[:limit]


def discover_recent_traefik_versions(limit: int = 3) -> list[str]:
    versions: list[str] = []
    try:
        releases = fetch_json("https://api.github.com/repos/traefik/traefik-helm-chart/releases?per_page=12")
        if isinstance(releases, list):
            for release in releases:
                if not isinstance(release, dict) or release.get("prerelease"):
                    continue
                tag = release.get("tag_name")
                if isinstance(tag, str):
                    normalized = tag.lstrip("v")
                    if normalized not in versions:
                        versions.append(normalized)
                if len(versions) >= limit:
                    break
    except Exception:
        pass

    if not versions:
        versions.append(DEFAULT_TRAEFIK_CHART_VERSION)

    return versions[:limit]


def discover_recent_proxmox_csi_versions(limit: int = 3) -> list[str]:
    # The OCI chart versions do not match the upstream application release tags.
    # Keep this pinned to the known-good chart train instead of surfacing invalid choices.
    return [DEFAULT_PROXMOX_CSI_CHART_VERSION][:limit]


def prompt_release_choice(text: str, versions: list[str], default: str) -> str:
    ordered: list[str] = []
    for version in versions:
        if version and version not in ordered:
            ordered.append(version)
    if default and default not in ordered:
        ordered.append(default)

    print_section(f"{text} options")
    for index, version in enumerate(ordered, start=1):
        labels: list[str] = []
        if index == 1:
            labels.append("recommended latest")
        if version == default:
            labels.append("current")
        rendered_version = colorize(version, ANSI_GREEN if index == 1 else ANSI_CYAN)
        rendered_labels = ", ".join(
            colorize(label, ANSI_GREEN if label == "recommended latest" else ANSI_DIM) for label in labels
        )
        suffix = f" ({rendered_labels})" if labels else ""
        print(f"  {colorize(str(index), ANSI_MAGENTA)}. {rendered_version}{suffix}")
    print(f"  {colorize('c', ANSI_MAGENTA)}. {colorize('Custom', ANSI_CYAN)}")

    default_choice = "c"
    if default in ordered:
        default_choice = str(ordered.index(default) + 1)

    while True:
        choice = prompt(f"{text} selection", default_choice).strip().lower()
        if choice in {"c", "custom"}:
            return prompt(text, default)
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(ordered):
                return ordered[index - 1]
        print(colorize("Choose one of the listed numbers or 'c' for custom.", ANSI_YELLOW))


def resolve_schematic_id(existing_value: object) -> str:
    if isinstance(existing_value, str):
        cleaned = existing_value.strip()
        if cleaned and cleaned != PLACEHOLDER_SCHEMATIC_ID:
            return cleaned
    return DEFAULT_VANILLA_SCHEMATIC_ID


def build_schematic_yaml(extensions: list[str]) -> str:
    if not extensions:
        return "{}\n"

    lines = [
        "customization:",
        "  systemExtensions:",
        "    officialExtensions:",
    ]
    lines.extend(f"      - {extension}" for extension in extensions)
    return "\n".join(lines) + "\n"


def generate_schematic_id(factory_base_url: str, extensions: list[str]) -> str:
    if not extensions:
        return DEFAULT_VANILLA_SCHEMATIC_ID

    base_url = factory_base_url.rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/schematics",
        data=build_schematic_yaml(extensions).encode(),
        headers={
            "Content-Type": "application/yaml",
            "User-Agent": "talosforge-configure/0.1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode())

    schematic_id = payload.get("id")
    if not isinstance(schematic_id, str) or not schematic_id:
        raise ValueError("Image Factory did not return a schematic ID.")
    return schematic_id


def default_schematic_id_for_extensions(extensions: list[str]) -> str | None:
    deduped = []
    for extension in extensions:
        if extension not in deduped:
            deduped.append(extension)
    if deduped == DEFAULT_PROXMOX_EXTENSIONS:
        return DEFAULT_PROXMOX_SCHEMATIC_ID
    if not deduped:
        return DEFAULT_VANILLA_SCHEMATIC_ID
    return None


def resolve_or_generate_schematic_id(factory_base_url: str, extensions: list[str]) -> str:
    try:
        return generate_schematic_id(factory_base_url, extensions)
    except Exception:
        fallback = default_schematic_id_for_extensions(extensions)
        if fallback is not None:
            return fallback
        raise


def load_existing(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def choose_storage(storages: list[dict], preferred: str | None, fallback: str) -> str:
    available = [entry["storage"] for entry in storages if entry.get("storage")]
    if preferred and preferred in available:
        return preferred
    if fallback in available:
        return fallback
    if available:
        return available[0]
    return fallback


def choose_node_defaults(existing: dict | None, proxmox_nodes: list[str]) -> tuple[str, str, str]:
    proxmox = (existing or {}).get("proxmox", {})
    node_default = str(proxmox.get("node_default", proxmox_nodes[0]))
    return (
        node_default,
        str(proxmox.get("network_bridge", "vmbr0")),
        str(proxmox.get("image_datastore", "local")),
    )


def discover_proxmox_cluster_name(client: ProxmoxClient) -> str | None:
    try:
        status = client.get("/cluster/status").get("data", [])
    except Exception:
        return None

    if not isinstance(status, list):
        return None

    for entry in status:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "cluster":
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def prompt_vmid_assignment(role_label: str, count: int, used_vmids: set[int], default_start_vmid: int) -> list[int]:
    if count == 0:
        return []

    mode = prompt_choice(f"{role_label} VM ID assignment mode", ["auto", "range", "manual"], "auto")
    if mode == "manual":
        values: list[int] = []
        for index in range(count):
            values.append(prompt_int(f"{role_label} node {index + 1} VM ID", default_start_vmid + index, minimum=100))
        return values
    if mode == "range":
        start_vmid = prompt_int(f"{role_label} starting VM ID", default_start_vmid, minimum=100)
        return list(range(start_vmid, start_vmid + count))
    return next_available_vmids(used_vmids, count, default_start_vmid)


def prompt_host_assignments(role_label: str, count: int, proxmox_nodes: list[str], existing: list[str] | None = None) -> list[str]:
    if count == 0:
        return []

    if len(proxmox_nodes) == 1:
        return [proxmox_nodes[0] for _ in range(count)]

    mode_default = "manual" if existing else "auto"
    mode = prompt_choice(f"{role_label} placement across Proxmox nodes", ["auto", "manual"], mode_default)

    if mode == "auto":
        if existing and len(existing) == count:
            return existing
        return [proxmox_nodes[index % len(proxmox_nodes)] for index in range(count)]

    values: list[str] = []
    for index in range(count):
        default = existing[index] if existing and len(existing) == count else proxmox_nodes[index % len(proxmox_nodes)]
        values.append(prompt_choice(f"{role_label} node {index + 1} host_node", proxmox_nodes, default))
    return values


def summarize_plan(payload: dict) -> None:
    print_section("Planned Talosforge nodes")
    for name, node in payload["nodes"].items():
        rendered_name = colorize(f"{name:<8}", ANSI_GREEN)
        rendered_role = colorize(f"{node['role']:<12}", ANSI_CYAN)
        rendered_vmid = colorize(f"vmid={node['vm_id']}", ANSI_MAGENTA)
        rendered_host = colorize(f"host={node['host_node']}", ANSI_YELLOW)
        print(
            f"  {rendered_name} "
            f"{rendered_role} "
            f"{node['ip']:<15} "
            f"{rendered_vmid} "
            f"{rendered_host} "
            f"cpu={node['cpu']} mem={node['memory_mb']}MB disk={node['disk_gb']}GB"
        )
    vip = payload["cluster"].get("api_vip")
    if vip:
        rendered_name = colorize(f"{'vip':<8}", ANSI_GREEN)
        rendered_role = colorize(f"{'endpoint':<12}", ANSI_CYAN)
        print(
            f"  {rendered_name} "
            f"{rendered_role} "
            f"{vip:<15}"
        )


def ensure_unique_vmids(vmids: list[int]) -> None:
    seen: set[int] = set()
    duplicates: list[int] = []
    for vmid in vmids:
        if vmid in seen and vmid not in duplicates:
            duplicates.append(vmid)
        seen.add(vmid)
    if duplicates:
        joined = ", ".join(str(value) for value in duplicates)
        raise ValueError(f"Duplicate VM ID(s) planned: {joined}")


def ensure_available_vmids(used_vmids: set[int], vmids: list[int]) -> None:
    conflicts = sorted(vmid for vmid in vmids if vmid in used_vmids)
    if conflicts:
        joined = ", ".join(str(value) for value in conflicts)
        raise ValueError(f"VM ID(s) already in use: {joined}")


def build_payload(
    *,
    cluster_name: str,
    gateway: str,
    prefix: int,
    dns_domain: str,
    nameservers: list[str],
    talos_version: str,
    kubernetes_version: str,
    api_vip: str | None,
    install_disk: str,
    network_interface: str,
    proxmox_endpoint: str,
    proxmox_username: str,
    proxmox_insecure: bool,
    proxmox_cluster_name: str | None,
    node_default: str,
    image_datastore: str,
    vm_datastore: str,
    initialization_datastore: str,
    network_bridge: str,
    proxmox_vlan_id: int | None,
    schematic_id: str,
    factory_base_url: str,
    image_extensions: list[str],
    controlplane_count: int,
    worker_count: int,
    controlplane_hosts: list[str],
    worker_hosts: list[str],
    controlplane_ips: list[str],
    worker_ips: list[str],
    controlplane_vlan_id: int | None,
    worker_vlan_id: int | None,
    controlplane_vmids: list[int],
    worker_vmids: list[int],
    controlplane_cpu: int,
    controlplane_memory_mb: int,
    controlplane_disk_gb: int,
    worker_cpu: int,
    worker_memory_mb: int,
    worker_disk_gb: int,
    cilium_enabled: bool,
    cilium_chart_version: str,
    load_balancer_ip_pools: list[str],
    cilium_lb_pool_name: str,
    cilium_l2_policy_name: str,
    traefik_enabled: bool,
    traefik_chart_version: str,
    proxmox_csi_enabled: bool,
    proxmox_csi_chart_version: str,
    proxmox_csi_storage: str,
) -> dict:
    nodes: dict[str, dict] = {}

    for index in range(controlplane_count):
        name = f"cp-{index + 1:02d}"
        nodes[name] = {
            "role": "controlplane",
            "host_node": controlplane_hosts[index],
            "vm_id": controlplane_vmids[index],
            "ip": controlplane_ips[index],
            "cpu": controlplane_cpu,
            "memory_mb": controlplane_memory_mb,
            "disk_gb": controlplane_disk_gb,
            "datastore_id": vm_datastore,
            "bridge": network_bridge,
            "tags": ["controlplane"],
        }
        if proxmox_vlan_id is None and controlplane_vlan_id is not None:
            nodes[name]["vlan_id"] = controlplane_vlan_id

    for index in range(worker_count):
        name = f"wk-{index + 1:02d}"
        nodes[name] = {
            "role": "worker",
            "host_node": worker_hosts[index],
            "vm_id": worker_vmids[index],
            "ip": worker_ips[index],
            "cpu": worker_cpu,
            "memory_mb": worker_memory_mb,
            "disk_gb": worker_disk_gb,
            "datastore_id": vm_datastore,
            "bridge": network_bridge,
            "tags": ["worker"],
        }
        if proxmox_vlan_id is None and worker_vlan_id is not None:
            nodes[name]["vlan_id"] = worker_vlan_id

    payload = {
        "proxmox": {
            "endpoint": proxmox_endpoint,
            "insecure": proxmox_insecure,
            "username": proxmox_username,
            "node_default": node_default,
            "image_datastore": image_datastore,
            "vm_datastore": vm_datastore,
            "initialization_datastore": initialization_datastore,
            "network_bridge": network_bridge,
            "tags": ["talosforge", "talos", cluster_name],
        },
        "cluster": {
            "name": cluster_name,
            "gateway": gateway,
            "prefix": prefix,
            "dns_domain": dns_domain,
            "nameservers": nameservers,
            "talos_version": talos_version,
            "kubernetes_version": kubernetes_version,
            "install_disk": install_disk,
            "network_interface": network_interface,
        },
        "image": {
            "schematic_id": schematic_id,
            "factory_base_url": factory_base_url,
            "arch": "amd64",
            "platform": "nocloud",
            "update_strategy": "recreate",
            "extensions": image_extensions,
        },
        "addons": {
            "cilium_enabled": cilium_enabled,
            "cilium_chart_version": cilium_chart_version,
            "load_balancer_ip_pools": load_balancer_ip_pools,
            "cilium_lb_pool_name": cilium_lb_pool_name,
            "cilium_l2_policy_name": cilium_l2_policy_name,
            "traefik_enabled": traefik_enabled,
            "traefik_chart_version": traefik_chart_version,
            "proxmox_csi_enabled": proxmox_csi_enabled,
            "proxmox_csi_chart_version": proxmox_csi_chart_version,
            "proxmox_csi_storage": proxmox_csi_storage,
        },
        "nodes": nodes,
    }

    if proxmox_cluster_name:
        payload["proxmox"]["cluster_name"] = proxmox_cluster_name

    if proxmox_vlan_id is not None:
        payload["proxmox"]["vlan_id"] = proxmox_vlan_id

    if api_vip:
        payload["cluster"]["api_vip"] = api_vip

    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive Talosforge configuration generator")
    parser.add_argument("--file", default="terraform.tfvars.json")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--non-interactive", action="store_true", help="Write a starter config without prompting.")
    args = parser.parse_args()

    path = Path(args.file)
    existing = load_existing(path)

    if args.non_interactive:
        from copy import deepcopy

        starter = deepcopy({
            "proxmox": {
                "endpoint": "https://proxmox.example.com:8006/api2/json",
                "insecure": True,
                "username": "terraform@pve",
                "cluster_name": "proxmox",
                "vlan_id": None,
                "node_default": "pve1",
                "image_datastore": "local",
                "vm_datastore": "local-zfs",
                "initialization_datastore": "local-zfs",
                "network_bridge": "vmbr0",
                "tags": ["talosforge", "talos"],
            },
            "cluster": {
                "name": "talosforge",
                "gateway": "192.168.1.1",
                "prefix": 24,
                "dns_domain": "cluster.local",
                "nameservers": ["192.168.1.1"],
                "talos_version": DEFAULT_TALOS_VERSION,
                "kubernetes_version": DEFAULT_KUBERNETES_VERSION,
                "api_vip": "192.168.1.20",
                "install_disk": DEFAULT_INSTALL_DISK,
                "network_interface": "eth0",
            },
            "image": {
                "schematic_id": resolve_or_generate_schematic_id(DEFAULT_FACTORY_BASE_URL, DEFAULT_PROXMOX_EXTENSIONS),
                "factory_base_url": DEFAULT_FACTORY_BASE_URL,
                "arch": "amd64",
                "platform": "nocloud",
                "update_strategy": "recreate",
                "extensions": DEFAULT_PROXMOX_EXTENSIONS,
            },
            "addons": {
                "cilium_enabled": True,
                "cilium_chart_version": DEFAULT_CILIUM_CHART_VERSION,
                "load_balancer_ip_pools": ["192.168.1.23-32"],
                "cilium_lb_pool_name": "default",
                "cilium_l2_policy_name": "default-l2",
                "traefik_enabled": True,
                "traefik_chart_version": DEFAULT_TRAEFIK_CHART_VERSION,
                "proxmox_csi_enabled": True,
                "proxmox_csi_chart_version": DEFAULT_PROXMOX_CSI_CHART_VERSION,
                "proxmox_csi_storage": "local-zfs",
            },
            "nodes": {
                "cp-01": {
                    "role": "controlplane",
                    "host_node": "pve1",
                    "vm_id": 800,
                    "ip": "192.168.1.21",
                    "cpu": 4,
                    "memory_mb": 8192,
                    "disk_gb": 40,
                    "datastore_id": "local-zfs",
                    "bridge": "vmbr0",
                    "tags": ["controlplane"],
                },
                "wk-01": {
                    "role": "worker",
                    "host_node": "pve1",
                    "vm_id": 810,
                    "ip": "192.168.1.31",
                    "cpu": 4,
                    "memory_mb": 8192,
                    "disk_gb": 80,
                    "datastore_id": "local-zfs",
                    "bridge": "vmbr0",
                    "tags": ["worker"],
                },
            },
        })
        if path.exists() and not args.force:
            print(f"{path} already exists. Re-run with --force to overwrite it.", file=sys.stderr)
            return 1
        path.write_text(json.dumps(starter, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote starter config to {path}")
        return 0

    print("Talosforge configurator\n")

    proxmox_existing = (existing or {}).get("proxmox", {})
    cluster_existing = (existing or {}).get("cluster", {})
    image_existing = (existing or {}).get("image", {})
    addons_existing = (existing or {}).get("addons", {})
    nodes_existing = (existing or {}).get("nodes", {})

    api_default = str(proxmox_existing.get("endpoint", "pve.local"))
    api_raw = prompt(
        "Proxmox hostname, IP, or API URL",
        api_default,
        show_default=True,
        default_label="pve.local",
    )
    try:
        proxmox_endpoint = normalize_proxmox_api_url(api_raw)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    proxmox_username = prompt("Proxmox username", str(proxmox_existing.get("username", "root@pam")))
    existing_password = os.environ.get("PROXMOX_PASSWORD") or os.environ.get("TF_VAR_proxmox_password")
    proxmox_password = prompt(
        "Proxmox password",
        existing_password if isinstance(existing_password, str) and existing_password else None,
        secret=True,
        show_default=False,
    )
    proxmox_insecure = prompt_bool("Skip Proxmox TLS verification", bool(proxmox_existing.get("insecure", True)))

    try:
        client = ProxmoxClient(
            api_url=proxmox_endpoint,
            username=proxmox_username,
            password=proxmox_password,
            insecure=proxmox_insecure,
        )
        client.login()
        proxmox_nodes_response = client.get("/nodes").get("data", [])
        vm_resources = client.get("/cluster/resources?type=vm").get("data", [])
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError) as exc:
        print(f"Unable to talk to Proxmox: {exc}", file=sys.stderr)
        return 1

    proxmox_cluster_name = discover_proxmox_cluster_name(client)
    proxmox_nodes = [entry["node"] for entry in proxmox_nodes_response if entry.get("node")]
    if not proxmox_nodes:
        print("No Proxmox nodes were returned by the API.", file=sys.stderr)
        return 1

    storages_by_node: dict[str, list[dict]] = {}
    for node_name in proxmox_nodes:
        try:
            storages_by_node[node_name] = client.get(f"/nodes/{node_name}/storage").get("data", [])
        except Exception:
            storages_by_node[node_name] = []

    used_vmids = {
        int(entry["vmid"])
        for entry in vm_resources
        if "vmid" in entry and str(entry.get("template", 0)) != "1"
    }

    print(f"Connected to Proxmox API: {proxmox_endpoint}")
    if proxmox_cluster_name:
        print(f"Detected Proxmox cluster: {proxmox_cluster_name}")
    print(f"Available Proxmox nodes: {', '.join(proxmox_nodes)}")

    node_default_default, bridge_default, image_store_default = choose_node_defaults(existing, proxmox_nodes)
    default_node = prompt_choice("Default Proxmox node for new VMs", proxmox_nodes, node_default_default)

    storages = storages_by_node.get(default_node, [])
    available_storages = [entry["storage"] for entry in storages if entry.get("storage")]
    if available_storages:
        print(f"Available storages on {default_node}: {', '.join(available_storages)}")

    image_datastore = prompt(
        "Image datastore",
        choose_storage(storages, proxmox_existing.get("image_datastore"), image_store_default),
    )
    vm_datastore = prompt(
        "VM datastore",
        choose_storage(storages, proxmox_existing.get("vm_datastore"), "local-zfs"),
    )
    initialization_datastore = prompt(
        "Initialization datastore",
        choose_storage(storages, proxmox_existing.get("initialization_datastore"), vm_datastore),
    )
    network_bridge = prompt("Network bridge", str(proxmox_existing.get("network_bridge", bridge_default)))
    existing_controlplane_vlans = [node.get("vlan_id") if isinstance(node, dict) else None for name, node in sorted(nodes_existing.items()) if isinstance(node, dict) and node.get("role") == "controlplane"]
    existing_worker_vlans = [node.get("vlan_id") if isinstance(node, dict) else None for name, node in sorted(nodes_existing.items()) if isinstance(node, dict) and node.get("role") == "worker"]
    use_shared_vlan, controlplane_vlan_id, worker_vlan_id = shared_vlan_default(
        proxmox_existing,
        existing_controlplane_vlans,
        existing_worker_vlans,
    )
    proxmox_vlan_id: int | None = None
    if prompt_bool("Use one VLAN ID for all Talos nodes", use_shared_vlan):
        shared_vlan_default_value = controlplane_vlan_id if use_shared_vlan else (proxmox_existing.get("vlan_id") if isinstance(proxmox_existing.get("vlan_id"), int) else None)
        shared_vlan_id = prompt_optional_int_with_choices(
            "Cluster VLAN ID (blank or none for untagged)",
            shared_vlan_default_value,
        )
        proxmox_vlan_id = shared_vlan_id
        controlplane_vlan_id = shared_vlan_id
        worker_vlan_id = shared_vlan_id
    else:
        controlplane_vlan_id = prompt_optional_int_with_choices(
            "Control plane VLAN ID (blank or none for untagged)",
            controlplane_vlan_id,
        )
        worker_vlan_id = prompt_optional_int_with_choices(
            "Worker VLAN ID (blank or none for untagged)",
            worker_vlan_id,
        )
    proxmox_cluster_name = str(proxmox_existing.get("cluster_name", proxmox_cluster_name or default_node))

    cluster_name = prompt("Cluster name", str(cluster_existing.get("name", "talosforge")))
    gateway = prompt_ip("Gateway IP", str(cluster_existing.get("gateway", "192.168.1.1")))
    prefix = prompt_int("Subnet prefix", int(cluster_existing.get("prefix", 24)), minimum=1)
    dns_domain = prompt("Cluster DNS domain", str(cluster_existing.get("dns_domain", "cluster.local")))
    nameservers = prompt_csv_ips("DNS servers (comma separated)", list(cluster_existing.get("nameservers", [gateway])))
    network_interface = prompt("Talos network interface", str(cluster_existing.get("network_interface", "eth0")))
    install_disk = str(cluster_existing.get("install_disk", DEFAULT_INSTALL_DISK))

    talos_default = str(cluster_existing.get("talos_version", discover_talos_version()))
    talos_version = prompt_release_choice("Talos version", discover_recent_talos_versions(), talos_default)

    kubernetes_default = str(cluster_existing.get("kubernetes_version", discover_kubernetes_version()))
    kubernetes_version = prompt_release_choice(
        "Kubernetes version",
        discover_recent_kubernetes_versions(),
        kubernetes_default,
    )

    factory_base_url = str(image_existing.get("factory_base_url", DEFAULT_FACTORY_BASE_URL))
    existing_extensions = image_existing.get("extensions")
    if isinstance(existing_extensions, list):
        extension_defaults = [str(item).strip() for item in existing_extensions if str(item).strip()]
    else:
        extension_defaults = list(DEFAULT_PROXMOX_EXTENSIONS)

    use_qemu_guest_agent = prompt_bool(
        "Enable qemu-guest-agent Image Factory extension",
        "siderolabs/qemu-guest-agent" in extension_defaults,
    )
    other_extension_defaults = [
        extension for extension in extension_defaults
        if extension != "siderolabs/qemu-guest-agent"
    ]
    additional_extensions = prompt_csv_values(
        "Additional Image Factory extensions (comma separated)",
        other_extension_defaults,
    )

    image_extensions: list[str] = []
    if use_qemu_guest_agent:
        image_extensions.append("siderolabs/qemu-guest-agent")
    image_extensions.extend(
        extension for extension in additional_extensions
        if extension not in image_extensions
    )

    existing_schematic = image_existing.get("schematic_id")
    keep_existing_custom_schematic = (
        isinstance(existing_schematic, str)
        and existing_schematic.strip()
        and existing_schematic not in {PLACEHOLDER_SCHEMATIC_ID, DEFAULT_VANILLA_SCHEMATIC_ID}
        and not isinstance(existing_extensions, list)
    )

    if keep_existing_custom_schematic:
        schematic_id = existing_schematic.strip()
        print(f"Talos Image Factory schematic: keeping existing custom schematic {schematic_id}.")
    else:
        try:
            schematic_id = resolve_or_generate_schematic_id(factory_base_url, image_extensions)
        except Exception as exc:
            print(f"Unable to generate Image Factory schematic: {exc}", file=sys.stderr)
            return 1
        if image_extensions:
            print(
                "Talos Image Factory schematic: generated "
                f"{schematic_id} for extensions {', '.join(image_extensions)}."
            )
        else:
            print("Talos Image Factory schematic: using built-in vanilla schematic.")

    controlplane_count = prompt_int("Control plane node count", len([n for n in nodes_existing.values() if n.get("role") == "controlplane"]) or 1, minimum=1)
    worker_count = prompt_int("Worker node count", len([n for n in nodes_existing.values() if n.get("role") == "worker"]) or 1, minimum=0)

    existing_controlplane_ips = [str(node.get("ip")) for name, node in sorted(nodes_existing.items()) if node.get("role") == "controlplane" and node.get("ip")]
    existing_worker_ips = [str(node.get("ip")) for name, node in sorted(nodes_existing.items()) if node.get("role") == "worker" and node.get("ip")]

    api_vip = None
    if controlplane_count > 1:
        vip_default = default_api_vip(
            str(cluster_existing.get("api_vip")) if cluster_existing.get("api_vip") else None,
            existing_controlplane_ips,
            existing_worker_ips,
        )
        api_vip = prompt_ip("Talos VIP for Kubernetes API", vip_default)

    if existing_controlplane_ips and len(existing_controlplane_ips) == controlplane_count:
        controlplane_suggested_start_ip = existing_controlplane_ips[0]
    elif api_vip:
        controlplane_suggested_start_ip = str(ipaddress.ip_address(api_vip) + 1)
    else:
        controlplane_suggested_start_ip = "192.168.1.20"

    controlplane_default_ips = list(existing_controlplane_ips)
    if api_vip:
        expected_controlplane_start_ip = str(ipaddress.ip_address(api_vip) + 1)
        if (
            len(controlplane_default_ips) != controlplane_count
            or not are_contiguous_ips(controlplane_default_ips)
            or controlplane_default_ips[0] != expected_controlplane_start_ip
            or api_vip in controlplane_default_ips
        ):
            controlplane_default_ips = []
        controlplane_suggested_start_ip = expected_controlplane_start_ip

    controlplane_ips = prompt_ip_assignments(
        "Control plane",
        controlplane_count,
        controlplane_default_ips,
        controlplane_suggested_start_ip,
    )

    if controlplane_ips:
        worker_suggested_start_ip = str(ipaddress.ip_address(controlplane_ips[-1]) + 1)
    elif existing_worker_ips and len(existing_worker_ips) == worker_count:
        worker_suggested_start_ip = existing_worker_ips[0]
    elif api_vip:
        worker_suggested_start_ip = str(ipaddress.ip_address(api_vip) + controlplane_count + 1)
    else:
        worker_suggested_start_ip = str(ipaddress.ip_address(controlplane_suggested_start_ip) + controlplane_count)

    worker_default_ips = list(existing_worker_ips)
    expected_worker_start_ip = worker_suggested_start_ip
    reserved_ips = set(controlplane_ips)
    if api_vip:
        reserved_ips.add(api_vip)
    if (
        len(worker_default_ips) != worker_count
        or not are_contiguous_ips(worker_default_ips)
        or (worker_default_ips and worker_default_ips[0] != expected_worker_start_ip)
        or any(ip in reserved_ips for ip in worker_default_ips)
    ):
        worker_default_ips = []

    worker_ips = prompt_ip_assignments(
        "Worker",
        worker_count,
        worker_default_ips,
        worker_suggested_start_ip,
    )

    existing_cp_hosts = [node.get("host_node") for name, node in sorted(nodes_existing.items()) if node.get("role") == "controlplane"]
    existing_wk_hosts = [node.get("host_node") for name, node in sorted(nodes_existing.items()) if node.get("role") == "worker"]
    controlplane_hosts = prompt_host_assignments("Control plane", controlplane_count, proxmox_nodes, existing_cp_hosts)
    worker_hosts = prompt_host_assignments("Worker", worker_count, proxmox_nodes, existing_wk_hosts)

    controlplane_cpu = prompt_int("Control plane CPU cores", 4, minimum=1)
    controlplane_memory_mb = prompt_int("Control plane memory (MB)", 8192, minimum=1024)
    controlplane_disk_gb = prompt_int("Control plane disk (GB)", 40, minimum=8)
    worker_cpu = prompt_int("Worker CPU cores", 4, minimum=1)
    worker_memory_mb = prompt_int("Worker memory (MB)", 8192, minimum=1024)
    worker_disk_gb = prompt_int("Worker disk (GB)", 80, minimum=8)

    next_seed = next_vmid_seed(used_vmids)
    controlplane_vmids = prompt_vmid_assignment("Control plane", controlplane_count, used_vmids, next_seed)
    reserved_vmids = used_vmids | set(controlplane_vmids)
    worker_vmids = prompt_vmid_assignment("Worker", worker_count, reserved_vmids, next_vmid_seed(reserved_vmids))

    cilium_enabled = prompt_bool("Install Cilium during bootstrap", bool(addons_existing.get("cilium_enabled", True)))
    cilium_default = str(addons_existing.get("cilium_chart_version", DEFAULT_CILIUM_CHART_VERSION))
    cilium_chart_version = cilium_default
    if cilium_enabled:
        cilium_chart_version = prompt_release_choice(
            "Cilium chart version",
            discover_recent_cilium_versions(),
            cilium_default,
        )

    load_balancer_ip_pools = [
        normalize_load_balancer_pool(str(item).strip())
        for item in addons_existing.get("load_balancer_ip_pools", addons_existing.get("metallb_pools", []))
        if str(item).strip()
    ]
    existing_lb_pool_name = str(addons_existing.get("cilium_lb_pool_name", "default"))
    existing_l2_policy_name = str(addons_existing.get("cilium_l2_policy_name", f"{existing_lb_pool_name}-l2"))
    cilium_lb_pool_name = existing_lb_pool_name
    cilium_l2_policy_name = existing_l2_policy_name
    if cilium_enabled:
        last_cluster_ip = None
        if worker_ips:
            last_cluster_ip = worker_ips[-1]
        elif controlplane_ips:
            last_cluster_ip = controlplane_ips[-1]
        elif api_vip:
            last_cluster_ip = api_vip

        suggested_lb_range = "192.168.1.240-249"
        suggested_lb_start_ip = None
        if last_cluster_ip:
            suggested_lb_start_ip = str(ipaddress.ip_address(last_cluster_ip) + 1)
            suggested_lb_range = suggest_range(suggested_lb_start_ip, 10)

        default_pool_mode = "range" if not load_balancer_ip_pools or (len(load_balancer_ip_pools) == 1 and "-" in load_balancer_ip_pools[0]) else "manual"
        load_balancer_pool_mode = prompt_choice("Cilium LoadBalancer IP pool assignment mode", ["range", "manual"], default_pool_mode)
        if load_balancer_pool_mode == "range":
            range_default = suggested_lb_range
            if len(load_balancer_ip_pools) == 1 and "-" in load_balancer_ip_pools[0]:
                existing_range = load_balancer_ip_pools[0]
                if suggested_lb_start_ip is None:
                    range_default = existing_range
                else:
                    try:
                        existing_range_start = expand_ipv4_range(existing_range)[0]
                    except ValueError:
                        existing_range_start = None
                    if existing_range_start == suggested_lb_start_ip:
                        range_default = existing_range
            load_balancer_ip_pools = [normalize_load_balancer_pool(prompt("Cilium LoadBalancer IP pool range", range_default))]
        else:
            manual_defaults = load_balancer_ip_pools or [suggested_lb_range]
            if suggested_lb_start_ip and load_balancer_ip_pools:
                try:
                    existing_manual_start = expand_ipv4_range(load_balancer_ip_pools[0])[0]
                except ValueError:
                    existing_manual_start = None
                if existing_manual_start != suggested_lb_start_ip:
                    manual_defaults = [suggested_lb_range]
            load_balancer_ip_pools = prompt_load_balancer_pools(
                "Cilium LoadBalancer IP pool(s), comma separated CIDR/range",
                manual_defaults,
            )

        cilium_lb_pool_name = prompt_hostname_label("Cilium LoadBalancer pool name", cilium_lb_pool_name or "default")
        l2_default = existing_l2_policy_name or f"{cilium_lb_pool_name}-l2"
        if (
            l2_default in {"default", "default-l2"}
            or l2_default == f"{existing_lb_pool_name}-l2"
            or not HOSTNAME_RE.fullmatch(l2_default)
        ):
            l2_default = f"{cilium_lb_pool_name}-l2"
        cilium_l2_policy_name = prompt_hostname_label("Cilium L2 announcement policy name", l2_default)
    else:
        load_balancer_ip_pools = []
        cilium_lb_pool_name = "default"
        cilium_l2_policy_name = "default-l2"

    traefik_enabled = prompt_bool("Install Traefik during bootstrap", bool(addons_existing.get("traefik_enabled", True)))
    traefik_default = str(addons_existing.get("traefik_chart_version", DEFAULT_TRAEFIK_CHART_VERSION))
    traefik_chart_version = traefik_default
    if traefik_enabled:
        traefik_chart_version = prompt_release_choice(
            "Traefik chart version",
            discover_recent_traefik_versions(),
            traefik_default,
        )

    proxmox_csi_enabled = prompt_bool("Install Proxmox CSI during bootstrap", bool(addons_existing.get("proxmox_csi_enabled", True)))
    proxmox_csi_default = str(addons_existing.get("proxmox_csi_chart_version", DEFAULT_PROXMOX_CSI_CHART_VERSION))
    if proxmox_csi_default in {"0.18.0", "0.18.1"}:
        proxmox_csi_default = DEFAULT_PROXMOX_CSI_CHART_VERSION
    proxmox_csi_chart_version = proxmox_csi_default
    proxmox_csi_storage = str(addons_existing.get("proxmox_csi_storage", proxmox_existing.get("vm_datastore", vm_datastore)))
    if proxmox_csi_enabled:
        proxmox_csi_chart_version = prompt_release_choice(
            "Proxmox CSI chart version",
            discover_recent_proxmox_csi_versions(),
            proxmox_csi_default,
        )
        proxmox_csi_storage = prompt("Proxmox CSI storage backend", proxmox_csi_storage)

    all_planned_vmids = controlplane_vmids + worker_vmids
    try:
        ensure_unique_vmids(all_planned_vmids)
        ensure_available_vmids(used_vmids, all_planned_vmids)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = build_payload(
        cluster_name=cluster_name,
        gateway=gateway,
        prefix=prefix,
        dns_domain=dns_domain,
        nameservers=nameservers,
        talos_version=talos_version,
        kubernetes_version=kubernetes_version,
        api_vip=api_vip,
        install_disk=install_disk,
        network_interface=network_interface,
        proxmox_endpoint=proxmox_endpoint,
        proxmox_username=proxmox_username,
        proxmox_insecure=proxmox_insecure,
        proxmox_cluster_name=proxmox_cluster_name,
        node_default=default_node,
        image_datastore=image_datastore,
        vm_datastore=vm_datastore,
        initialization_datastore=initialization_datastore,
        network_bridge=network_bridge,
        proxmox_vlan_id=proxmox_vlan_id,
        schematic_id=schematic_id,
        factory_base_url=factory_base_url,
        image_extensions=image_extensions,
        controlplane_count=controlplane_count,
        worker_count=worker_count,
        controlplane_hosts=controlplane_hosts,
        worker_hosts=worker_hosts,
        controlplane_ips=controlplane_ips,
        worker_ips=worker_ips,
        controlplane_vlan_id=controlplane_vlan_id,
        worker_vlan_id=worker_vlan_id,
        controlplane_vmids=controlplane_vmids,
        worker_vmids=worker_vmids,
        controlplane_cpu=controlplane_cpu,
        controlplane_memory_mb=controlplane_memory_mb,
        controlplane_disk_gb=controlplane_disk_gb,
        worker_cpu=worker_cpu,
        worker_memory_mb=worker_memory_mb,
        worker_disk_gb=worker_disk_gb,
        cilium_enabled=cilium_enabled,
        cilium_chart_version=cilium_chart_version,
        load_balancer_ip_pools=load_balancer_ip_pools,
        cilium_lb_pool_name=cilium_lb_pool_name,
        cilium_l2_policy_name=cilium_l2_policy_name,
        traefik_enabled=traefik_enabled,
        traefik_chart_version=traefik_chart_version,
        proxmox_csi_enabled=proxmox_csi_enabled,
        proxmox_csi_chart_version=proxmox_csi_chart_version,
        proxmox_csi_storage=proxmox_csi_storage,
    )

    summarize_plan(payload)

    if path.exists() and not args.force:
        if not prompt_bool(f"{path} already exists. Overwrite it", False):
            print("Configure cancelled.")
            return 1

    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote configuration to {path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
