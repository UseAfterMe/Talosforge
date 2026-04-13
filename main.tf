provider "proxmox" {
  endpoint = var.proxmox.endpoint
  username = var.proxmox.username
  password = var.proxmox_password
  insecure = var.proxmox.insecure
}

locals {
  normalized_nodes = {
    for name, node in var.nodes : name => merge(node, {
      host_node         = coalesce(try(node.host_node, null), var.proxmox.node_default)
      datastore_id      = coalesce(try(node.datastore_id, null), var.proxmox.vm_datastore)
      bridge            = coalesce(try(node.bridge, null), var.proxmox.network_bridge)
      vlan_id           = try(node.vlan_id, null) != null ? node.vlan_id : try(var.proxmox.vlan_id, null)
      network_interface = coalesce(try(node.network_interface, null), try(var.cluster.network_interface, "eth0"))
      start_on_boot     = try(node.start_on_boot, true)
      tags              = distinct(concat(try(var.proxmox.tags, []), [var.cluster.name, node.role], try(node.tags, [])))
    })
  }

  controlplane_nodes = {
    for name, node in local.normalized_nodes : name => node
    if node.role == "controlplane"
  }

  worker_nodes = {
    for name, node in local.normalized_nodes : name => node
    if node.role == "worker"
  }

  highly_available = length(local.controlplane_nodes) > 1

  kubernetes_endpoint_host = local.highly_available ? var.cluster.api_vip : one([
    for _, node in local.controlplane_nodes : node.ip
  ])

  kubernetes_endpoint = "https://${local.kubernetes_endpoint_host}:6443"
  proxmox_hosts       = toset(distinct([for _, node in local.normalized_nodes : node.host_node]))
  schematic_id        = coalesce(try(var.image.schematic_id, null), try(trimspace(file(var.image.schematic_file)), null))
  talos_image_url     = "${var.image.factory_base_url}/image/${local.schematic_id}/${var.cluster.talos_version}/${var.image.platform}-${var.image.arch}.raw.gz"
  talos_image_name    = "talos-${replace(var.cluster.talos_version, ".", "-")}-${var.image.platform}-${var.image.arch}-${substr(local.schematic_id, 0, 12)}.img"
}

resource "proxmox_download_file" "talos_image" {
  for_each = local.proxmox_hosts

  content_type            = "iso"
  datastore_id            = var.proxmox.image_datastore
  node_name               = each.key
  file_name               = local.talos_image_name
  url                     = local.talos_image_url
  decompression_algorithm = "gz"
  overwrite               = false
}

resource "proxmox_virtual_environment_vm" "node" {
  for_each = local.normalized_nodes

  name            = "${var.cluster.name}-${replace(replace(each.key, "-0", ""), "-", "")}"
  description     = "Talos ${each.value.role} node for ${var.cluster.name}"
  tags            = each.value.tags
  node_name       = each.value.host_node
  vm_id           = each.value.vm_id
  on_boot         = each.value.start_on_boot
  started         = true
  stop_on_destroy = true
  machine         = "q35"
  bios            = "seabios"
  boot_order      = ["virtio0"]
  hotplug         = "network,disk,usb,cpu"

  agent {
    enabled = contains(try(var.image.extensions, []), "siderolabs/qemu-guest-agent")
    timeout = "1m"
  }

  cpu {
    cores = each.value.cpu
    type  = "host"
    numa  = true
  }

  memory {
    dedicated = each.value.memory_mb
    floating  = 0
  }

  disk {
    datastore_id = each.value.datastore_id
    interface    = "virtio0"
    file_id      = proxmox_download_file.talos_image[each.value.host_node].id
    file_format  = "raw"
    size         = each.value.disk_gb
    iothread     = true
    discard      = "on"
    ssd          = true
    cache        = "writethrough"
  }

  initialization {
    datastore_id = coalesce(try(var.proxmox.initialization_datastore, null), each.value.datastore_id)

    dynamic "dns" {
      for_each = length(try(var.cluster.nameservers, [])) > 0 || try(var.cluster.dns_domain, null) != null ? [1] : []
      content {
        domain  = try(var.cluster.dns_domain, null)
        servers = try(var.cluster.nameservers, [])
      }
    }

    ip_config {
      ipv4 {
        address = "${each.value.ip}/${var.cluster.prefix}"
        gateway = var.cluster.gateway
      }
    }
  }

  network_device {
    bridge  = each.value.bridge
    model   = "virtio"
    vlan_id = each.value.vlan_id
  }

  operating_system {
    type = "l26"
  }

  serial_device {
  }
}
