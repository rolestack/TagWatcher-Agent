import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_runtime() -> tuple[str, str]:
    """
    Detect whether we're running in Docker or Kubernetes.

    Returns:
        (runtime_type, runtime_metadata)
        runtime_type: "docker" or "kubernetes"
        runtime_metadata: JSON string with runtime-specific info
    """
    # Check if running in Kubernetes
    # Kubernetes injects service account files and environment variables
    if _is_kubernetes():
        namespace = _get_k8s_namespace()
        metadata = f'{{"namespace": "{namespace}"}}'
        logger.info(f"Detected Kubernetes runtime (namespace: {namespace})")
        return "kubernetes", metadata

    # Default to Docker
    logger.info("Detected Docker runtime")
    return "docker", "{}"


def _is_kubernetes() -> bool:
    """Check if we're running inside a Kubernetes pod."""
    # Kubernetes mounts service account token
    if Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists():
        return True

    # Check for Kubernetes environment variables
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return True

    return False


def _get_k8s_namespace() -> str:
    """Get the current Kubernetes namespace."""
    # Kubernetes mounts namespace file
    ns_file = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if ns_file.exists():
        try:
            return ns_file.read_text().strip()
        except Exception:
            pass

    # Fallback to environment variable or default
    return os.getenv("POD_NAMESPACE", "default")
