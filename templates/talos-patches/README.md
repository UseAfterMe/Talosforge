# Talos Patches

This directory is reserved for Talos machine-config patches that should stay small and composable.

Expected early uses:

- shared cluster patches
- control-plane VIP patching
- per-role kubelet settings
- Cilium-related early bootstrap adjustments, if needed

The intent is to keep patches explicit rather than burying all customization inside a single giant template.
