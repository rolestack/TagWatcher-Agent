"""Fetch container logs and push them to TagWatcher as streaming chunks."""
import logging
import time as _time

import docker
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# container_id → unix timestamp of last log fetch (used to fetch only new lines)
_last_log_since: dict[str, float] = {}


def push_log_chunks(agent_secret: str, container_ids: list[str]) -> None:
    """Fetch recent logs for each container and POST them to TagWatcher."""
    client = docker.from_env()
    chunks = []
    try:
        for cid in container_ids:
            try:
                c = client.containers.get(cid)
                since = _last_log_since.get(cid, 0)
                _last_log_since[cid] = _time.time()
                if since == 0:
                    raw = c.logs(tail=200, timestamps=True)
                else:
                    raw = c.logs(since=int(since), timestamps=True)
                lines = raw.decode("utf-8", errors="replace").splitlines()
                if lines:
                    chunks.append({"container_id": cid, "lines": lines})
            except Exception as e:
                logger.debug(f"Failed to fetch logs for container {cid}: {e}")
    finally:
        client.close()

    if not chunks:
        return

    url = settings.TAGWATCHER_URL.rstrip("/") + "/api/agent/log-data"
    try:
        httpx.post(
            url,
            json={"chunks": chunks},
            headers={"Authorization": f"Bearer {agent_secret}"},
            timeout=15,
            verify=settings.tls_verify,
        )
    except Exception as e:
        logger.debug(f"Failed to push log data to TagWatcher: {e}")
