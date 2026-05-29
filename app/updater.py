"""Container update logic — mirrors TagWatcher's docker_service recreate helpers."""
import logging
import subprocess

import docker

logger = logging.getLogger(__name__)


def _extract_bind_volumes(hcfg: dict) -> dict:
    volumes: dict = {}
    for bind in (hcfg.get("Binds") or []):
        parts = bind.split(":")
        if len(parts) < 2:
            continue
        mode = parts[2] if len(parts) > 2 else "rw"
        volumes[parts[0]] = {"bind": parts[1], "mode": mode}
    return volumes


def _extract_named_volumes(attrs: dict, existing: dict) -> dict:
    volumes: dict = {}
    for mount in (attrs.get("Mounts") or []):
        if mount.get("Type") != "volume":
            continue
        src = mount.get("Name") or mount.get("Source")
        if not src or src in existing:
            continue
        mode = "ro" if mount.get("RW") is False else "rw"
        volumes[src] = {"bind": mount["Destination"], "mode": mode}
    return volumes


def _extract_volumes(attrs: dict, hcfg: dict) -> dict:
    bind_vols = _extract_bind_volumes(hcfg)
    named_vols = _extract_named_volumes(attrs, bind_vols)
    return {**bind_vols, **named_vols}


def _extract_ports(hcfg: dict) -> dict:
    ports: dict = {}
    for port, bindings in (hcfg.get("PortBindings") or {}).items():
        ports[port] = [(b.get("HostIp", ""), b.get("HostPort", "")) for b in bindings] if bindings else None
    return ports


def _extract_restart_policy(hcfg: dict) -> dict | None:
    rp = hcfg.get("RestartPolicy") or {}
    name = rp.get("Name")
    if not name or name == "no":
        return None
    policy: dict = {"Name": name}
    if rp.get("MaximumRetryCount"):
        policy["MaximumRetryCount"] = rp["MaximumRetryCount"]
    return policy


def _extract_extra_hosts(hcfg: dict) -> dict:
    hosts: dict = {}
    for entry in (hcfg.get("ExtraHosts") or []):
        if ":" in entry:
            h, ip = entry.split(":", 1)
            hosts[h] = ip
    return hosts


def _build_create_kwargs(container, new_image: str) -> tuple[dict, list]:
    attrs = container.attrs
    cfg = attrs.get("Config", {})
    hcfg = attrs.get("HostConfig", {})
    net_settings = attrs.get("NetworkSettings", {})
    container_name = attrs.get("Name", "").lstrip("/")

    primary_network = hcfg.get("NetworkMode", "bridge")
    extra_networks = [n for n in (net_settings.get("Networks") or {}) if n != primary_network]

    create_kwargs: dict = {
        "image": new_image,
        "name": container_name,
        "detach": True,
        "environment": cfg.get("Env") or [],
        "command": cfg.get("Cmd"),
        "entrypoint": cfg.get("Entrypoint"),
        "user": cfg.get("User") or "",
        "working_dir": cfg.get("WorkingDir") or "",
        "labels": cfg.get("Labels") or {},
        "network": primary_network,
    }

    volumes = _extract_volumes(attrs, hcfg)
    ports = _extract_ports(hcfg)
    restart_policy = _extract_restart_policy(hcfg)
    extra_hosts = _extract_extra_hosts(hcfg)

    if volumes:
        create_kwargs["volumes"] = volumes
    if ports:
        create_kwargs["ports"] = ports
    if restart_policy:
        create_kwargs["restart_policy"] = restart_policy
    if extra_hosts:
        create_kwargs["extra_hosts"] = extra_hosts
    if hcfg.get("Privileged"):
        create_kwargs["privileged"] = True

    return create_kwargs, extra_networks


def _do_recreate(client, container, new_image: str, start: bool = True):
    container_name = container.attrs.get("Name", "").lstrip("/")
    create_kwargs, extra_networks = _build_create_kwargs(container, new_image)

    container.remove(v=False)

    logger.info(f"Creating new container: {container_name} with {new_image}")
    new_container = client.containers.create(**create_kwargs)

    for net_name in extra_networks:
        try:
            client.networks.get(net_name).connect(new_container)
        except Exception as e:
            logger.warning(f"Could not connect {container_name} to network {net_name}: {e}")

    if start:
        new_container.start()
        logger.info(f"Container {container_name} started with {new_image}")

    new_container.reload()
    return new_container


def apply_update(container_id: str, new_image: str) -> tuple[bool, str]:
    """Pull new_image and recreate the container, preserving its full configuration.

    Returns (success, error_message).
    """
    client = docker.from_env()
    try:
        container = client.containers.get(container_id)
        container_name = container.attrs.get("Name", "").lstrip("/")
        is_compose = "com.docker.compose.project" in (container.labels or {})

        if is_compose:
            _compose_update(client, container, new_image)
        else:
            was_running = container.status == "running"
            logger.info(f"Pulling {new_image} for container {container_name}")
            client.images.pull(new_image)
            logger.info(f"Stopping {container_name}")
            container.stop(timeout=30)
            _do_recreate(client, container, new_image, start=was_running)

        logger.info(f"Update complete: {container_name} → {new_image}")
        return True, ""
    except Exception as e:
        logger.error(f"Update failed for container {container_id} ({new_image}): {e}")
        return False, str(e)
    finally:
        client.close()


def _compose_update(client, target, new_image: str) -> None:
    """Use `docker compose up -d` so all compose-managed settings (devices, sysctls,
    capabilities, etc.) are preserved exactly as defined in the compose file."""
    labels = target.labels or {}
    service = labels.get("com.docker.compose.service")
    working_dir = labels.get("com.docker.compose.project.working_dir")
    config_files = labels.get("com.docker.compose.project.config_files", "")

    if not working_dir or not service:
        raise RuntimeError(
            f"Cannot determine compose context for '{target.name}': "
            f"working_dir={working_dir!r}, service={service!r}"
        )

    cmd = ["docker", "compose"]
    for f in (f.strip() for f in config_files.split(",") if f.strip()):
        cmd += ["-f", f]
    cmd += ["up", "-d", "--pull", "always", service]

    logger.info(f"Running: {' '.join(cmd)} (cwd={working_dir})")
    result = subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=300)
    if result.stdout:
        logger.debug(f"compose stdout: {result.stdout.strip()}")
    if result.returncode != 0:
        raise RuntimeError(f"docker compose up failed (rc={result.returncode}): {result.stderr.strip()}")
