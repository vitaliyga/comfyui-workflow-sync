# ComfyUI S3 Sync

Lightweight web app that lives next to ComfyUI. Drop a workflow JSON →
it figures out which model files and custom-node packages the workflow needs →
diffs against what is already on disk → on a button click, runs
`aws s3 sync` to pull the missing pieces.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`brew install uv` / `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- AWS CLI v2 in `PATH`
- Read access to your S3 bucket (creds via env or `~/.aws/credentials`)

## Install

```bash
git clone <repo> comfyui-workflow-sync
cd comfyui-workflow-sync
uv sync
cp .env.example .env
$EDITOR .env       # fill in AWS keys, COMFYUI_PATH, S3 bases
$EDITOR config.yaml  # tweak node_packages / builtin_nodes for your install
```

`.env` keys:

| Var                       | Purpose                                                    |
| ------------------------- | ---------------------------------------------------------- |
| `AWS_ACCESS_KEY_ID`       | passed through to `aws` CLI                                |
| `AWS_SECRET_ACCESS_KEY`   | ditto                                                      |
| `AWS_DEFAULT_REGION`      | ditto                                                      |
| `AWS_ENDPOINT_URL`        | optional, for R2/MinIO/Wasabi                              |
| `COMFYUI_PATH`            | absolute path to ComfyUI install root                      |
| `S3_MODELS_BASE`          | base prefix where the `models/` layout lives               |
| `S3_NODES_BASE`           | base prefix for `custom_nodes/` (leave empty to disable)   |
| `SYNC_PARALLEL`           | optional, max concurrent `aws s3 sync` procs (default `3`) |
| `SYNC_STALL_SECS`         | optional, stalled-warning threshold (default `8`)          |
| `INDEX_TTL`               | optional, S3 index cache TTL in seconds (default `86400`)  |

## Run

```bash
uv run python main.py
```

Server listens on `0.0.0.0:8765`. Open <http://localhost:8765>.

## Autostart

### Linux (systemd)

Put this in `/etc/systemd/system/comfyui-sync.service`, then
`systemctl daemon-reload && systemctl enable --now comfyui-sync`.

```ini
[Unit]
Description=ComfyUI S3 Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=comfy
WorkingDirectory=/opt/comfyui-workflow-sync
ExecStart=/usr/local/bin/uv run python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`.env` next to `main.py` is auto-loaded — no need to pass env vars in the unit.

Logs: `journalctl -u comfyui-sync -f`.

### RunPod

RunPod pods don't run systemd, and the container is restarted on every
pod start — so you set up an autostart hook that runs every boot.

1. **One-time setup** (inside the pod terminal):

   ```bash
   cd /workspace
   git clone <repo> comfyui-workflow-sync
   cd comfyui-workflow-sync

   # uv + python deps
   curl -LsSf https://astral.sh/uv/install.sh | sh
   export PATH="$HOME/.local/bin:$PATH"
   uv sync

   # aws CLI v2 if not present
   apt-get update && apt-get install -y unzip
   curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/aws.zip
   unzip -q /tmp/aws.zip -d /tmp && /tmp/aws/install

   # .env — put it on the persistent volume so it survives pod restarts
   cp .env.example .env
   nano .env
   ```

   In `.env` set `COMFYUI_PATH=/workspace/ComfyUI` (or whatever path your pod
   template uses).

2. **Expose the port.** In the pod's "Edit Pod" settings add TCP port `8765`
   to **Expose HTTP Ports**. RunPod gives you a URL like
   `https://<pod-id>-8765.proxy.runpod.net`.

3. **Autostart on pod boot.** RunPod's official ComfyUI templates run a
   `start.sh`-style script — append a launcher to it, or put a hook in
   `/workspace/onstart.sh` and reference it from the pod's "Start Command".

   ```bash
   cat > /workspace/start-sync.sh <<'EOF'
   #!/usr/bin/env bash
   set -e
   export PATH="$HOME/.local/bin:$PATH"
   cd /workspace/comfyui-workflow-sync
   exec uv run python main.py >> /workspace/comfyui-sync.log 2>&1
   EOF
   chmod +x /workspace/start-sync.sh
   ```

   Then in the RunPod template **Container Start Command**, run both
   ComfyUI and this app:

   ```bash
   bash -lc 'cd /workspace/ComfyUI && python main.py --listen 0.0.0.0 & /workspace/start-sync.sh'
   ```

   Or if you don't want to edit the template, just run it in a detached
   `tmux` session after pod start:

   ```bash
   tmux new-session -d -s sync '/workspace/start-sync.sh'
   ```

4. **Logs:** `tail -f /workspace/comfyui-sync.log`.

Notes:
- Keep the repo + `.env` under `/workspace` (the only persistent volume on RunPod).
- The proxy URL is HTTPS and supports SSE — no extra config.
- `aws CLI` install needs to be re-run if your container template doesn't
  persist `/usr` between restarts. Easier: add the AWS-CLI install line to
  your start script (idempotent — `aws --version` check first).

### Reverse-proxy (optional)

For remote access put `nginx` / `caddy` in front. Minimal Caddyfile:

```
sync.example.com {
  reverse_proxy 127.0.0.1:8765
}
```

SSE streams work over HTTP/1.1 — no special config needed.

## How it works

- **S3 index** (auto-built, refreshed every 24h or via `POST /reindex`):
  - Models: one `aws s3 ls --recursive` under `$S3_MODELS_BASE`, builds
    `{filename: {folder, bytes, s3_url}}` keyed by both full path and basename.
  - Custom nodes: lists package dirs under `$S3_NODES_BASE`, fetches each
    package's `__init__.py` (and follows `from .X import NODE_CLASS_MAPPINGS`
    reexports), parses `NODE_CLASS_MAPPINGS` keys to build a
    `{class_name: package_dir}` map.
  - Cached to `.s3-index.json`. The whole bucket → ~5s for a few hundred models
    and a dozen packages.
- `POST /analyze` — parses workflow JSON (both standard and API-format),
  every string ending in `.safetensors`/`.gguf`/etc. is looked up in the
  models index; every node class name is looked up in the nodes index.
  `node_packages` in config is a fallback override for packages that don't
  register classes in a way the parser can detect.
- `POST /size` — for each S3 source: `aws s3 ls --summarize` for prefixes,
  exact-or-fuzzy lookup for files. Returns size + similar-named candidates
  when an exact match is missing (handles renamed lora versions).
- `POST /sync` — kicks off N parallel `aws s3 sync` procs (semaphore-limited),
  streams progress via SSE at `/sync/{job_id}/stream`. Parses `Completed X/Y`
  lines from aws output for byte-level progress.
- `DELETE /sync/{job_id}` — cancels by terminating running subprocesses.
- `/status` — local file listing + preflight checks (auth, bucket access,
  ComfyUI path, free disk).

## Scope notes

Out of scope by design: PNG workflow embed parsing, custom-node git install,
auth, multi-user. PRs welcome but check spec first.
