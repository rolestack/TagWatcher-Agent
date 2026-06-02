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


def _find_owner(v1, apps_v1, namespace: str, pod_name: str) -> tuple[str, str]:
    """
    Trace pod ownerReferences to find the root workload.
    Returns (kind, name): Deployment, StatefulSet, or DaemonSet.
    """
    pod = v1.read_namespaced_pod(pod_name, namespace)

    for ref in (pod.metadata.owner_references or []):
        if ref.kind == "ReplicaSet":
            rs = apps_v1.read_namespaced_replica_set(ref.name, namespace)
            for rs_ref in (rs.metadata.owner_references or []):
                if rs_ref.kind == "Deployment":
                    return "Deployment", rs_ref.name
            return "ReplicaSet", ref.name
        if ref.kind in ("StatefulSet", "DaemonSet"):
            return ref.kind, ref.name

    raise ValueError(f"No supported workload owner found for pod '{pod_name}'")


def _patch_image(apps_v1, namespace: str, kind: str, name: str, container_name: str, new_image: str) -> None:
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": container_name, "image": new_image}]
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
    elif kind == "ReplicaSet":
        apps_v1.patch_namespaced_replica_set(name, namespace, patch)
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

    container_id format: {namespace}_{pod_name}_{container_name}

    For versioned tags (e.g. 1.0.0 → 1.1.0): patches the image in the workload spec.
    For fixed tags (latest, stable, etc.): patches the image AND forces a rollout
    restart so Kubernetes re-pulls the image even if the tag name hasn't changed.
    """
    parts = container_id.split("_", 2)
    if len(parts) != 3:
        return False, f"Invalid container_id format: {container_id}"

    namespace, pod_name, container_name = parts

    try:
        _load_config()
        v1 = client.CoreV1Api()
        apps_v1 = client.AppsV1Api()

        kind, workload_name = _find_owner(v1, apps_v1, namespace, pod_name)
        logger.info(f"Updating {kind}/{workload_name} [{container_name}] → {new_image}")

        _patch_image(apps_v1, namespace, kind, workload_name, container_name, new_image)

        # Fixed tags require a rollout restart to force re-pull
        tag = new_image.rsplit(":", 1)[-1] if ":" in new_image else "latest"
        if tag in _FIXED_TAGS:
            logger.info(f"Fixed tag '{tag}' detected — forcing rollout restart")
            _rollout_restart(apps_v1, namespace, kind, workload_name)

        return True, ""
    except Exception as e:
        logger.error(f"Failed to update {container_id}: {e}")
        return False, str(e)
