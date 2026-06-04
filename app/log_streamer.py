"""Fetch container logs and push them to TagWatcher as streaming chunks."""
import logging
import re
import time as _time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# container_id → unix timestamp of last log fetch (used to fetch only new lines)
_last_log_since: dict[str, float] = {}

# Number of recent lines to load on the first fetch (keeps the viewer fast).
_FIRST_FETCH_TAIL = 50

# The RFC3339 timestamp Kubernetes prepends when timestamps=True.
_K8S_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?$")

# Common app-emitted timestamp formats, searched near the start of the line.
_OWN_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"          # ISO 8601
    r"|\[\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}"     # nginx / Apache CLF
    r"|[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"       # syslog
)


def _normalize_k8s_line(line: str) -> str:
    parts = line.split(" ", 1)
    if len(parts) == 2 and _K8S_TS.match(parts[0]):
        body = parts[1]
        return body if _OWN_TS.search(body[:48]) else line
    return line


def _fetch_docker_logs(container_ids: list[str]) -> list[dict]:
    import docker
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
                logger.debug(f"Failed to fetch Docker logs for {cid}: {e}")
    finally:
        client.close()
    return chunks


def _fetch_k8s_logs(container_ids: list[str]) -> list[dict]:
    from kubernetes import client as k8s_client, config as k8s_config
    from app.kubernetes_reader import _pod_map

    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()

    v1 = k8s_client.CoreV1Api()
    chunks = []

    for cid in container_ids:
        pod_info = _pod_map.get(cid)
        if not pod_info:
            logger.warning(
                f"No pod mapping for container_id '{cid}'. "
                f"Known keys: {list(_pod_map.keys())[:10]}"
            )
            continue

        namespace, pod_name, container_name = pod_info
        since = _last_log_since.get(cid, 0)
        _last_log_since[cid] = _time.time()

        try:
            kwargs = {
                "name": pod_name,
                "namespace": namespace,
                "container": container_name,
                "timestamps": True,
            }
            if since == 0:
                kwargs["tail_lines"] = _FIRST_FETCH_TAIL
            else:
                kwargs["since_seconds"] = max(1, int(_time.time() - since) + 2)

            resp = v1.read_namespaced_pod_log(**kwargs, _preload_content=False)
            try:
                data = resp.read()
            finally:
                resp.release_conn()
            raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
            lines = [_normalize_k8s_line(ln) for ln in raw.splitlines()] if raw else []
            logger.debug(f"Fetched {len(lines)} K8s log line(s) for {namespace}/{pod_name}/{container_name}")
            if lines:
                chunks.append({"container_id": cid, "lines": lines})
        except Exception as e:
            logger.warning(f"Failed to fetch K8s logs for {namespace}/{pod_name}/{container_name}: {e}")

    return chunks


def reset_since(container_ids: list[str]) -> None:
    """Forget the last-fetch position so the next fetch starts from the tail again."""
    for cid in container_ids:
        _last_log_since.pop(cid, None)


def push_log_chunks(agent_secret: str, container_ids: list[str], runtime_type: str) -> None:
    """Fetch recent logs for each container and POST them to TagWatcher."""
    if runtime_type == "kubernetes":
        chunks = _fetch_k8s_logs(container_ids)
    else:
        chunks = _fetch_docker_logs(container_ids)

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
