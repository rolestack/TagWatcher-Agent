import logging
import socket
import time

import httpx

from app.config import settings
from app import state as _state

logger = logging.getLogger(__name__)

_RETRY_INITIAL_DELAY = 5
_RETRY_MAX_DELAY = 60


def _register_once() -> str:
    register_url = settings.TAGWATCHER_URL.rstrip("/") + "/api/agent/register"
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
    secret = resp.json()["agent_secret"]
    _state.save({
        "agent_secret": secret,
        "tagwatcher_url": settings.TAGWATCHER_URL,
    })
    return secret


def get_agent_secret() -> str:
    """Return the agent_secret, registering with TagWatcher if no state exists.

    Registration is retried indefinitely with exponential backoff so the agent
    survives the server being temporarily down or unreachable.
    """
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

    logger.info(f"Registering with TagWatcher at {settings.TAGWATCHER_URL} ...")

    delay = _RETRY_INITIAL_DELAY
    attempt = 0
    while True:
        attempt += 1
        try:
            secret = _register_once()
            logger.info(
                "Registration successful. "
                "You can now remove REGISTRATION_TOKEN — it is no longer needed."
            )
            return secret
        except httpx.HTTPStatusError as e:
            reason = f"{e.response.status_code} {e.response.reason_phrase}"
            if logger.isEnabledFor(logging.DEBUG):
                reason += f"\n{e.response.text}"
        except httpx.RequestError as e:
            reason = str(e)

        logger.warning(
            f"Registration attempt {attempt} failed: {reason}. Retrying in {delay}s..."
        )
        time.sleep(delay)
        delay = min(delay * 2, _RETRY_MAX_DELAY)
