variable "proxmox" {
  description = "Proxmox API and default infrastructure settings."

  type = object({
    endpoint                 = string
    insecure                 = optional(bool, false)
    username                 = string
    cluster_name             = optional(string)
    node_default             = string
    image_datastore          = string
    vm_datastore             = string
    initialization_datastore = optional(string)
    network_bridge           = string
    vlan_id                  = optional(number)
    tags                     = optional(list(string), [])
  })
}

variable "proxmox_password" {
  description = "Proxmox API password supplied via environment or prompt."
  type        = string
  sensitive   = true
}

variable "cluster" {
  description = "Talos cluster settings shared by all nodes."

  type = object({
    name               = string
    gateway            = string
    prefix             = number
    dns_domain         = optional(string, "cluster.local")
    nameservers        = optional(list(string), [])
    talos_version      = string
    kubernetes_version = optional(string)
    api_vip            = optional(string)
    install_disk       = string
    network_interface  = optional(string, "eth0")
    additional_sans    = optional(list(string), [])
  })
}

variable "image" {
  description = "Talos Image Factory inputs."

  type = object({
    schematic_id     = optional(string)
    schematic_file   = optional(string)
    factory_base_url = optional(string, "https://factory.talos.dev")
    arch             = optional(string, "amd64")
    platform         = optional(string, "nocloud")
    update_strategy  = optional(string, "recreate")
    extensions       = optional(list(string), [])
  })

  default = {}
}

variable "addons" {
  description = "Optional cluster add-ons."

  type = object({
    cilium_enabled            = optional(bool, true)
    cilium_chart_version      = optional(string, "1.19.1")
    metallb_enabled           = optional(bool, false)
    metallb_chart_version     = optional(string, "0.15.3")
    metallb_pools             = optional(list(string), [])
    traefik_enabled           = optional(bool, false)
    traefik_chart_version     = optional(string, "39.0.7")
    proxmox_csi_enabled       = optional(bool, false)
    proxmox_csi_chart_version = optional(string, "0.18.0")
    proxmox_csi_storage       = optional(string, "")
  })

  default = {}
}

variable "nodes" {
  description = "Talos VM definitions keyed by node name."

  type = map(object({
    role              = string
    host_node         = optional(string)
    vm_id             = number
    ip                = string
    cpu               = number
    memory_mb         = number
    disk_gb           = number
    datastore_id      = optional(string)
    bridge            = optional(string)
    vlan_id           = optional(number)
    network_interface = optional(string)
    start_on_boot     = optional(bool, true)
    tags              = optional(list(string), [])
  }))

  validation {
    condition = alltrue([
      for _, node in var.nodes : contains(["controlplane", "worker"], node.role)
    ])
    error_message = "nodes[*].role must be either controlplane or worker."
  }
}
