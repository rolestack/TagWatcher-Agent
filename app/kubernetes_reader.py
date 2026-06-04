import logging
import os
from kubernetes import client, config

from app.schemas import ContainerInfo

logger = logging.getLogger(__name__)

# Maps stable container_id → (namespace, pod_name, container_name)
# Updated on every sync cycle, used by log_streamer to find actual pod.
_pod_map: dict[str, tuple[str, str, str]] = {}


def _split_image_ref(ref: str) -> tuple[str, str]:
    """Split an image reference into (name, tag).

    Strips a pinned digest first: "repo:tag@sha256:..." → "repo:tag".
    The tag is read straight from the reference — no "latest" guessing.
    """
    if "@" in ref:
        ref = ref.split("@", 1)[0]
    if ref.startswith("sha256:"):
        return ref, ""
    if ":" in ref:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, ""


def _extract_digest(image_id: str) -> str | None:
    """Extract the sha256 digest from a Kubernetes imageID.

    imageID can look like:
      "repo@sha256:abc..."                 → "sha256:abc..."
      "docker-pullable://repo@sha256:abc"  → "sha256:abc..."
      "sha256:abc..."                      → "sha256:abc..."
    """
    if not image_id:
        return None
    if "@" in image_id:
        return image_id.rsplit("@", 1)[1]
    if image_id.startswith("sha256:"):
        return image_id
    return None


def _build_rs_to_deployment_map(apps_v1, namespaces_needed: set[str]) -> dict[tuple[str, str], str]:
    """
    Build a mapping of (namespace, replicaset_name) → deployment_name.
    Used to resolve stable workload identity for Deployment pods.
    """
    rs_map: dict[tuple[str, str], str] = {}
    try:
        if namespaces_needed:
            for ns in namespaces_needed:
                resp = apps_v1.list_namespaced_replica_set(ns)
                for rs in resp.items:
                    for ref in (rs.metadata.owner_references or []):
                        if ref.kind == "Deployment":
                            rs_map[(ns, rs.metadata.name)] = ref.name
        else:
            resp = apps_v1.list_replica_set_for_all_namespaces()
            for rs in resp.items:
                for ref in (rs.metadata.owner_references or []):
                    if ref.kind == "Deployment":
                        rs_map[(rs.metadata.namespace, rs.metadata.name)] = ref.name
    except Exception as e:
        logger.warning(f"Failed to build ReplicaSet→Deployment map: {e}")
    return rs_map


def _workload_id(pod, rs_map: dict) -> tuple[str, str]:
    """
    Derive a stable workload (kind, name) from pod ownerReferences.

    Including the kind prevents collisions when, e.g., a StatefulSet and a
    Deployment in the same namespace share a name.

    - StatefulSet / DaemonSet → (kind, workload name)  [pod name is already stable]
    - Deployment (via ReplicaSet) → ("Deployment", deployment name)
    - standalone ReplicaSet → ("ReplicaSet", rs name)
    - Job → ("Job", job name)
    - Bare pod / unknown → ("Pod", pod name)
    """
    namespace = pod.metadata.namespace
    for ref in (pod.metadata.owner_references or []):
        if ref.kind in ("StatefulSet", "DaemonSet"):
            return ref.kind, ref.name
        if ref.kind == "ReplicaSet":
            deployment = rs_map.get((namespace, ref.name))
            if deployment:
                return "Deployment", deployment
            return "ReplicaSet", ref.name
        if ref.kind == "Job":
            return "Job", ref.name
    return "Pod", pod.metadata.name


def list_containers() -> list[ContainerInfo]:
    """
    List containers from Kubernetes.

    Behavior is controlled by environment variables (set via Helm values):
    - K8S_CLUSTER_WIDE=true (default): scan all namespaces cluster-wide
    - K8S_CLUSTER_WIDE=false + K8S_NAMESPACE: scan a specific namespace only
    - K8S_LABEL_SELECTOR: optional label filter (e.g. "app=myapp")
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        logger.warning("Failed to load in-cluster config, falling back to kubeconfig")
        config.load_kube_config()

    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    cluster_wide = os.getenv("K8S_CLUSTER_WIDE", "true").lower() != "false"
    namespaces_raw = os.getenv("K8S_NAMESPACES", "")
    namespaces = [ns.strip() for ns in namespaces_raw.split(",") if ns.strip()] if namespaces_raw else []
    label_selector = os.getenv("K8S_LABEL_SELECTOR", "") or None

    kwargs = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    all_pods = []
    try:
        if cluster_wide and not namespaces:
            resp = v1.list_pod_for_all_namespaces(**kwargs)
            all_pods = resp.items
            logger.debug(f"Listed pods cluster-wide (label_selector={label_selector})")
        elif namespaces:
            for ns in namespaces:
                resp = v1.list_namespaced_pod(ns, **kwargs)
                all_pods.extend(resp.items)
            logger.debug(f"Listed pods in namespaces={namespaces} (label_selector={label_selector})")
        else:
            ns = os.getenv("POD_NAMESPACE", "default")
            resp = v1.list_namespaced_pod(ns, **kwargs)
            all_pods = resp.items
            logger.debug(f"Listed pods in namespace={ns} (label_selector={label_selector})")
    except Exception as e:
        logger.error(f"Failed to list pods: {e}")
        return []

    # Collect namespaces that have ReplicaSet-owned pods for batch RS lookup
    rs_namespaces = {
        pod.metadata.namespace
        for pod in all_pods
        if any(ref.kind == "ReplicaSet" for ref in (pod.metadata.owner_references or []))
    }
    rs_map = _build_rs_to_deployment_map(
        apps_v1,
        rs_namespaces if not cluster_wide or namespaces else set(),
    )

    global _pod_map
    new_pod_map: dict[str, tuple[str, str, str]] = {}

    result = []
    for pod in all_pods:
        pod_name = pod.metadata.name
        pod_namespace = pod.metadata.namespace

        # Skip pods that are being deleted (Terminating)
        if pod.metadata.deletion_timestamp is not None:
            continue

        workload_kind, workload_name = _workload_id(pod, rs_map)

        # spec.image is the intended reference (repo:tag[@digest]); status.image
        # can be resolved to a bare digest by the runtime, losing the tag.
        spec_images = {c.name: c.image for c in (pod.spec.containers or [])}

        for container_status in (pod.status.container_statuses or []):
            container_name = container_status.name
            image_ref = spec_images.get(container_name) or container_status.image
            image_id = container_status.image_id or ""

            if container_status.state.running:
                status = "running"
            elif container_status.state.waiting:
                status = "waiting"
            elif container_status.state.terminated:
                continue
            else:
                continue

            image_name, tag = _split_image_ref(image_ref)
            digest = _extract_digest(image_id)

            # container_id uses stable {namespace}_{kind}_{workload}_{container},
            # so it survives pod restarts and avoids cross-kind name collisions.
            container_id = f"{pod_namespace}_{workload_kind}_{workload_name}_{container_name}"
            new_pod_map[container_id] = (pod_namespace, pod_name, container_name)

            result.append(ContainerInfo(
                container_id=container_id,
                name=workload_name,
                namespace=pod_namespace,
                image=image_name,
                tag=tag,
                digest=digest,
                status=status,
                labels=pod.metadata.labels or {},
            ))

    _pod_map = new_pod_map
    return result
