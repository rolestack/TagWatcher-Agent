import logging
import docker
import docker.errors

from app.schemas import ContainerInfo

logger = logging.getLogger(__name__)


def _split_image_ref(ref: str) -> tuple[str, str]:
    if ":" in ref and not ref.startswith("sha256:"):
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, "latest"


def _extract_digest(image_attrs: dict) -> str | None:
    for rd in (image_attrs.get("RepoDigests") or []):
        if "@" in rd:
            return rd.split("@", 1)[1]
    return None


def list_containers() -> list[ContainerInfo]:
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    result = []
    try:
        for c in client.containers.list(all=True):
            if c.image.tags:
                image_ref = c.image.tags[0]
            else:
                # Config.Image holds the original reference (e.g. "nginx:latest") even after
                # the local image has been replaced/untagged. Fall back to sha256 ID only as
                # a last resort so we never send a corrupted sha256:...:latest:latest ref.
                config_img = (c.attrs.get("Config") or {}).get("Image", "")
                image_ref = config_img if (config_img and not config_img.startswith("sha256:")) else (c.image.id or "unknown")
            image_name, tag = _split_image_ref(image_ref)
            try:
                digest = _extract_digest(c.image.attrs)
            except Exception:
                digest = None
            result.append(ContainerInfo(
                container_id=c.short_id,
                name=c.name.lstrip("/"),
                image=image_name,
                tag=tag,
                digest=digest,
                status=c.status,
                labels=dict(c.labels or {}),
            ))
    finally:
        client.close()
    return result
