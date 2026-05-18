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

### macOS (launchd)

Put this in `~/Library/LaunchAgents/com.local.comfyui-sync.plist`, then
`launchctl load -w ~/Library/LaunchAgents/com.local.comfyui-sync.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.local.comfyui-sync</string>
  <key>WorkingDirectory</key><string>/Users/mac/Scripts/comfyui-workflow-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/uv</string>
    <string>run</string>
    <string>python</string>
    <string>main.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/comfyui-sync.log</string>
  <key>StandardErrorPath</key><string>/tmp/comfyui-sync.log</string>
</dict>
</plist>
```

Logs: `tail -f /tmp/comfyui-sync.log`. Stop: `launchctl unload ~/Library/LaunchAgents/com.local.comfyui-sync.plist`.

### Reverse-proxy (optional)

For remote access put `nginx` / `caddy` in front. Minimal Caddyfile:

```
sync.example.com {
  reverse_proxy 127.0.0.1:8765
}
```

SSE streams work over HTTP/1.1 — no special config needed.

## How it works

- `POST /analyze` — parses workflow `nodes[]`, extracts model filenames from
  loader widgets (config `model_node_map`) and detects custom-node packages
  via substring match (`node_packages`). Whitelist of stock nodes lives in
  `builtin_nodes`.
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
