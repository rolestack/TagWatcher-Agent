import logging
import os
from kubernetes import client, config

from app.schemas import ContainerInfo

logger = logging.getLogger(__name__)


def _split_image_ref(ref: str) -> tuple[str, str]:
    """Split image reference into name and tag."""
    if ":" in ref and not ref.startswith("sha256:"):
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, "latest"


def _extract_digest(image_id: str) -> str | None:
    """Extract digest from imageID (sha256:abc...)."""
    if image_id and image_id.startswith("sha256:"):
        return image_id
    return None


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
            # Scan entire cluster
            resp = v1.list_pod_for_all_namespaces(**kwargs)
            all_pods = resp.items
            logger.debug(f"Listed pods cluster-wide (label_selector={label_selector})")
        elif namespaces:
            # Scan specific namespaces
            for ns in namespaces:
                resp = v1.list_namespaced_pod(ns, **kwargs)
                all_pods.extend(resp.items)
            logger.debug(f"Listed pods in namespaces={namespaces} (label_selector={label_selector})")
        else:
            # Scan release namespace only
            ns = os.getenv("POD_NAMESPACE", "default")
            resp = v1.list_namespaced_pod(ns, **kwargs)
            all_pods = resp.items
            logger.debug(f"Listed pods in namespace={ns} (label_selector={label_selector})")
    except Exception as e:
        logger.error(f"Failed to list pods: {e}")
        return []

    result = []
    for pod in all_pods:
        pod_name = pod.metadata.name
        pod_namespace = pod.metadata.namespace
        for container_status in (pod.status.container_statuses or []):
            container_name = container_status.name
            image_ref = container_status.image
            image_id = container_status.image_id or ""

            image_name, tag = _split_image_ref(image_ref)
            digest = _extract_digest(image_id)

            if container_status.state.running:
                status = "running"
            elif container_status.state.waiting:
                status = "waiting"
            elif container_status.state.terminated:
                status = "terminated"
            else:
                status = "unknown"

            container_id = f"{pod_namespace}_{pod_name}_{container_name}"

            result.append(ContainerInfo(
                container_id=container_id,
                name=f"{pod_namespace}/{pod_name}/{container_name}",
                image=image_name,
                tag=tag,
                digest=digest,
                status=status,
                labels=pod.metadata.labels or {},
            ))

    return result
