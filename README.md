# TagWatcher Agent

A lightweight agent that runs on a **Docker host or Kubernetes cluster** and pushes container data to [TagWatcher](https://github.com/rolestack/TagWatcher).

The agent **auto-detects** its runtime: on Docker it talks to the Docker socket, in Kubernetes it uses the in-cluster API. No separate image — the same agent handles both.

## How It Works

```
TagWatcher (server)              TagWatcher-Agent (remote host)
        |                                   |
        |  <── POST /api/agent/sync ────    |  (every 30s)
        |      container list, digests      |
        |                                   |
        |  ──── pending_updates ──────>     |  apply update
        |  ──── request_logs ─────────>     |  stream logs back
        |                                   |
        |  <── POST /api/agent/log-data ─   |  (live log chunks)
```

The agent **pushes** data to TagWatcher on a configurable interval. There is no inbound connection to the agent — only outbound HTTPS.

---

## Add an Agent Host

This step is the same for Docker and Kubernetes.

In the TagWatcher UI, go to a Space → **Hosts** → **Add Host** → select type **Agent**.

Copy the generated **Registration Token** — you'll pass it to the agent below. It is valid for 24 hours.

---

## Docker

### 1. Configure

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

### 2. Run

```bash
docker compose up -d
```

The agent registers on first startup and saves a persistent secret to `/data`. The provided `docker-compose.yml` mounts `/data` as a named volume so the secret survives restarts.

### docker-compose.yml

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

## Kubernetes (Helm)

In a cluster the agent discovers pods via the Kubernetes API and applies updates as **rolling updates**. Workload identity (Deployment/StatefulSet/DaemonSet/Pod) is resolved automatically, so it survives pod restarts.

### 1. Add the Helm repository

```bash
helm repo add tagwatcher https://rolestack.github.io/Tagwatcher-Agent
helm repo update
```

### 2. Create `values.yaml`

```yaml
tagwatcher:
  url: https://tagwatcher.example.com

secret:
  registrationToken: "<registration-token>"

kubernetes:
  # Scan all namespaces (creates a ClusterRole). Default.
  clusterWide: true

  # Or restrict to specific namespaces (uses namespace-scoped Roles):
  # clusterWide: false
  # namespaces:
  #   - production
  #   - staging
```

See [`values.yaml`](helm/tagwatcher-agent/values.yaml) for all options — timezone, label selector, resources, using an existing Secret, etc.

### 3. Install

```bash
helm install tagwatcher-agent tagwatcher/tagwatcher-agent \
  --namespace watcher --create-namespace \
  -f values.yaml
```

Apply changes later by editing `values.yaml` and running:

```bash
helm upgrade tagwatcher-agent tagwatcher/tagwatcher-agent -n watcher -f values.yaml
```

### RBAC (auto-created)

| Resource | Verbs | Purpose |
|---|---|---|
| `pods` | get/list/watch/patch | discovery + bare-Pod image update |
| `pods/log` | get | live logs |
| `replicasets` | get/list | trace Pod → Deployment |
| `deployments`, `statefulsets`, `daemonsets` | get/list/patch | rolling update |

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

> On Kubernetes these are set via Helm `values.yaml`, not a `.env` file.

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

When you click **Apply Update** in the TagWatcher UI for a Docker agent host:

1. TagWatcher queues the update internally.
2. On the next sync, the queue is returned to the agent in `pending_updates`.
3. The agent applies the update in a background thread:
   - **Compose containers:** runs `docker compose up -d --pull always <service>` in the original working directory — all compose settings (devices, volumes, networks, etc.) are preserved.
   - **Standalone containers:** pulls the new image and recreates the container with the same configuration.
4. The result (success or error) is sent back to TagWatcher on the next sync.

On **Kubernetes** the button is labeled **Rolling Update** and applies via the API:

- **Version tag** (`1.0.0` → `1.1.0`): patches the workload image → controller rolls.
- **Fixed tag** (`latest`, etc.): the server pins the digest (`repo:tag@sha256:...`) so the spec changes and a rollout is triggered even though the tag name is unchanged.
- **Bare Pod**: the image is patched in place; kubelet restarts the container.
- Job/CronJob have no rolling concept, so the update button is hidden for them.

---

## IP Allow List

You can restrict which IP addresses are allowed to call the agent API from the **Edit Host** page in TagWatcher (under **IP Allow List**). Accepts CIDR notation, one entry per line. Default `0.0.0.0/0` allows all.

---

## Live Logs

When you open the **Logs** tab for a container on an agent host, TagWatcher opens a WebSocket and requests log streaming from the agent. The agent starts pushing log chunks every 5 seconds for up to 10 minutes.
