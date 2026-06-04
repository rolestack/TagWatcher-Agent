import json
import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_STATE_FILE = Path(settings.DATA_DIR) / "agent.json"


def load() -> dict | None:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception as e:
            logger.warning(f"Failed to read state file: {e}")
    return None


def save(data: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, indent=2))
    _STATE_FILE.chmod(0o600)
    logger.debug(f"State saved to {_STATE_FILE}")
