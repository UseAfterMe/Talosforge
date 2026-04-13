#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-help}"
shift || true

TFVARS_FILE="${TFVARS_FILE:-terraform.tfvars.json}"
OUT_DIR="${OUT_DIR:-out}"
KUBECONFIG_PATH="${OUT_DIR}/kubeconfig"
TALOSCONFIG_PATH="${OUT_DIR}/talosconfig"
METADATA_PATH="${OUT_DIR}/cluster-metadata.json"
DEPLOYMENT_HISTORY_DIR="${OUT_DIR}/deployment-history"
BOOTSTRAP_TIMEOUT_SECS="${BOOTSTRAP_TIMEOUT_SECS:-900}"
BOOTSTRAP_API_HEARTBEATS="${BOOTSTRAP_API_HEARTBEATS:-3}"
BOOTSTRAP_API_HEARTBEAT_INTERVAL_SECS="${BOOTSTRAP_API_HEARTBEAT_INTERVAL_SECS:-5}"
SKIP_PROXMOX_PREFLIGHT="${SKIP_PROXMOX_PREFLIGHT:-false}"
PROXMOX_KEYCHAIN_SERVICE="${PROXMOX_KEYCHAIN_SERVICE:-talosforge-proxmox}"
COLOR_RESET=""
COLOR_CYAN=""
COLOR_BLUE=""
COLOR_GREEN=""
COLOR_YELLOW=""
COLOR_RED=""
COLOR_DIM=""
COLOR_BOLD=""

handle_interrupt() {
  printf '\n' >&2
  log_warn "Interrupted."
  log_warn "No further Talosforge actions will be taken."
  exit 130
}

init_colors() {
  if [[ -t 1 && "${TERM:-}" != "dumb" ]]; then
    COLOR_RESET=$'\033[0m'
    COLOR_CYAN=$'\033[36m'
    COLOR_BLUE=$'\033[34m'
    COLOR_GREEN=$'\033[32m'
    COLOR_YELLOW=$'\033[33m'
    COLOR_RED=$'\033[31m'
    COLOR_DIM=$'\033[2m'
    COLOR_BOLD=$'\033[1m'
  fi
}

log_step() {
  printf '%s==>%s %s\n' "${COLOR_CYAN}${COLOR_BOLD}" "${COLOR_RESET}" "$*"
}

log_success() {
  printf '%s%s%s\n' "${COLOR_GREEN}${COLOR_BOLD}" "$*" "${COLOR_RESET}"
}

log_info() {
  printf '%s%s%s\n' "${COLOR_BLUE}" "$*" "${COLOR_RESET}"
}

log_warn() {
  printf '%s%s%s\n' "${COLOR_YELLOW}${COLOR_BOLD}" "$*" "${COLOR_RESET}" >&2
}

log_error() {
  printf '%s%s%s\n' "${COLOR_RED}${COLOR_BOLD}" "$*" "${COLOR_RESET}" >&2
}

log_item() {
  printf '  %s-%s %s\n' "${COLOR_DIM}" "${COLOR_RESET}" "$*"
}

log_next_step() {
  printf '  %s$%s %s\n' "${COLOR_GREEN}${COLOR_BOLD}" "${COLOR_RESET}" "$*"
}

init_colors
trap handle_interrupt INT TERM

usage() {
  printf '%sTalosforge Deploy Script%s\n' "${COLOR_BOLD}" "${COLOR_RESET}"
  printf '%sUsage:%s ./deploy.sh [preflight|configure|apply|bootstrap|health|destroy|install-kubeconfig|install-talosconfig]\n' "${COLOR_CYAN}" "${COLOR_RESET}"
  printf '\n'
  printf '%sCommands:%s\n' "${COLOR_CYAN}" "${COLOR_RESET}"
  log_item "preflight            Check and optionally install required tools for the Talosforge workflow"
  log_item "configure            Write a starter terraform.tfvars.json"
  log_item "apply                Validate config, preflight VMIDs, render Talos artifacts, and apply Terraform"
  log_item "bootstrap            Apply Talos configs, bootstrap etcd, export kubeconfig, and merge local configs safely"
  log_item "health               Check Talos and Kubernetes health"
  log_item "destroy              Destroy one tracked Talosforge cluster and prune only its local config entries"
  log_item "install-kubeconfig   Merge out/kubeconfig into ~/.kube/config"
  log_item "install-talosconfig  Merge out/talosconfig into ~/.talos/config"
}

platform_id() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    printf '%s\n' "macos"
    return
  fi

  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    if [[ "${ID_LIKE:-}" == *debian* || "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]]; then
      printf '%s\n' "apt"
      return
    fi
    if [[ "${ID_LIKE:-}" == *rhel* || "${ID_LIKE:-}" == *fedora* || "${ID:-}" == "rocky" || "${ID:-}" == "rhel" || "${ID:-}" == "fedora" ]]; then
      printf '%s\n' "dnf"
      return
    fi
  fi

  printf '%s\n' "unknown"
}

machine_arch() {
  case "$(uname -m)" in
    x86_64|amd64)
      printf '%s\n' "amd64"
      ;;
    arm64|aarch64)
      printf '%s\n' "arm64"
      ;;
    *)
      log_error "Unsupported machine architecture: $(uname -m)"
      exit 1
      ;;
  esac
}

ensure_local_bin_dir() {
  mkdir -p "${HOME}/.local/bin"
}

ensure_brew_available() {
  if command -v brew >/dev/null 2>&1; then
    return 0
  fi

  log_error "Homebrew is required for automatic installs on macOS."
  log_warn "Install it from: https://brew.sh"
  return 1
}

ensure_curl_available() {
  if command -v curl >/dev/null 2>&1; then
    return 0
  fi

  case "$(platform_id)" in
    apt)
      sudo apt-get update
      sudo apt-get install -y curl
      ;;
    dnf)
      sudo dnf install -y curl
      ;;
    *)
      log_error "curl is required to install this command automatically."
      return 1
      ;;
  esac
}

print_local_bin_path_hint_if_needed() {
  if [[ ":${PATH}:" != *":${HOME}/.local/bin:"* ]]; then
    log_warn "Note: ${HOME}/.local/bin is not in PATH yet."
    log_warn "Add this to your shell profile and open a new shell:"
    log_warn "  export PATH=\"${HOME}/.local/bin:\$PATH\""
  fi
}

install_linux_local_kubectl() {
  local arch tmp_dir stable_version
  arch="$(machine_arch)"
  ensure_curl_available
  ensure_local_bin_dir
  tmp_dir="$(mktemp -d)"
  stable_version="$(curl -L -s https://dl.k8s.io/release/stable.txt)"
  curl -fsSLo "${tmp_dir}/kubectl" "https://dl.k8s.io/release/${stable_version}/bin/linux/${arch}/kubectl"
  chmod +x "${tmp_dir}/kubectl"
  mv "${tmp_dir}/kubectl" "${HOME}/.local/bin/kubectl"
  rm -rf "${tmp_dir}"
  print_local_bin_path_hint_if_needed
}

install_linux_local_talosctl() {
  ensure_curl_available
  ensure_local_bin_dir
  curl -sL https://talos.dev/install | sh

  if command -v talosctl >/dev/null 2>&1; then
    return 0
  fi

  if [[ -f "./talosctl" ]]; then
    mv "./talosctl" "${HOME}/.local/bin/talosctl"
    chmod +x "${HOME}/.local/bin/talosctl"
  elif [[ -f "./bin/talosctl" ]]; then
    mv "./bin/talosctl" "${HOME}/.local/bin/talosctl"
    chmod +x "${HOME}/.local/bin/talosctl"
    rmdir "./bin" 2>/dev/null || true
  fi

  print_local_bin_path_hint_if_needed
}

install_linux_local_tofu() {
  local tmp_dir
  ensure_curl_available
  ensure_local_bin_dir
  tmp_dir="$(mktemp -d)"
  curl --proto '=https' --tlsv1.2 -fsSL https://get.opentofu.org/install-opentofu.sh -o "${tmp_dir}/install-opentofu.sh"
  chmod +x "${tmp_dir}/install-opentofu.sh"
  "${tmp_dir}/install-opentofu.sh" --install-method standalone --install-path "${HOME}/.local/bin" --symlink-path - --skip-verify

  if command -v tofu >/dev/null 2>&1; then
    rm -rf "${tmp_dir}"
    return 0
  fi

  rm -rf "${tmp_dir}"
  print_local_bin_path_hint_if_needed
}

prompt_yes_no() {
  local question="$1"
  local default="${2:-false}"
  local label="y/N"

  if [[ "${default}" == "true" ]]; then
    label="Y/n"
  fi

  while true; do
    local reply
    read -r -p "${question} [${label}]: " reply
    reply="$(printf '%s' "${reply}" | tr '[:upper:]' '[:lower:]')"
    if [[ -z "${reply}" ]]; then
      [[ "${default}" == "true" ]]
      return
    fi
    case "${reply}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
    esac
    log_warn "Enter yes or no."
  done
}

install_hint() {
  local command_name="$1"
  local platform
  platform="$(platform_id)"

  case "${platform}:${command_name}" in
    macos:tofu)
      printf '%s\n' "Install it with: brew install opentofu"
      ;;
    macos:talosctl)
      printf '%s\n' "Install it with: brew install siderolabs/tap/talosctl"
      ;;
    macos:kubectl)
      printf '%s\n' "Install it with: brew install kubectl"
      ;;
    macos:helm)
      printf '%s\n' "Install it with: brew install helm"
      ;;
    macos:jq)
      printf '%s\n' "Install it with: brew install jq"
      ;;
    macos:python3)
      printf '%s\n' "Install it with: brew install python"
      ;;
    apt:python3)
      printf '%s\n' "Install it with: sudo apt install -y python3"
      ;;
    apt:jq)
      printf '%s\n' "Install it with: sudo apt install -y jq"
      ;;
    apt:helm)
      printf '%s\n' "Install it with the official Helm instructions: https://helm.sh/docs/intro/install/"
      ;;
    apt:kubectl)
      printf '%s\n' "Install it with the official kubectl instructions: https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/"
      ;;
    apt:tofu)
      printf '%s\n' "Install it with the official OpenTofu instructions: https://opentofu.org/docs/intro/install/"
      ;;
    apt:talosctl)
      printf '%s\n' "Install talosctl with the official installer: curl -sL https://talos.dev/install | sh"
      ;;
    dnf:python3)
      printf '%s\n' "Install it with: sudo dnf install -y python3"
      ;;
    dnf:jq)
      printf '%s\n' "Install it with: sudo dnf install -y jq"
      ;;
    dnf:helm)
      printf '%s\n' "Install it with the official Helm instructions: https://helm.sh/docs/intro/install/"
      ;;
    dnf:kubectl)
      printf '%s\n' "Install it with the official kubectl instructions: https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/"
      ;;
    dnf:tofu)
      printf '%s\n' "Install it with the official OpenTofu instructions: https://opentofu.org/docs/intro/install/"
      ;;
    dnf:talosctl)
      printf '%s\n' "Install talosctl with the official installer: curl -sL https://talos.dev/install | sh"
      ;;
  esac
}

install_required_command() {
  local command_name="$1"
  local platform
  platform="$(platform_id)"

  if [[ "${platform}" == "macos" ]]; then
    ensure_brew_available || return 1
  fi

  case "${platform}:${command_name}" in
    macos:tofu)
      brew install opentofu
      ;;
    macos:talosctl)
      brew install siderolabs/tap/talosctl
      ;;
    macos:kubectl)
      brew install kubectl
      ;;
    macos:helm)
      brew install helm
      ;;
    macos:jq)
      brew install jq
      ;;
    macos:python3)
      brew install python
      ;;
    apt:python3)
      sudo apt-get update
      sudo apt-get install -y python3
      ;;
    apt:jq)
      sudo apt-get update
      sudo apt-get install -y jq
      ;;
    apt:helm)
      sudo apt-get install curl gpg apt-transport-https --yes
      curl -fsSL https://packages.buildkite.com/helm-linux/helm-debian/gpgkey | gpg --dearmor | sudo tee /usr/share/keyrings/helm.gpg >/dev/null
      echo "deb [signed-by=/usr/share/keyrings/helm.gpg] https://packages.buildkite.com/helm-linux/helm-debian/any/ any main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list >/dev/null
      sudo apt-get update
      sudo apt-get install -y helm
      ;;
    apt:kubectl)
      install_linux_local_kubectl
      ;;
    apt:tofu)
      install_linux_local_tofu
      ;;
    apt:talosctl)
      install_linux_local_talosctl
      ;;
    dnf:python3)
      sudo dnf install -y python3
      ;;
    dnf:jq)
      sudo dnf install -y jq
      ;;
    dnf:helm)
      sudo dnf install -y helm
      ;;
    dnf:kubectl)
      install_linux_local_kubectl
      ;;
    dnf:tofu)
      install_linux_local_tofu
      ;;
    dnf:talosctl)
      install_linux_local_talosctl
      ;;
    *)
      return 1
      ;;
  esac
}

prompt_install_required_command() {
  local command_name="$1"

  if [[ ! -t 0 ]]; then
    return 1
  fi

  if ! prompt_yes_no "Install missing required command '${command_name}' now?" true; then
    return 1
  fi

  install_required_command "${command_name}"
}

check_required_command() {
  local command_name="$1"

  if command -v "${command_name}" >/dev/null 2>&1; then
    return 0
  fi

  log_error "Missing required command: ${command_name}"

  if prompt_install_required_command "${command_name}" && command -v "${command_name}" >/dev/null 2>&1; then
    return 0
  fi

  local hint
  hint="$(install_hint "${command_name}" || true)"
  if [[ -n "${hint}" ]]; then
    log_info "${hint}"
  fi
  return 1
}

need() {
  check_required_command "$1" || exit 1
}

need_all() {
  local failed=0
  local command_name

  for command_name in "$@"; do
    if ! check_required_command "${command_name}"; then
      failed=1
    fi
  done

  return "${failed}"
}

ensure_tfvars() {
  if [[ ! -f "${TFVARS_FILE}" ]]; then
    log_error "Missing ${TFVARS_FILE}. Run ./deploy.sh configure first."
    exit 1
  fi
}

ensure_dirs() {
  mkdir -p "${OUT_DIR}" "${DEPLOYMENT_HISTORY_DIR}"
}

read_tfvar() {
  local key="$1"
  python3 - "${TFVARS_FILE}" "${key}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8"))
value = data
for part in key.split("."):
    if not isinstance(value, dict) or part not in value:
        print("")
        raise SystemExit(0)
    value = value[part]
if value is None:
    print("")
elif isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

read_tfvar_from_file() {
  local path="$1"
  local key="$2"
  python3 - "${path}" "${key}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8"))
value = data
for part in key.split("."):
    if not isinstance(value, dict) or part not in value:
        print("")
        raise SystemExit(0)
    value = value[part]
if value is None:
    print("")
elif isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

can_use_keychain() {
  [[ "$(uname -s)" == "Darwin" ]] && command -v security >/dev/null 2>&1
}

proxmox_keychain_account_from_file() {
  local tfvars_path="$1"
  local endpoint username host
  endpoint="$(read_tfvar_from_file "${tfvars_path}" "proxmox.endpoint")"
  username="$(read_tfvar_from_file "${tfvars_path}" "proxmox.username")"
  host="$(python3 - "${endpoint}" <<'PY'
import sys
import urllib.parse

parsed = urllib.parse.urlparse(sys.argv[1])
print(parsed.hostname or "")
PY
)"
  printf '%s\n' "${username}@${host}"
}

load_password_from_keychain() {
  local account="$1"
  security find-generic-password \
    -s "${PROXMOX_KEYCHAIN_SERVICE}" \
    -a "${account}" \
    -w 2>/dev/null || true
}

save_password_to_keychain() {
  local account="$1"
  security add-generic-password \
    -U \
    -s "${PROXMOX_KEYCHAIN_SERVICE}" \
    -a "${account}" \
    -w "${PROXMOX_PASSWORD}" >/dev/null
}

load_proxmox_password_from_tfvars() {
  local tfvars_path="$1"
  local account

  if [[ -n "${PROXMOX_PASSWORD:-}" ]]; then
    export TF_VAR_proxmox_password="${PROXMOX_PASSWORD}"
    return 0
  fi

  if can_use_keychain; then
    account="$(proxmox_keychain_account_from_file "${tfvars_path}")"
    PROXMOX_PASSWORD="$(load_password_from_keychain "${account}")"
  fi

  if [[ -z "${PROXMOX_PASSWORD:-}" ]]; then
    if [[ ! -t 0 ]]; then
      log_error "Missing PROXMOX_PASSWORD and no interactive terminal is available."
      exit 1
    fi

    read -r -s -p "Proxmox password: " PROXMOX_PASSWORD
    echo
    export PROXMOX_PASSWORD

    if can_use_keychain; then
      if prompt_yes_no "Save the Proxmox password in macOS Keychain for reuse?" true; then
        account="${account:-$(proxmox_keychain_account_from_file "${tfvars_path}")}"
        save_password_to_keychain "${account}"
      fi
    fi
  fi

  export PROXMOX_PASSWORD
  export TF_VAR_proxmox_password="${PROXMOX_PASSWORD}"
}

is_cilium_enabled() {
  local raw
  raw="$(read_tfvar "addons.cilium_enabled")"
  [[ -z "${raw}" || "${raw}" == "true" ]]
}

is_metallb_enabled() {
  local raw
  raw="$(read_tfvar "addons.metallb_enabled")"
  [[ "${raw}" == "true" ]]
}

is_traefik_enabled() {
  local raw
  raw="$(read_tfvar "addons.traefik_enabled")"
  [[ "${raw}" == "true" ]]
}

is_proxmox_csi_enabled() {
  local raw
  raw="$(read_tfvar "addons.proxmox_csi_enabled")"
  [[ "${raw}" == "true" ]]
}

uses_helm_addons() {
  if [[ ! -f "${TFVARS_FILE}" ]]; then
    return 0
  fi

  is_cilium_enabled || is_metallb_enabled || is_traefik_enabled || is_proxmox_csi_enabled
}

metadata_get() {
  local key="$1"
  python3 - "${METADATA_PATH}" "${key}" <<'PY'
import json
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
key = sys.argv[2]
data = json.loads(metadata_path.read_text(encoding="utf-8"))
value = data
for part in key.split("."):
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (list, dict)):
    print(json.dumps(value))
else:
    print(value)
PY
}

metadata_csv() {
  local key="$1"
  python3 - "${METADATA_PATH}" "${key}" <<'PY'
import json
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
key = sys.argv[2]
data = json.loads(metadata_path.read_text(encoding="utf-8"))
value = data
for part in key.split("."):
    value = value[part]

if isinstance(value, list):
    print(",".join(str(item) for item in value))
else:
    print(value)
PY
}

sanitize_workspace_name() {
  python3 - "$1" <<'PY'
import re
import sys
value = sys.argv[1].strip().lower()
value = re.sub(r"[^a-z0-9_-]+", "-", value).strip("-")
print(value or "default")
PY
}

current_workspace_name() {
  local cluster_name
  cluster_name="$(read_tfvar "cluster.name")"
  if [[ -z "${cluster_name}" ]]; then
    echo "Unable to determine cluster.name from ${TFVARS_FILE}." >&2
    exit 1
  fi
  sanitize_workspace_name "${cluster_name}"
}

workspace_history_dir() {
  local workspace="$1"
  printf '%s/%s\n' "${DEPLOYMENT_HISTORY_DIR}" "${workspace}"
}

workspace_last_applied_tfvars_path() {
  local workspace="$1"
  printf '%s/last-applied.tfvars.json\n' "$(workspace_history_dir "${workspace}")"
}

workspace_kubeconfig_path() {
  local workspace="$1"
  printf '%s/kubeconfig\n' "$(workspace_history_dir "${workspace}")"
}

workspace_talosconfig_path() {
  local workspace="$1"
  printf '%s/talosconfig\n' "$(workspace_history_dir "${workspace}")"
}

workspace_metadata_path() {
  local workspace="$1"
  printf '%s/cluster-metadata.json\n' "$(workspace_history_dir "${workspace}")"
}

select_workspace() {
  local workspace="$1"
  if tofu workspace select "${workspace}" >/dev/null 2>&1; then
    return 0
  fi
  tofu workspace new "${workspace}" >/dev/null
}

snapshot_last_applied_tfvars() {
  local workspace="$1"
  local target_dir
  target_dir="$(workspace_history_dir "${workspace}")"
  mkdir -p "${target_dir}"

  if [[ -f "${TFVARS_FILE}" ]]; then
    cp "${TFVARS_FILE}" "$(workspace_last_applied_tfvars_path "${workspace}")"
    cp "${TFVARS_FILE}" "${target_dir}/tfvars.$(date +%Y%m%d%H%M%S).json"
  fi
}

snapshot_generated_artifacts() {
  local workspace="$1"
  local target_dir
  target_dir="$(workspace_history_dir "${workspace}")"
  mkdir -p "${target_dir}"

  if [[ -f "${KUBECONFIG_PATH}" ]]; then
    cp "${KUBECONFIG_PATH}" "$(workspace_kubeconfig_path "${workspace}")"
  fi

  if [[ -f "${TALOSCONFIG_PATH}" ]]; then
    cp "${TALOSCONFIG_PATH}" "$(workspace_talosconfig_path "${workspace}")"
  fi

  if [[ -f "${METADATA_PATH}" ]]; then
    cp "${METADATA_PATH}" "$(workspace_metadata_path "${workspace}")"
  fi
}

list_recorded_workspaces() {
  if [[ ! -d "${DEPLOYMENT_HISTORY_DIR}" ]]; then
    return 0
  fi

  local workspace_dir workspace
  while IFS= read -r workspace_dir; do
    [[ -z "${workspace_dir}" ]] && continue
    workspace="$(basename "${workspace_dir}")"
    if [[ -f "$(workspace_last_applied_tfvars_path "${workspace}")" ]] && workspace_has_state_resources "${workspace}"; then
      printf '%s\n' "${workspace}"
    fi
  done < <(find "${DEPLOYMENT_HISTORY_DIR}" -mindepth 1 -maxdepth 1 -type d | sort)
}

state_resource_count() {
  local state_path="$1"
  python3 - "${state_path}" <<'PY'
import json
import os
import sys

path = sys.argv[1]
if not os.path.exists(path):
    print(0)
    raise SystemExit(0)

try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    print(len(data.get("resources", [])))
except Exception:
    print(0)
PY
}

workspace_state_path() {
  local workspace="$1"
  if [[ "${workspace}" == "default" ]]; then
    printf '%s\n' "terraform.tfstate"
    return
  fi
  printf '%s\n' "terraform.tfstate.d/${workspace}/terraform.tfstate"
}

workspace_has_state_resources() {
  local workspace="$1"
  local original_workspace workspace_count

  original_workspace="$(tofu workspace show 2>/dev/null || true)"
  if [[ -z "${original_workspace}" ]]; then
    return 1
  fi

  if ! tofu workspace select "${workspace}" >/dev/null 2>&1; then
    tofu workspace select "${original_workspace}" >/dev/null 2>&1 || true
    return 1
  fi

  workspace_count="$(tofu state list 2>/dev/null | wc -l | tr -d ' ')"
  tofu workspace select "${original_workspace}" >/dev/null 2>&1 || true

  [[ "${workspace_count}" != "0" ]]
}

workspace_cluster_name() {
  local workspace="$1"
  local snapshot_path
  snapshot_path="$(workspace_last_applied_tfvars_path "${workspace}")"

  if [[ ! -f "${snapshot_path}" ]]; then
    printf '%s\n' "${workspace}"
    return
  fi

  python3 - "${snapshot_path}" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(data.get("cluster", {}).get("name", ""))
PY
}

describe_workspace() {
  local workspace="$1"
  local snapshot_path
  snapshot_path="$(workspace_last_applied_tfvars_path "${workspace}")"

  if [[ ! -f "${snapshot_path}" ]]; then
    printf '%s\n' "${workspace}"
    return
  fi

  python3 - "${snapshot_path}" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cluster = data.get("cluster", {})
nodes = data.get("nodes", {})
addons = data.get("addons", {})

cluster_name = cluster.get("name") or "unknown"
talos_version = cluster.get("talos_version") or "unknown"
kubernetes_version = cluster.get("kubernetes_version") or "unknown"
controlplanes = sum(1 for node in nodes.values() if isinstance(node, dict) and node.get("role") == "controlplane")
workers = sum(1 for node in nodes.values() if isinstance(node, dict) and node.get("role") == "worker")
vip = cluster.get("api_vip")

enabled_addons = []
if addons.get("cilium_enabled", True):
    enabled_addons.append("cilium")
if addons.get("metallb_enabled", False):
    enabled_addons.append("metallb")
if addons.get("traefik_enabled", False):
    enabled_addons.append("traefik")

parts = [
    cluster_name,
    f"Talos {talos_version}",
    f"Kubernetes {kubernetes_version}",
    f"{controlplanes} cp / {workers} wk",
]
if vip:
    parts.append(f"VIP {vip}")
parts.append("addons: " + (", ".join(enabled_addons) if enabled_addons else "none"))
print(" | ".join(parts))
PY
}

choose_destroy_workspace() {
  local workspaces=()
  local workspace

  while IFS= read -r workspace; do
    [[ -z "${workspace}" ]] && continue
    workspaces+=("${workspace}")
  done < <(list_recorded_workspaces)

  if (( ${#workspaces[@]} == 0 )); then
    if [[ -f "${TFVARS_FILE}" ]]; then
      printf '%s\n' "$(current_workspace_name)"
      return
    fi
    echo "No tracked deployments were found." >&2
    exit 1
  fi

  if (( ${#workspaces[@]} == 1 )); then
    printf '%s\n' "${workspaces[0]}"
    return
  fi

  if [[ ! -t 0 ]]; then
    log_error "Multiple tracked deployments exist. Set DESTROY_WORKSPACE to choose one."
    exit 1
  fi

  log_step "Tracked Talosforge deployments:"
  for workspace in "${workspaces[@]}"; do
    log_item "${workspace}: $(describe_workspace "${workspace}")"
  done

  while true; do
    local raw
    read -r -p "Enter the workspace name to destroy: " raw
    for workspace in "${workspaces[@]}"; do
      if [[ "${raw}" == "${workspace}" ]]; then
        printf '%s\n' "${workspace}"
        return
      fi
    done
    log_warn "Enter one of the listed workspace names."
  done
}

confirm_destroy_workspace() {
  local workspace="$1"
  local summary
  summary="$(describe_workspace "${workspace}")"

  if [[ ! -t 0 ]]; then
    return 0
  fi

  printf '\n'
  log_warn "You are about to destroy cluster workspace:"
  log_item "workspace: ${workspace}"
  log_item "cluster:   ${summary}"
  printf '\n'

  if ! prompt_yes_no "Proceed with destroy?" false; then
    log_warn "Destroy cancelled."
    exit 1
  fi
}

resolve_destroy_tfvars() {
  local workspace="$1"
  local snapshot_path
  snapshot_path="$(workspace_last_applied_tfvars_path "${workspace}")"

  if [[ -f "${snapshot_path}" ]]; then
    printf '%s\n' "${snapshot_path}"
    return
  fi

  if [[ -f "${TFVARS_FILE}" ]]; then
    printf '%s\n' "${TFVARS_FILE}"
    return
  fi

  log_error "Missing deployment snapshot for ${workspace} and no local ${TFVARS_FILE} is available."
  exit 1
}

resolve_destroy_state_args() {
  local workspace="$1"
  local workspace_state root_state workspace_count root_count

  workspace_state="$(workspace_state_path "${workspace}")"
  root_state="terraform.tfstate"
  workspace_count="$(state_resource_count "${workspace_state}")"
  root_count="$(state_resource_count "${root_state}")"

  if (( workspace_count > 0 )); then
    return 0
  fi

  if [[ "${workspace}" != "default" ]] && (( root_count > 0 )); then
    log_warn "Workspace ${workspace} is empty, but legacy root state still has resources. Falling back to terraform.tfstate."
    printf '%s\n' "-state=${root_state}"
  fi
}

current_workspace_managed_vmids() {
  local state_tmp

  state_tmp="$(mktemp)"
  if ! tofu state pull >"${state_tmp}" 2>/dev/null; then
    rm -f "${state_tmp}"
    return 0
  fi

  python3 - "${state_tmp}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)

for resource in data.get("resources", []):
    for instance in resource.get("instances", []):
        attrs = instance.get("attributes", {})
        vmid = attrs.get("vm_id")
        if isinstance(vmid, int):
            print(vmid)
PY
  rm -f "${state_tmp}"
}

validate_config() {
  need python3
  python3 scripts/validate_config.py --file "${TFVARS_FILE}"
}

render_talos_artifacts() {
  need python3
  need talosctl
  python3 scripts/render_talos_config.py --file "${TFVARS_FILE}" --out-dir "${OUT_DIR}"
}

tf_init() {
  need tofu
  ensure_dirs
  tofu init
}

run_proxmox_vmid_preflight() {
  local workspace="$1"

  if [[ "${SKIP_PROXMOX_PREFLIGHT}" == "true" ]]; then
    log_warn "Skipping Proxmox preflight because SKIP_PROXMOX_PREFLIGHT=true"
    return 0
  fi

  need python3

  local allow_args=()
  local vmid
  while IFS= read -r vmid; do
    [[ -z "${vmid}" ]] && continue
    allow_args+=(--allow-vmid "${vmid}")
  done < <(current_workspace_managed_vmids)

  if (( ${#allow_args[@]} > 0 )); then
    python3 scripts/check_proxmox.py --file "${TFVARS_FILE}" "${allow_args[@]}"
  else
    python3 scripts/check_proxmox.py --file "${TFVARS_FILE}"
  fi
}

run_proxmox_image_cache_preflight() {
  if [[ "${SKIP_PROXMOX_PREFLIGHT}" == "true" ]]; then
    return 0
  fi

  need python3

  if ! python3 scripts/check_image_cache.py "${TFVARS_FILE}"; then
    log_warn "Unable to determine Talos image cache status from Proxmox. Continuing with apply."
  fi
}

normalize_kubeconfig_for_merge() {
  local source_path="$1"
  local target_path="$2"
  local source_json_path

  source_json_path="$(mktemp)"
  kubectl --kubeconfig="${source_path}" config view --raw -o json >"${source_json_path}"

  python3 - "${source_json_path}" "${target_path}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)

contexts = data.get("contexts", [])
users = data.get("users", [])
users_by_name = {
    entry.get("name"): entry.get("user", {})
    for entry in users
    if entry.get("name")
}

new_users = []
seen_users = set()

for context_entry in contexts:
    context = context_entry.get("context", {})
    cluster_name = context.get("cluster")
    user_name = context.get("user")

    if not cluster_name or not user_name:
        continue

    desired_user_name = user_name if user_name.endswith(f"@{cluster_name}") else f"{user_name.split('@', 1)[0]}@{cluster_name}"
    context["user"] = desired_user_name

    if desired_user_name in seen_users:
        continue

    user_payload = users_by_name.get(user_name)
    if user_payload is None and "@" in user_name:
        user_payload = users_by_name.get(user_name.split("@", 1)[0])
    if user_payload is None:
        continue

    new_users.append({"name": desired_user_name, "user": user_payload})
    seen_users.add(desired_user_name)

if new_users:
    data["users"] = new_users

with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY

  rm -f "${source_json_path}"
}

merge_kubeconfig_files() {
  local existing_path="$1"
  local incoming_path="$2"
  local target_path="$3"
  local existing_json_path

  existing_json_path="$(mktemp)"
  kubectl --kubeconfig="${existing_path}" config view --raw -o json >"${existing_json_path}"

  python3 - "${existing_json_path}" "${incoming_path}" "${target_path}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    existing = json.load(fh)
with open(sys.argv[2], encoding="utf-8") as fh:
    incoming = json.load(fh)

def merge_named(existing_items, incoming_items):
    merged = {item["name"]: item for item in (existing_items or []) if isinstance(item, dict) and item.get("name")}
    for item in incoming_items or []:
        if isinstance(item, dict) and item.get("name"):
            merged[item["name"]] = item
    return list(merged.values())

merged = dict(existing)
merged["clusters"] = merge_named(existing.get("clusters", []), incoming.get("clusters", []))
merged["users"] = merge_named(existing.get("users", []), incoming.get("users", []))
merged["contexts"] = merge_named(existing.get("contexts", []), incoming.get("contexts", []))

if incoming.get("current-context"):
    merged["current-context"] = incoming["current-context"]

with open(sys.argv[3], "w", encoding="utf-8") as fh:
    json.dump(merged, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY

  rm -f "${existing_json_path}"
}

prune_cluster_from_kubeconfig() {
  local workspace="$1"
  local target_path snapshot_path

  target_path="${HOME}/.kube/config"
  snapshot_path="$(workspace_kubeconfig_path "${workspace}")"

  if [[ ! -f "${target_path}" || ! -f "${snapshot_path}" ]]; then
    return 0
  fi

  if ! python3 - "${target_path}" "${snapshot_path}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

target_path = Path(sys.argv[1])
snapshot_path = Path(sys.argv[2])

target_raw = subprocess.run(
    ["kubectl", "--kubeconfig", str(target_path), "config", "view", "--raw", "-o", "json"],
    check=True,
    capture_output=True,
    text=True,
).stdout
snapshot_raw = subprocess.run(
    ["kubectl", "--kubeconfig", str(snapshot_path), "config", "view", "--raw", "-o", "json"],
    check=True,
    capture_output=True,
    text=True,
).stdout

target = json.loads(target_raw)
snapshot = json.loads(snapshot_raw)

def user_aliases(user_name, cluster_name):
    names = set()
    if not user_name:
        return names
    names.add(user_name)
    base_name = user_name.split("@", 1)[0]
    if cluster_name:
        names.add(f"{base_name}@{cluster_name}")
    return names

clusters_to_remove = {entry.get("name") for entry in snapshot.get("clusters", []) if entry.get("name")}
contexts_to_remove = {entry.get("name") for entry in snapshot.get("contexts", []) if entry.get("name")}
users_to_remove = set()

for entry in snapshot.get("users", []):
    name = entry.get("name")
    if not name:
        continue
    users_to_remove.add(name)
    base_name = name.split("@", 1)[0]
    for cluster_name in clusters_to_remove:
        users_to_remove.add(f"{base_name}@{cluster_name}")

for entry in snapshot.get("contexts", []):
    context = entry.get("context", {})
    cluster_name = context.get("cluster")
    user_name = context.get("user")
    users_to_remove.update(user_aliases(user_name, cluster_name))

removed_contexts = set()
contexts_to_keep = []
for entry in target.get("contexts", []):
    name = entry.get("name")
    cluster_name = entry.get("context", {}).get("cluster")
    if name in contexts_to_remove or cluster_name in clusters_to_remove:
        removed_contexts.add(name)
        continue
    contexts_to_keep.append(entry)

clusters_to_keep = [entry for entry in target.get("clusters", []) if entry.get("name") not in clusters_to_remove]

users_in_use = {
    entry.get("context", {}).get("user")
    for entry in contexts_to_keep
    if entry.get("context", {}).get("user")
}
users_to_keep = [
    entry for entry in target.get("users", [])
    if entry.get("name") not in users_to_remove or entry.get("name") in users_in_use
]

target["contexts"] = contexts_to_keep
target["clusters"] = clusters_to_keep
target["users"] = users_to_keep

if target.get("current-context") in removed_contexts:
    target["current-context"] = contexts_to_keep[0]["name"] if contexts_to_keep else ""

target_path.write_text(json.dumps(target, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  then
    log_warn "Unable to prune Talosforge kubeconfig entries for ${workspace}."
  fi
}

prune_cluster_from_talosconfig() {
  local workspace="$1"
  local target_path cluster_name

  target_path="${HOME}/.talos/config"
  cluster_name="$(workspace_cluster_name "${workspace}")"

  if [[ ! -f "${target_path}" || -z "${cluster_name}" ]]; then
    return 0
  fi

  if ! grep -Fq "${cluster_name}" "${target_path}" 2>/dev/null; then
    return 0
  fi

  if ! talosctl --talosconfig "${target_path}" config remove "${cluster_name}" >/dev/null 2>&1; then
    log_warn "Unable to prune Talosforge talosconfig context '${cluster_name}' from ${target_path}."
  fi
}

out_metadata_matches_workspace() {
  local workspace="$1"
  local cluster_name

  if [[ ! -f "${METADATA_PATH}" ]]; then
    return 1
  fi

  cluster_name="$(workspace_cluster_name "${workspace}")"
  [[ -n "${cluster_name}" ]] || return 1

  python3 - "${METADATA_PATH}" "${cluster_name}" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if data.get("cluster_name") == sys.argv[2] else 1)
PY
}

cleanup_local_artifacts_for_workspace() {
  local workspace="$1"

  if ! out_metadata_matches_workspace "${workspace}"; then
    return 0
  fi

  rm -f \
    "${KUBECONFIG_PATH}" \
    "${TALOSCONFIG_PATH}" \
    "${METADATA_PATH}" \
    "${OUT_DIR}/controlplane.yaml" \
    "${OUT_DIR}/worker.yaml" \
    "${OUT_DIR}/bootstrap-nodes.tsv" \
    "${OUT_DIR}/worker-nodes.txt" \
    "${OUT_DIR}/secrets.yaml"
  rm -rf "${OUT_DIR}/patches" "${OUT_DIR}/addons"
}

cleanup_destroyed_workspace_metadata() {
  local workspace="$1"
  local history_dir

  history_dir="$(workspace_history_dir "${workspace}")"
  rm -rf "${history_dir}"

  if [[ "${workspace}" != "default" ]]; then
    tofu workspace select default >/dev/null 2>&1 || true
    tofu workspace delete -force "${workspace}" >/dev/null 2>&1 || true
  fi
}

wait_for_talos_node() {
  local node_ip="$1"
  local deadline=$((SECONDS + BOOTSTRAP_TIMEOUT_SECS))

  while (( SECONDS < deadline )); do
    if talosctl --talosconfig "${TALOSCONFIG_PATH}" -e "${node_ip}" -n "${node_ip}" version >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done

  echo "Timed out waiting for Talos API on ${node_ip}." >&2
  return 1
}

apply_talos_config_to_node() {
  local node_ip="$1"
  local base_config="$2"
  local patch_path="$3"
  local insecure_output secure_output

  if insecure_output="$(
    talosctl apply-config \
      --insecure \
      --nodes "${node_ip}" \
      --file "${base_config}" \
      --config-patch "@${patch_path}" 2>&1
  )"; then
    return 0
  fi

  if printf '%s' "${insecure_output}" | grep -qi 'tls: certificate required'; then
    log_warn "Node ${node_ip} is already using the secure Talos API; retrying apply-config with talosconfig."
    if secure_output="$(
      talosctl \
        --talosconfig "${TALOSCONFIG_PATH}" \
        --endpoints "${node_ip}" \
        --nodes "${node_ip}" \
        apply-config \
        --file "${base_config}" \
        --config-patch "@${patch_path}" 2>&1
    )"; then
      return 0
    fi
    printf '%s\n' "${secure_output}" >&2
    return 1
  fi

  printf '%s\n' "${insecure_output}" >&2
  return 1
}

wait_for_kubernetes_api() {
  local kubeconfig_path="$1"
  local deadline=$((SECONDS + BOOTSTRAP_TIMEOUT_SECS))

  while (( SECONDS < deadline )); do
    if KUBECONFIG="${kubeconfig_path}" kubectl version --output=json >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done

  log_error "Timed out waiting for the Kubernetes API to become reachable."
  return 1
}

wait_for_kubernetes_api_heartbeats() {
  local kubeconfig_path="$1"
  local required_heartbeats interval_seconds deadline consecutive

  required_heartbeats="${BOOTSTRAP_API_HEARTBEATS}"
  interval_seconds="${BOOTSTRAP_API_HEARTBEAT_INTERVAL_SECS}"

  if [[ ! "${required_heartbeats}" =~ ^[0-9]+$ ]] || (( required_heartbeats < 1 )); then
    required_heartbeats=3
  fi
  if [[ ! "${interval_seconds}" =~ ^[0-9]+$ ]] || (( interval_seconds < 1 )); then
    interval_seconds=5
  fi

  deadline=$((SECONDS + BOOTSTRAP_TIMEOUT_SECS))
  consecutive=0

  log_step "Waiting for stable Kubernetes API heartbeats (${required_heartbeats} consecutive /readyz checks)"
  while (( SECONDS < deadline )); do
    if KUBECONFIG="${kubeconfig_path}" kubectl get --raw='/readyz' >/dev/null 2>&1; then
      consecutive=$((consecutive + 1))
      log_item "Kubernetes API heartbeat ${consecutive}/${required_heartbeats}"
      if (( consecutive >= required_heartbeats )); then
        return 0
      fi
    else
      if (( consecutive > 0 )); then
        log_warn "Kubernetes API heartbeat interrupted; restarting readiness count."
      fi
      consecutive=0
    fi
    sleep "${interval_seconds}"
  done

  log_error "Timed out waiting for stable Kubernetes API heartbeats."
  return 1
}

wait_for_cluster_readiness_fallback() {
  local kubeconfig_path="$1"

  log_warn "Falling back to Kubernetes readiness checks."
  KUBECONFIG="${kubeconfig_path}" kubectl wait --for=condition=Ready nodes --all --timeout=10m
  KUBECONFIG="${kubeconfig_path}" kubectl -n kube-system rollout status deployment/coredns --timeout=10m
}

wait_for_talos_cluster_health() {
  local talosconfig_path="$1"
  local kubeconfig_path="$2"
  local controlplane_nodes_csv="$3"
  local init_node_ip="$4"
  local k8s_endpoint_url="$5"
  local wait_timeout="$6"
  log_warn "Skipping talosctl health in bootstrap because Talos HA discovery is currently hanging on duplicate-node reporting."
  log_warn "Using Kubernetes readiness checks for bootstrap completion instead."
  wait_for_cluster_readiness_fallback "${kubeconfig_path}"
}

run_talos_service_health_checks() {
  local talosconfig_path="$1"
  local endpoint_ip="$2"
  local controlplane_nodes_csv="$3"
  local worker_nodes_csv="$4"
  local all_nodes_csv

  all_nodes_csv="${controlplane_nodes_csv}"
  if [[ -n "${worker_nodes_csv}" ]]; then
    all_nodes_csv="${all_nodes_csv},${worker_nodes_csv}"
  fi

  printf '\n'
  log_step "Checking Talos apid service"
  talosctl --talosconfig "${talosconfig_path}" service apid -e "${endpoint_ip}" -n "${all_nodes_csv}"

  printf '\n'
  log_step "Checking Talos kubelet service"
  talosctl --talosconfig "${talosconfig_path}" service kubelet -e "${endpoint_ip}" -n "${all_nodes_csv}"

  printf '\n'
  log_step "Checking Talos etcd service"
  talosctl --talosconfig "${talosconfig_path}" service etcd -e "${endpoint_ip}" -n "${controlplane_nodes_csv}"

  printf '\n'
  log_warn "Skipping raw talosctl health because Talos HA discovery is misreporting duplicate/missing nodes in this setup."
  log_warn "Talosforge health uses direct Talos service checks plus Kubernetes readiness instead."
}

bootstrap_etcd_if_needed() {
  local output

  if output="$(
    talosctl \
      --talosconfig "${TALOSCONFIG_PATH}" \
      -e "${controlplane_csv}" \
      -n "${first_controlplane_ip}" \
      bootstrap 2>&1
  )"; then
    return 0
  fi

  if printf '%s' "${output}" | grep -Eqi 'already bootstrap|already initialized|already exists|already configured|data directory is not empty'; then
    log_warn "Talos bootstrap was already completed earlier; continuing."
    return 0
  fi

  printf '%s\n' "${output}" >&2
  return 1
}

install_cilium() {
  local cilium_enabled chart_version values_path

  cilium_enabled="$(metadata_get "cilium_enabled")"
  if [[ "${cilium_enabled}" != "true" ]]; then
    return 0
  fi

  need helm
  chart_version="$(metadata_get "cilium_chart_version")"
  values_path="$(metadata_get "cilium_values_path")"

  log_step "Waiting for Kubernetes API"
  wait_for_kubernetes_api "${KUBECONFIG_PATH}"
  wait_for_kubernetes_api_heartbeats "${KUBECONFIG_PATH}"

  log_step "Installing Cilium ${chart_version}"
  clear_pending_helm_release "cilium" "kube-system"
  KUBECONFIG="${KUBECONFIG_PATH}" \
    helm upgrade --install cilium oci://quay.io/cilium/charts/cilium \
      --version "${chart_version}" \
      --namespace kube-system \
      --create-namespace \
      --values "${values_path}"

  printf '\n'
  log_step "Waiting for Cilium operator"
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n kube-system rollout status deployment/cilium-operator --timeout=10m

  printf '\n'
  log_step "Waiting for Kubernetes nodes to be Ready after Cilium install"
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl wait --for=condition=Ready nodes --all --timeout=10m

  if ! KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n kube-system rollout status daemonset/cilium --timeout=5m; then
    log_warn "Cilium DaemonSet rollout is still converging; continuing because all Kubernetes nodes are Ready."
  fi
}

helm_release_status() {
  local release="$1"
  local namespace="$2"
  local status_output

  if ! status_output="$(KUBECONFIG="${KUBECONFIG_PATH}" helm --namespace "${namespace}" status "${release}" 2>/dev/null)"; then
    return 1
  fi

  printf '%s\n' "${status_output}" | awk -F': ' '/^STATUS:/{print $2; exit}'
}

clear_pending_helm_release() {
  local release="$1"
  local namespace="$2"
  local status revision secret_name

  if ! status="$(helm_release_status "${release}" "${namespace}")"; then
    return 0
  fi

  case "${status}" in
    pending-install|pending-upgrade|pending-rollback)
      ;;
    *)
      return 0
      ;;
  esac

  revision="$(KUBECONFIG="${KUBECONFIG_PATH}" helm --namespace "${namespace}" history "${release}" 2>/dev/null | awk 'NR > 1 {rev=$1} END {print rev}')"
  if [[ -z "${revision}" ]]; then
    return 0
  fi

  secret_name="sh.helm.release.v1.${release}.v${revision}"
  log_warn "Helm release ${release} in namespace ${namespace} is stuck in status ${status}; removing stale release secret ${secret_name}."
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl --namespace "${namespace}" delete secret "${secret_name}" --ignore-not-found >/dev/null
}

install_traefik() {
  local traefik_enabled chart_version values_path

  traefik_enabled="$(metadata_get "traefik_enabled")"
  if [[ "${traefik_enabled}" != "true" ]]; then
    return 0
  fi

  need helm
  chart_version="$(metadata_get "traefik_chart_version")"
  values_path="$(metadata_get "traefik_values_path")"

  log_step "Installing Traefik ${chart_version}"
  clear_pending_helm_release "traefik" "traefik"
  helm repo add traefik https://traefik.github.io/charts >/dev/null 2>&1 || true
  helm repo update >/dev/null
  KUBECONFIG="${KUBECONFIG_PATH}" \
    helm upgrade --install traefik traefik/traefik \
      --version "${chart_version}" \
      --namespace traefik \
      --create-namespace \
      --values "${values_path}" \
      --wait
}

install_proxmox_csi() {
  local proxmox_csi_enabled chart_version values_path endpoint username insecure region temp_secret

  proxmox_csi_enabled="$(metadata_get "proxmox_csi_enabled")"
  if [[ "${proxmox_csi_enabled}" != "true" ]]; then
    return 0
  fi

  if [[ -z "${PROXMOX_PASSWORD:-}" ]]; then
    load_proxmox_password_from_tfvars "${TFVARS_FILE}"
  fi

  need helm
  chart_version="$(metadata_get "proxmox_csi_chart_version")"
  values_path="$(metadata_get "proxmox_csi_values_path")"
  endpoint="$(metadata_get "proxmox_endpoint")"
  username="$(metadata_get "proxmox_username")"
  insecure="$(metadata_get "proxmox_insecure")"
  region="$(metadata_get "proxmox_cluster_name")"

  if [[ -z "${region}" ]]; then
    region="$(read_tfvar "proxmox.node_default")"
  fi

  temp_secret="$(mktemp)"
  cat >"${temp_secret}" <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: proxmox-csi-plugin
  namespace: csi-proxmox
stringData:
  config.yaml: |
    clusters:
      - url: "${endpoint}"
        insecure: ${insecure}
        username: "${username}"
        password: "${PROXMOX_PASSWORD}"
        region: "${region}"
EOF

  log_step "Installing Proxmox CSI ${chart_version}"
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl create namespace csi-proxmox --dry-run=client -o yaml | KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f -
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl label namespace csi-proxmox \
    pod-security.kubernetes.io/enforce=privileged \
    pod-security.kubernetes.io/audit=privileged \
    pod-security.kubernetes.io/warn=privileged \
    --overwrite >/dev/null
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${temp_secret}"
  rm -f "${temp_secret}"

  clear_pending_helm_release "proxmox-csi-plugin" "csi-proxmox"
  KUBECONFIG="${KUBECONFIG_PATH}" \
    helm upgrade --install proxmox-csi-plugin oci://ghcr.io/sergelogvinov/charts/proxmox-csi-plugin \
      --version "${chart_version}" \
      --namespace csi-proxmox \
      --create-namespace \
      --values "${values_path}" \
      --wait
}

install_metallb() {
  local metallb_enabled chart_version resources_path

  metallb_enabled="$(metadata_get "metallb_enabled")"
  if [[ "${metallb_enabled}" != "true" ]]; then
    return 0
  fi

  need helm
  chart_version="$(metadata_get "metallb_chart_version")"
  resources_path="$(metadata_get "metallb_resources_path")"

  log_step "Installing MetalLB ${chart_version}"
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl create namespace metallb-system --dry-run=client -o yaml | KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f -
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl label namespace metallb-system \
    pod-security.kubernetes.io/enforce=privileged \
    pod-security.kubernetes.io/audit=privileged \
    pod-security.kubernetes.io/warn=privileged \
    --overwrite >/dev/null
  clear_pending_helm_release "metallb" "metallb-system"
  helm repo add metallb https://metallb.github.io/metallb >/dev/null 2>&1 || true
  helm repo update >/dev/null
  KUBECONFIG="${KUBECONFIG_PATH}" \
    helm upgrade --install metallb metallb/metallb \
      --version "${chart_version}" \
      --namespace metallb-system \
      --create-namespace

  printf '\n'
  log_step "Waiting for MetalLB controller"
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n metallb-system rollout status deployment/metallb-controller --timeout=10m

  if ! KUBECONFIG="${KUBECONFIG_PATH}" kubectl -n metallb-system rollout status daemonset/metallb-speaker --timeout=5m; then
    log_warn "MetalLB speaker DaemonSet is still converging; continuing after controller readiness."
  fi

  log_step "Applying MetalLB address pools"
  KUBECONFIG="${KUBECONFIG_PATH}" kubectl apply -f "${resources_path}"
}

install_kubeconfig() {
  local target_dir target_path backup_path merge_tmp merged_config_tmp normalized_source_path
  target_dir="${HOME}/.kube"
  target_path="${target_dir}/config"

  if [[ ! -f "${KUBECONFIG_PATH}" ]]; then
    log_error "Missing ${KUBECONFIG_PATH}. Run ./deploy.sh bootstrap first."
    exit 1
  fi

  need kubectl
  mkdir -p "${target_dir}"

  merge_tmp="$(mktemp -d)"
  normalized_source_path="${merge_tmp}/incoming-kubeconfig.json"
  normalize_kubeconfig_for_merge "${KUBECONFIG_PATH}" "${normalized_source_path}"

  if [[ -f "${target_path}" ]]; then
    backup_path="${target_path}.bak.$(date +%Y%m%d%H%M%S)"
    cp "${target_path}" "${backup_path}"
    log_step "Backed up existing kubeconfig to ${backup_path}"

    merged_config_tmp="${merge_tmp}/config"
    merge_kubeconfig_files "${backup_path}" "${normalized_source_path}" "${merged_config_tmp}"
    cp "${merged_config_tmp}" "${target_path}"
    log_success "Merged kubeconfig into ${target_path}"
  else
    cp "${normalized_source_path}" "${target_path}"
    log_success "Installed kubeconfig to ${target_path}"
  fi

  chmod 600 "${target_path}"
  rm -rf "${merge_tmp}"
}

install_talosconfig() {
  local target_dir target_path backup_path
  target_dir="${HOME}/.talos"
  target_path="${target_dir}/config"

  if [[ ! -f "${TALOSCONFIG_PATH}" ]]; then
    log_error "Missing ${TALOSCONFIG_PATH}. Run ./deploy.sh bootstrap first."
    exit 1
  fi

  need talosctl
  mkdir -p "${target_dir}"

  if [[ -f "${target_path}" ]]; then
    backup_path="${target_path}.bak.$(date +%Y%m%d%H%M%S)"
    cp "${target_path}" "${backup_path}"
    log_step "Backed up existing talosconfig to ${backup_path}"
    talosctl --talosconfig "${target_path}" config merge "${TALOSCONFIG_PATH}"
    log_success "Merged talosconfig into ${target_path}"
  else
    cp "${TALOSCONFIG_PATH}" "${target_path}"
    log_success "Installed talosconfig to ${target_path}"
  fi

  chmod 600 "${target_path}"
}

refresh_kubeconfig_after_bootstrap_if_available() {
  if [[ -f "${KUBECONFIG_PATH}" ]]; then
    printf '\n'
    log_info "Refreshing local kubeconfig from ${KUBECONFIG_PATH}..."
    install_kubeconfig
  fi
}

refresh_talosconfig_after_bootstrap_if_available() {
  if [[ -f "${TALOSCONFIG_PATH}" ]]; then
    printf '\n'
    log_info "Refreshing local talosconfig from ${TALOSCONFIG_PATH}..."
    install_talosconfig
  fi
}

preflight_workflow_tools() {
  printf '\n'
  log_step "Checking required tools for Talosforge workflow..."
  local required_commands=(python3 tofu talosctl kubectl)
  if [[ ! -f "${TFVARS_FILE}" ]] || uses_helm_addons; then
    required_commands+=(helm)
  fi
  need_all "${required_commands[@]}"
}

preflight_action_tools() {
  local action="$1"

  case "${action}" in
    preflight)
      preflight_workflow_tools
      ;;
    configure)
      need_all python3
      ;;
    apply)
      need_all python3 tofu talosctl
      ;;
    bootstrap)
      local bootstrap_commands=(python3 talosctl kubectl)
      if [[ -f "${TFVARS_FILE}" ]] && uses_helm_addons; then
        bootstrap_commands+=(helm)
      fi
      need_all "${bootstrap_commands[@]}"
      ;;
    health)
      need_all talosctl kubectl
      ;;
    destroy)
      need_all python3 tofu kubectl talosctl
      ;;
    install-kubeconfig)
      need_all python3 kubectl
      ;;
    install-talosconfig)
      need_all talosctl
      ;;
  esac
}

run_action_preflight() {
  case "${ACTION}" in
    help|-h|--help)
      return 0
      ;;
  esac

  log_step "Preflight: checking required tools for '${ACTION}'..."
  preflight_action_tools "${ACTION}"

  return 0
}

run_preflight() {
  ensure_dirs
  printf '\n'
  log_success "Preflight complete."
}

run_configure() {
  ensure_dirs
  python3 scripts/configure.py --file "${TFVARS_FILE}" "$@"
  preflight_workflow_tools

  printf '\n'
  log_success "Configuration complete."
  log_info "Next step:"
  log_next_step "./deploy.sh apply"
}

run_apply() {
  local workspace

  ensure_dirs
  ensure_tfvars
  validate_config
  render_talos_artifacts
  load_proxmox_password_from_tfvars "${TFVARS_FILE}"
  tf_init
  workspace="$(current_workspace_name)"
  select_workspace "${workspace}"
  run_proxmox_vmid_preflight "${workspace}"
  run_proxmox_image_cache_preflight
  tofu apply -var-file="${TFVARS_FILE}" -auto-approve "$@"
  snapshot_last_applied_tfvars "${workspace}"
  snapshot_generated_artifacts "${workspace}"

  printf '\n'
  log_success "Infrastructure apply complete for workspace ${workspace}."
  log_info "Next step:"
  log_next_step "./deploy.sh bootstrap"
}

run_bootstrap() {
  local first_controlplane_ip controlplane_csv endpoint_url workspace

  ensure_dirs
  ensure_tfvars
  validate_config
  render_talos_artifacts

  workspace="$(current_workspace_name)"
  first_controlplane_ip="$(metadata_get "bootstrap_controlplane_ip")"
  controlplane_csv="$(metadata_get "controlplane_csv")"
  endpoint_url="$(metadata_get "kubernetes_endpoint")"

  log_step "Applying Talos configs"
  while IFS=$'\t' read -r role node_name node_ip patch_path; do
    local base_config
    base_config="${OUT_DIR}/${role}.yaml"

    log_item "${node_name} (${node_ip})"
    apply_talos_config_to_node "${node_ip}" "${base_config}" "${patch_path}"
  done <"${OUT_DIR}/bootstrap-nodes.tsv"

  printf '\n'
  log_step "Waiting for Talos API"
  while IFS=$'\t' read -r _role node_name node_ip _patch_path; do
    log_item "${node_name} (${node_ip})"
    wait_for_talos_node "${node_ip}"
  done <"${OUT_DIR}/bootstrap-nodes.tsv"

  printf '\n'
  log_step "Configuring talosconfig endpoints"
  talosctl --talosconfig "${TALOSCONFIG_PATH}" config endpoint ${controlplane_csv//,/ }
  talosctl --talosconfig "${TALOSCONFIG_PATH}" config node "${first_controlplane_ip}"

  printf '\n'
  log_step "Bootstrapping etcd on ${first_controlplane_ip}"
  bootstrap_etcd_if_needed

  printf '\n'
  log_step "Fetching kubeconfig"
  rm -f "${KUBECONFIG_PATH}"
  (
    cd "${OUT_DIR}"
    talosctl \
      --talosconfig talosconfig \
      -e "${controlplane_csv}" \
      -n "${first_controlplane_ip}" \
      kubeconfig . \
      --merge=false \
      --force
  )

  install_cilium

  printf '\n'
  log_step "Waiting for cluster health"
  wait_for_talos_cluster_health "${TALOSCONFIG_PATH}" "${KUBECONFIG_PATH}" "${controlplane_csv}" "${first_controlplane_ip}" "${endpoint_url}" "15m"

  install_metallb
  install_proxmox_csi
  install_traefik

  snapshot_last_applied_tfvars "${workspace}"
  snapshot_generated_artifacts "${workspace}"
  refresh_kubeconfig_after_bootstrap_if_available
  refresh_talosconfig_after_bootstrap_if_available
  snapshot_generated_artifacts "${workspace}"

  printf '\n'
  log_success "Bootstrap complete."
  log_info "Exports:"
  log_item "${TALOSCONFIG_PATH}"
  log_item "${KUBECONFIG_PATH}"
}

run_health() {
  local talosconfig_path kubeconfig_path first_controlplane_ip controlplane_csv worker_csv endpoint_url cilium_enabled metallb_enabled traefik_enabled proxmox_csi_enabled

  need talosctl
  need kubectl

  talosconfig_path="${HOME}/.talos/config"
  if [[ ! -f "${talosconfig_path}" ]]; then
    talosconfig_path="${TALOSCONFIG_PATH}"
  fi

  kubeconfig_path="${HOME}/.kube/config"
  if [[ ! -f "${kubeconfig_path}" ]]; then
    kubeconfig_path="${KUBECONFIG_PATH}"
  fi

  if [[ ! -f "${talosconfig_path}" ]]; then
    log_error "Missing talosconfig. Run ./deploy.sh bootstrap first."
    exit 1
  fi

  if [[ ! -f "${kubeconfig_path}" ]]; then
    log_error "Missing kubeconfig. Run ./deploy.sh bootstrap first."
    exit 1
  fi

  first_controlplane_ip="$(metadata_get "bootstrap_controlplane_ip")"
  controlplane_csv="$(metadata_get "controlplane_csv")"
  worker_csv="$(metadata_csv "worker_ips")"
  endpoint_url="$(metadata_get "kubernetes_endpoint")"
  cilium_enabled="$(metadata_get "cilium_enabled")"
  metallb_enabled="$(metadata_get "metallb_enabled")"
  traefik_enabled="$(metadata_get "traefik_enabled")"
  proxmox_csi_enabled="$(metadata_get "proxmox_csi_enabled")"

  run_talos_service_health_checks "${talosconfig_path}" "${first_controlplane_ip}" "${controlplane_csv}" "${worker_csv}"
  wait_for_cluster_readiness_fallback "${kubeconfig_path}"

  printf '\n'
  log_step "kubectl get nodes -o wide"
  KUBECONFIG="${kubeconfig_path}" kubectl get nodes -o wide
  printf '\n'
  log_step "kubectl get pods -A"
  KUBECONFIG="${kubeconfig_path}" kubectl get pods -A

  if [[ "${cilium_enabled}" == "true" ]]; then
    printf '\n'
    log_step "Waiting for Cilium DaemonSet"
    KUBECONFIG="${kubeconfig_path}" kubectl -n kube-system rollout status daemonset/cilium --timeout=5m
    printf '\n'
    log_step "Waiting for Cilium Operator"
    KUBECONFIG="${kubeconfig_path}" kubectl -n kube-system rollout status deployment/cilium-operator --timeout=5m
    printf '\n'
    log_step "Waiting for Ready nodes"
    KUBECONFIG="${kubeconfig_path}" kubectl wait --for=condition=Ready nodes --all --timeout=5m
  else
    printf '\n'
    log_warn "Skipping node Ready wait because Cilium installation is disabled."
  fi

  if [[ "${metallb_enabled}" == "true" ]]; then
    printf '\n'
    log_step "Waiting for MetalLB controller"
    KUBECONFIG="${kubeconfig_path}" kubectl -n metallb-system rollout status deployment/metallb-controller --timeout=5m
    printf '\n'
    log_step "Waiting for MetalLB speakers"
    KUBECONFIG="${kubeconfig_path}" kubectl -n metallb-system rollout status daemonset/metallb-speaker --timeout=5m
  fi

  if [[ "${traefik_enabled}" == "true" ]]; then
    printf '\n'
    log_step "Waiting for Traefik deployment"
    KUBECONFIG="${kubeconfig_path}" kubectl -n traefik rollout status deployment/traefik --timeout=5m
  fi

  if [[ "${proxmox_csi_enabled}" == "true" ]]; then
    printf '\n'
    log_step "Waiting for Proxmox CSI pods"
    KUBECONFIG="${kubeconfig_path}" kubectl -n csi-proxmox wait --for=condition=Ready pod -l app.kubernetes.io/instance=proxmox-csi-plugin --timeout=10m
  fi
}

run_destroy() {
  local destroy_workspace destroy_tfvars destroy_state_arg

  ensure_dirs
  destroy_workspace="${DESTROY_WORKSPACE:-$(choose_destroy_workspace)}"
  confirm_destroy_workspace "${destroy_workspace}"
  destroy_tfvars="$(resolve_destroy_tfvars "${destroy_workspace}")"
  if [[ ! -f "${destroy_tfvars}" ]]; then
    log_error "Missing ${destroy_tfvars}."
    log_error "Run ./deploy.sh apply first or restore the last applied tfvars snapshot for ${destroy_workspace}."
    exit 1
  fi

  load_proxmox_password_from_tfvars "${destroy_tfvars}"
  tf_init
  destroy_state_arg="$(resolve_destroy_state_args "${destroy_workspace}")"

  log_step "Destroying workspace ${destroy_workspace}"
  log_info "Using deployment snapshot: ${destroy_tfvars}"
  select_workspace "${destroy_workspace}"
  if [[ -n "${destroy_state_arg}" ]]; then
    tofu destroy -refresh=false "${destroy_state_arg}" -var-file="${destroy_tfvars}" -auto-approve "$@"
  else
    tofu destroy -refresh=false -var-file="${destroy_tfvars}" -auto-approve "$@"
  fi
  prune_cluster_from_kubeconfig "${destroy_workspace}"
  prune_cluster_from_talosconfig "${destroy_workspace}"
  cleanup_local_artifacts_for_workspace "${destroy_workspace}"
  cleanup_destroyed_workspace_metadata "${destroy_workspace}"
}

run_install_kubeconfig() {
  local workspace
  workspace="$(current_workspace_name)"
  install_kubeconfig
  snapshot_generated_artifacts "${workspace}"
}

run_install_talosconfig() {
  local workspace
  workspace="$(current_workspace_name)"
  install_talosconfig
  snapshot_generated_artifacts "${workspace}"
}

run_action_preflight

case "${ACTION}" in
  preflight)
    run_preflight "$@"
    ;;
  configure)
    run_configure "$@"
    ;;
  apply)
    run_apply "$@"
    ;;
  bootstrap)
    run_bootstrap "$@"
    ;;
  health)
    run_health "$@"
    ;;
  destroy)
    run_destroy "$@"
    ;;
  install-kubeconfig)
    run_install_kubeconfig "$@"
    ;;
  install-talosconfig)
    run_install_talosconfig "$@"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
