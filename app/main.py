import logging
import socket
import threading
import time

import httpx

from app.config import settings
from app.docker_reader import list_containers
from app.log_streamer import push_log_chunks
from app.registration import get_agent_secret
from app.updater import apply_update

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

AGENT_VERSION = "1.0.0"

# container_ids currently being streamed in a background thread
_active_log_streams: set[str] = set()
_active_log_streams_lock = threading.Lock()

# update results collected by background threads; flushed to TagWatcher on next sync
_update_results: list[dict] = []
_update_results_lock = threading.Lock()


def _apply_and_record(container_id: str, new_image: str, container_name: str) -> None:
    success, error = apply_update(container_id, new_image)
    with _update_results_lock:
        _update_results.append({
            "container_id": container_id,
            "container_name": container_name,
            "success": success,
            "error": error,
        })


def _log_stream_worker(agent_secret: str, container_ids: list[str]) -> None:
    """Push log chunks to TagWatcher every 5 s for up to 10 minutes."""
    with _active_log_streams_lock:
        _active_log_streams.update(container_ids)
    try:
        for _ in range(120):  # 120 × 5 s = 10 min
            with _active_log_streams_lock:
                active = [cid for cid in container_ids if cid in _active_log_streams]
            if not active:
                break
            push_log_chunks(agent_secret, active)
            time.sleep(5)
    finally:
        with _active_log_streams_lock:
            _active_log_streams.difference_update(container_ids)


def _push_sync(agent_secret: str) -> None:
    try:
        containers = list_containers()
    except Exception as e:
        logger.error(f"Failed to read containers from Docker socket: {e}")
        return

    with _update_results_lock:
        flushed_results = list(_update_results)
        _update_results.clear()

    payload = {
        "containers": [c.model_dump() for c in containers],
        "hostname": settings.AGENT_HOSTNAME or socket.gethostname(),
        "agent_version": AGENT_VERSION,
        "update_results": flushed_results,
    }
    url = settings.TAGWATCHER_URL.rstrip("/") + "/api/agent/sync"
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {agent_secret}"},
            timeout=30,
            verify=settings.tls_verify,
        )
        resp.raise_for_status()
        logger.debug(f"Pushed {len(containers)} container(s) to TagWatcher")

        data = resp.json()

        pending = data.get("pending_updates", [])
        for item in pending:
            logger.info(f"Applying update: {item['container_name']} → {item['new_image']}")
            threading.Thread(
                target=_apply_and_record,
                args=(item["container_id"], item["new_image"], item["container_name"]),
                daemon=True,
            ).start()

        request_logs = data.get("request_logs", [])
        if request_logs:
            with _active_log_streams_lock:
                new_streams = [cid for cid in request_logs if cid not in _active_log_streams]
            if new_streams:
                logger.info(f"Starting log stream for {len(new_streams)} container(s)")
                t = threading.Thread(
                    target=_log_stream_worker,
                    args=(agent_secret, new_streams),
                    daemon=True,
                )
                t.start()

    except httpx.HTTPStatusError as e:
        logger.error(f"Sync rejected by TagWatcher ({e.response.status_code}): {e.response.text}")
    except httpx.RequestError as e:
        logger.error(f"Could not reach TagWatcher at {settings.TAGWATCHER_URL}: {e}")


def main() -> None:
    agent_secret = get_agent_secret()
    interval = settings.SYNC_INTERVAL_SECONDS
    logger.info(f"TagWatcher Agent {AGENT_VERSION} started — syncing every {interval}s")

    while True:
        _push_sync(agent_secret)
        time.sleep(interval)


if __name__ == "__main__":
    main()
