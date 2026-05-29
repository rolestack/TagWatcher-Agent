from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # TagWatcher server URL (e.g. https://tagwatcher.example.com)
    TAGWATCHER_URL: str = ""

    # One-time registration token from TagWatcher web UI.
    # Can be removed from config after the agent registers successfully.
    REGISTRATION_TOKEN: str = ""

    # How often the agent pushes container data to TagWatcher (seconds).
    # Also determines the maximum delay before update requests from the TagWatcher UI are picked up.
    SYNC_INTERVAL_SECONDS: int = 30

    # Override the hostname reported to TagWatcher (default: auto-detected).
    # Useful when running the agent inside Docker, where the auto-detected
    # hostname is a container ID rather than the actual machine name.
    AGENT_HOSTNAME: str = ""

    # Directory for persistent state (agent_secret stored here after registration).
    DATA_DIR: str = "/data"

    LOG_LEVEL: str = "INFO"

    # Set to "false" to skip TLS certificate verification when contacting TagWatcher.
    # Useful for self-signed certificates.
    TLS_VERIFY: str = "true"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def tls_verify(self) -> bool:
        return self.TLS_VERIFY.lower() not in ("false", "0", "no")


settings = Settings()
