import logging
import socket

import httpx

from app.config import settings
from app import state as _state

logger = logging.getLogger(__name__)


def get_agent_secret() -> str:
    """Return the agent_secret, registering with TagWatcher if no state exists."""
    saved = _state.load()
    if saved and saved.get("agent_secret"):
        logger.info("Loaded existing registration from state file.")
        return saved["agent_secret"]

    if not settings.REGISTRATION_TOKEN:
        raise RuntimeError(
            "No existing registration found and REGISTRATION_TOKEN is not set.\n"
            "  1. In TagWatcher, go to Spaces → <Space> → Hosts → Add Host → Agent\n"
            "  2. Copy the registration token\n"
            "  3. Set REGISTRATION_TOKEN=<token> in your .env and restart the agent"
        )
    if not settings.TAGWATCHER_URL:
        raise RuntimeError("TAGWATCHER_URL must be set.")

    register_url = settings.TAGWATCHER_URL.rstrip("/") + "/api/agent/register"
    logger.info(f"Registering with TagWatcher at {settings.TAGWATCHER_URL} ...")

    try:
        resp = httpx.post(
            register_url,
            json={
                "token": settings.REGISTRATION_TOKEN,
                "hostname": socket.gethostname(),
            },
            timeout=30,
            verify=settings.tls_verify,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Registration failed ({e.response.status_code}): {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise RuntimeError(
            f"Could not reach TagWatcher at {settings.TAGWATCHER_URL}: {e}"
        ) from e

    secret = resp.json()["agent_secret"]
    _state.save({
        "agent_secret": secret,
        "tagwatcher_url": settings.TAGWATCHER_URL,
    })
    logger.info(
        "Registration successful. "
        "You can now remove REGISTRATION_TOKEN from your .env — it is no longer needed."
    )
    return secret
