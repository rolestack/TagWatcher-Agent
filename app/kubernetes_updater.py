import logging
from datetime import datetime, timezone

from kubernetes import client, config

logger = logging.getLogger(__name__)

# Tags that don't encode a version — need rollout restart to re-pull
_FIXED_TAGS = {"latest", "stable", "main", "master", "dev", "edge", "nightly"}


def _load_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _parse_container_id(container_id: str) -> tuple[str, str, str, str]:
    """
    Parse a container_id produced by kubernetes_reader into its parts.

    Format: {namespace}_{kind}_{workload}_{container}
    Since namespace/kind/container names cannot contain '_', and the workload
    name is the only field that could (rare, but possible via generateName edge
    cases), we split the fixed fields from the ends and treat the middle as the
    workload name.
    """
    parts = container_id.split("_")
    if len(parts) < 4:
        raise ValueError(f"Invalid container_id format: {container_id}")
    namespace = parts[0]
    kind = parts[1]
    container_name = parts[-1]
    workload = "_".join(parts[2:-1])
    return namespace, kind, workload, container_name


def _patch_image(apps_v1, v1, namespace: str, kind: str, name: str, container_name: str, new_image: str) -> None:
    container_patch = {"name": container_name, "image": new_image}
    if kind == "Pod":
        # Bare Pod has no template; patch the container image directly.
        # kubelet restarts the container in place with the new image.
        v1.patch_namespaced_pod(name, namespace, {"spec": {"containers": [container_patch]}})
        return

    patch = {"spec": {"template": {"spec": {"containers": [container_patch]}}}}
    if kind == "Deployment":
        apps_v1.patch_namespaced_deployment(name, namespace, patch)
    elif kind == "StatefulSet":
        apps_v1.patch_namespaced_stateful_set(name, namespace, patch)
    elif kind == "DaemonSet":
        apps_v1.patch_namespaced_daemon_set(name, namespace, patch)
    else:
        raise ValueError(f"Unsupported workload kind: {kind}")


def _rollout_restart(apps_v1, namespace: str, kind: str, name: str) -> None:
    """Force a rollout restart to re-pull a fixed tag (e.g. latest)."""
    now = datetime.now(timezone.utc).isoformat()
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now
                    }
                }
            }
        }
    }
    if kind == "Deployment":
        apps_v1.patch_namespaced_deployment(name, namespace, patch)
    elif kind == "StatefulSet":
        apps_v1.patch_namespaced_stateful_set(name, namespace, patch)
    elif kind == "DaemonSet":
        apps_v1.patch_namespaced_daemon_set(name, namespace, patch)


def apply_update(container_id: str, new_image: str) -> tuple[bool, str]:
    """
    Update a Kubernetes container image by patching the owning workload.

    container_id format: {namespace}_{kind}_{workload}_{container_name}

    The kind and workload are encoded in the container_id by kubernetes_reader,
    so no live ownerReference lookup is needed.

    If new_image pins a digest (repo:tag@sha256:...), the spec changes and the
    controller rolls automatically. Otherwise, a fixed tag (latest, etc.) needs a
    rollout restart to force a re-pull. Bare Pods are patched in place by kubelet.
    """
    try:
        namespace, kind, workload_name, container_name = _parse_container_id(container_id)
    except ValueError as e:
        return False, str(e)

    if kind not in ("Deployment", "StatefulSet", "DaemonSet", "Pod"):
        return False, f"Updates are not supported for workload kind '{kind}'"

    try:
        _load_config()
        apps_v1 = client.AppsV1Api()
        v1 = client.CoreV1Api()

        logger.info(f"Updating {kind}/{workload_name} [{container_name}] → {new_image}")
        _patch_image(apps_v1, v1, namespace, kind, workload_name, container_name, new_image)

        # A pinned digest already changed the spec → rollout is automatic.
        # Without a digest, a fixed tag needs an explicit rollout restart.
        # (Pod has no template, so it can't be rollout-restarted.)
        has_digest = "@sha256:" in new_image
        if not has_digest and kind != "Pod":
            tag = new_image.rsplit(":", 1)[-1]
            if tag in _FIXED_TAGS:
                logger.info(f"Fixed tag '{tag}' without digest — forcing rollout restart")
                _rollout_restart(apps_v1, namespace, kind, workload_name)

        return True, ""
    except Exception as e:
        logger.error(f"Failed to update {container_id}: {e}")
        return False, str(e)
