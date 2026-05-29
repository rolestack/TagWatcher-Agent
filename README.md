# TagWatcher Agent

A lightweight agent that runs on a Docker host and pushes container data to [TagWatcher](https://github.com/rolestack/TagWatcher).

Use the agent when you cannot expose the Docker TCP port or mount the Docker socket directly into TagWatcher — for example, on a remote server in a different network.

## How It Works

```
TagWatcher (server)              TagWatcher-Agent (remote host)
        |                                   |
        |  <── POST /api/agent/sync ────    |  (every 30s)
        |      container list, digests      |
        |                                   |
        |  ──── pending_updates ──────>     |  apply update (docker compose up -d)
        |  ──── request_logs ─────────>     |  stream logs back
        |                                   |
        |  <── POST /api/agent/log-data ─   |  (live log chunks)
```

The agent **pushes** data to TagWatcher on a configurable interval. There is no inbound connection to the agent — only outbound HTTPS.

---

## Quick Start

### 1. Add an Agent host in TagWatcher

In the TagWatcher UI, go to a Space → **Hosts** → **Add Host** → select type **Agent**.

Copy the generated **Registration Token**. It is valid for 24 hours and single-use.

### 2. Create the environment file

```bash
cp .env.example .env
```

Edit `.env`:

```env
TAGWATCHER_URL=https://tagwatcher.example.com
REGISTRATION_TOKEN=paste-token-here
AGENT_HOSTNAME=my-server
```

`AGENT_HOSTNAME` is the display name shown in TagWatcher. Defaults to the system hostname if not set.

### 3. Start the agent

```bash
docker compose up -d
```

The agent registers with TagWatcher on first startup, discards the registration token, and saves a persistent secret to `/data/agent_secret`. Mount `/data` as a named volume (already in the provided `docker-compose.yml`) so the secret survives container restarts.

---

## docker-compose.yml

```yaml
services:
  tagwatcher-agent:
    image: tagwatcher-agent:latest
    build: .
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - tagwatcher-agent-data:/data
    env_file: .env

volumes:
  tagwatcher-agent-data:
```

> The Docker socket is mounted **read-only** for container discovery. Write access is granted separately via the host's Docker CLI for apply-update operations.

---

## Environment Variables

| Variable | Default | Required | Description |
|----------|---------|:--------:|-------------|
| `TAGWATCHER_URL` | — | ✅ | Base URL of the TagWatcher server (e.g. `https://tagwatcher.example.com`). |
| `REGISTRATION_TOKEN` | — | ✅ (first run) | One-time token from the TagWatcher UI. Can be removed after the agent registers. |
| `AGENT_HOSTNAME` | *(system hostname)* | | Display name shown in TagWatcher. Set this when the auto-detected hostname is a container ID. |
| `SYNC_INTERVAL_SECONDS` | `30` | | How often the agent pushes container data. Also controls the maximum delay before an Apply Update request is picked up. |
| `DATA_DIR` | `/data` | | Directory for persistent state (`agent_secret`). Mount this as a volume. |
| `LOG_LEVEL` | `INFO` | | Log level: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `TLS_VERIFY` | `true` | | Set to `false` to skip TLS certificate verification (useful for self-signed certs). |

---

## Registration Flow

```
First run:
  Agent ──► POST /api/agent/register { token, hostname }
         ◄── { agent_secret }
  Agent saves agent_secret to /data/agent_secret
  REGISTRATION_TOKEN is no longer needed

Subsequent runs:
  Agent reads agent_secret from /data/agent_secret
  Agent ──► POST /api/agent/sync  (Bearer agent_secret)
         ◄── { pending_updates, request_logs }
```

If the agent secret is lost (e.g. volume deleted), go to **Edit Host** in TagWatcher and click **Rotate Token** to generate a new registration token.

---

## Apply Update

When you click **Apply Update** in the TagWatcher UI for an agent host:

1. TagWatcher queues the update internally.
2. On the next sync, the queue is returned to the agent in `pending_updates`.
3. The agent applies the update in a background thread:
   - **Compose containers:** runs `docker compose up -d --pull always <service>` in the original working directory — all compose settings (devices, volumes, networks, etc.) are preserved.
   - **Standalone containers:** pulls the new image and recreates the container with the same configuration.
4. The result (success or error) is sent back to TagWatcher on the next sync.

---

## IP Allow List

You can restrict which IP addresses are allowed to call the agent API from the **Edit Host** page in TagWatcher (under **IP Allow List**). Accepts CIDR notation, one entry per line. Default `0.0.0.0/0` allows all.

---

## Live Logs

When you open the **Logs** tab for a container on an agent host, TagWatcher opens a WebSocket and requests log streaming from the agent. The agent starts pushing log chunks every 5 seconds for up to 10 minutes.

---

## License

Private
