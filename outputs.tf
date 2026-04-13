output "cluster_name" {
  value = var.cluster.name
}

output "controlplane_ips" {
  value = [for name in sort(keys(local.controlplane_nodes)) : local.controlplane_nodes[name].ip]
}

output "worker_ips" {
  value = [for name in sort(keys(local.worker_nodes)) : local.worker_nodes[name].ip]
}

output "highly_available" {
  value = local.highly_available
}

output "kubernetes_endpoint" {
  value = local.kubernetes_endpoint
}

output "talos_image_url" {
  value = local.talos_image_url
}

output "node_vm_ids" {
  value = { for name, node in local.normalized_nodes : name => node.vm_id }
}
