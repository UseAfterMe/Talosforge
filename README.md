# Talosforge

Talosforge deploys Talos Linux Kubernetes clusters on Proxmox.

It uses OpenTofu/Terraform for VM provisioning, `talosctl` for cluster lifecycle, Talos VIP for the Kubernetes API endpoint, and a simple `deploy.sh` workflow for day-to-day operations.

## Overview

Talosforge is built for clean Proxmox-based Talos clusters with a straightforward operator flow:

- Provision Talos VMs on Proxmox.
- Support single-node and HA control planes.
- Use Talos VIP for a stable Kubernetes API endpoint.
- Generate and apply Talos machine configs.
- Bootstrap the cluster and export client configs.
- Optionally install core add-ons during bootstrap:
  Cilium, Cilium LoadBalancer IPAM + L2 announcements, Traefik, and Proxmox CSI.

## Quick Start

```bash
./deploy.sh configure
./deploy.sh apply
./deploy.sh bootstrap
./deploy.sh health
```

## Commands

```bash
./deploy.sh configure
./deploy.sh preflight
./deploy.sh apply
./deploy.sh bootstrap
./deploy.sh health
./deploy.sh destroy
./deploy.sh install-kubeconfig
./deploy.sh install-talosconfig
```

What each command does:

- `configure` builds or updates `terraform.tfvars.json` interactively.
- `preflight` checks required tools before you deploy.
- `apply` validates config, renders Talos artifacts, and provisions infrastructure.
- `bootstrap` applies Talos config, bootstraps etcd, installs enabled add-ons, exports client configs, and merges them into your local kubeconfig and talosconfig automatically.
- `health` checks Talos services, Kubernetes readiness, and add-on rollout health.
- `destroy` tears down one tracked Talosforge workspace and prunes only that cluster's local config entries.
- `install-kubeconfig` manually re-merges `out/kubeconfig` into your local kubeconfig.
- `install-talosconfig` manually re-merges `out/talosconfig` into your local talosconfig.

## Highlights

- Proxmox-aware interactive configuration for nodes, datastores, VLANs, host placement, and VMIDs.
- Talos Image Factory workflow with `siderolabs/qemu-guest-agent` enabled by default.
- Proxmox-managed MAC addresses only.
- VirtIO disk layout with Talos installing to `/dev/vda`.
- Talos VIP for control-plane HA and Cilium-native LoadBalancer IP management.
- Safe local config handling that merges kubeconfig and talosconfig entries instead of replacing full files.
- Tracked destroy flow that removes only Talosforge-owned local entries and artifacts.
- Direct Talos service checks, HA-aware `talosctl health`, Kubernetes readiness checks, and LoadBalancer visibility.

## Outputs

Talosforge renders and exports cluster artifacts under `out/`:

- `out/controlplane.yaml`
- `out/worker.yaml`
- `out/talosconfig`
- `out/kubeconfig`
- `out/addons/`
- `out/deployment-history/`

## Repo Layout

```text
Talosforge/
├── README.md
├── LICENSE
├── .gitignore
├── deploy.sh
├── versions.tf
├── variables.tf
├── main.tf
├── outputs.tf
├── terraform.tfvars.example
├── templates/
│   ├── controlplane.yaml.tftpl
│   ├── worker.yaml.tftpl
│   └── talos-patches/
├── scripts/
│   ├── configure.py
│   ├── validate_config.py
│   └── render_talos_config.py
└── out/
```

## Current Status

Talosforge is working today for the core Proxmox + Talos workflow:

- Talos VM provisioning on Proxmox.
- Talos config rendering and bootstrap.
- Safe local kubeconfig and talosconfig merge behavior.
- Optional add-on installation for Cilium, Cilium LoadBalancer IPAM + L2 announcements, Traefik, and Proxmox CSI.
- Health checks for Talos services, HA-aware `talosctl health`, Kubernetes node readiness, enabled add-ons, and Cilium LoadBalancer state.

## Storage Notes

If you enable Proxmox CSI, these upstream references are worth keeping handy:

- Proxmox CSI driver:
  `https://github.com/sergelogvinov/proxmox-csi-plugin`
- Proxmox CSI migration utility:
  `https://github.com/sergelogvinov/proxmox-csi-plugin/blob/main/docs/pvecsictl.md`

For this stack, `pvecsictl` is the important migration tool to call out. The upstream Proxmox CSI docs explicitly note that volumes on local storage cannot automatically move across Proxmox nodes and should be moved with `pvecsictl`. A more generic PVC copy tool like `pv-migrate` can still be useful for storage-class or namespace migrations, but it is not the Proxmox CSI-native answer for node-to-node local-volume moves.

## References

- Stonegarden Talos on Proxmox with OpenTofu:
  `https://blog.stonegarden.dev/articles/2024/08/talos-proxmox-tofu/`
- Talos boot assets and Image Factory:
  `https://docs.siderolabs.com/talos/v1.12/platform-specific-installations/boot-assets`
- Talos getting started:
  `https://docs.siderolabs.com/talos/v1.12/getting-started/getting-started`
- Talos virtual shared IP:
  `https://docs.siderolabs.com/talos/v1.12/networking/advanced/vip`
- Cilium LB IPAM:
  `https://docs.cilium.io/en/stable/network/lb-ipam/`
- Cilium L2 announcements:
  `https://docs.cilium.io/en/stable/network/l2-announcements/`
- Talos patching:
  `https://docs.siderolabs.com/talos/v1.12/configure-your-talos-cluster/system-configuration/patching`
- Talos CLI install and usage:
  `https://docs.siderolabs.com/talos/v1.12/getting-started/talosctl`
