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
    List all containers in the current Kubernetes namespace.

    Uses in-cluster configuration (service account token).
    """
    try:
        # Load in-cluster config (works when running inside a pod)
        config.load_incluster_config()
    except config.ConfigException:
        logger.warning("Failed to load in-cluster config, falling back to kubeconfig")
        config.load_kube_config()

    v1 = client.CoreV1Api()
    namespace = os.getenv("POD_NAMESPACE", "default")

    # Get label selector from environment (optional filtering)
    label_selector = os.getenv("K8S_LABEL_SELECTOR", "")

    try:
        if label_selector:
            pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
        else:
            pods = v1.list_namespaced_pod(namespace)
    except Exception as e:
        logger.error(f"Failed to list pods in namespace {namespace}: {e}")
        return []

    result = []
    for pod in pods.items:
        pod_name = pod.metadata.name
        for container_status in (pod.status.container_statuses or []):
            container_name = container_status.name
            image_ref = container_status.image
            image_id = container_status.image_id or ""

            image_name, tag = _split_image_ref(image_ref)
            digest = _extract_digest(image_id)

            # Container status: running, waiting, terminated
            if container_status.state.running:
                status = "running"
            elif container_status.state.waiting:
                status = "waiting"
            elif container_status.state.terminated:
                status = "terminated"
            else:
                status = "unknown"

            # Use pod_name + container_name as unique ID
            container_id = f"{pod_name}_{container_name}"

            result.append(ContainerInfo(
                container_id=container_id,
                name=f"{pod_name}/{container_name}",
                image=image_name,
                tag=tag,
                digest=digest,
                status=status,
                labels=pod.metadata.labels or {},
            ))

    return result
