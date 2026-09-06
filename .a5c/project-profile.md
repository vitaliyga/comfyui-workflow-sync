# Project Profile: comfyui-workflow-sync

ComfyUI S3 Sync — a single-operator, self-hosted FastAPI web app (main.py, 1490 lines / ~53KB, plus one 938-line vanilla-JS page at static/index.html) that takes a dropped ComfyUI workflow JSON (standard or API export), resolves every model file and custom-node package it needs, diffs that against the local ComfyUI install at COMFYUI_PATH, and fetches only what is missing by shelling out to AWS CLI v2 (`aws s3 sync`) against S3 or an S3-compatible endpoint (Cloudflare R2 / MinIO / Wasabi). Resolution is index-driven: an S3 catalogue built from `aws s3 ls --recursive` plus per-package *_CLASS_MAPPINGS parsing, enriched by the ComfyUI-Manager extension-node-map registry, cached to .s3-index.json. Managed with uv; no database, no auth, no tests, no CI, no containers — deployment is manual (systemd unit or a RunPod /workspace/start-sync.sh, both documented in README prose only).

> Last updated: 2026-09-06T19:27:00Z | Version: 1

## Goals

- **correctness** [critical]: Keep ComfyUI S3 Sync a reliable single-operator tool: drop a workflow JSON and have every required model file and custom-node package resolved correctly, pulling only what is missing from S3/R2. Correctness of resolution matters more than new features (owner, verbatim). (in-progress)
- **correctness** [high]: Make node/model classification converge instead of escalating: reduce dependence on hardcoded per-package tables (_KNOWN_NODE_PACKAGES / the LTXVideo map from 548db62) and on ComfyUI-Manager blind spots, so a new upstream workflow does not require another patch to extract_models / extract_models_api / extract_custom_nodes / _classify_node. (in-progress)
- **documentation** [high]: Bring documentation back level with the code: README has been frozen since 2026-05-19 while 21 code commits landed (+903/-157), so the S3 folder picker and name search, R2 support, ComfyUI API-format workflow support and APP_PORT/PORT are undocumented; .env.example omits AWS_ENDPOINT_URL, INDEX_TTL, AWS_ENDPOINT_URL_S3, CM_REGISTRY_URL, APP_HOST and CONFIG which main.py reads. (open)
- **maintainability** [high]: Keep the main.py monolith workable: it is 1490 lines carrying 48% of all repo churn (23 of 31 commits) and is hidden-coupled to static/index.html (9 shared commits; 178e53f was a real breakage from changing one without the other). Either contain the coupling with an explicit response-contract discipline or extract the classifier/index/sync-engine seams. (open)
- **process** [medium]: Install babysitter to get a structured, deterministic orchestration setup for future work on this repo — semi-autonomous execution with breakpoints on architecture decisions, destructive operations and deploys. (in-progress)
- **scope** [medium]: Hold the declared scope line from README 'Scope notes': PNG-embedded workflow parsing, git-based custom-node installation, authentication and multi-user support stay out of scope by design. (ongoing)

## Tech Stack

### Languages

- Python v>=3.11 (Primary — the entire backend and all business logic in a single main.py (1490 lines). Modern syntax throughout: `from __future__ import annotations`, PEP 604 unions (`str | None`), builtin generics (`dict[str, Any]`), str.removeprefix/removesuffix.)
- JavaScript vES2017+ (browser-native, no transpile) (Frontend — one inline <script> in static/index.html; vanilla DOM, fetch, EventSource, CSS.escape. No framework, bundler or npm.)
- HTML/CSS (Single static page with inline <style>; dark-themed, hand-written, no CSS framework.)
- YAML (Optional legacy config format (config.yaml, yaml.safe_load); the file itself was removed from the repo — everything is auto-discovered now.)
- Bash/shell (Documentation only — install, systemd unit and RunPod start scripts live in README.md. No shell script is committed.)

### Frameworks

- FastAPI v>=0.115 [http-framework]
- uvicorn[standard] v>=0.32 [asgi-server]
- pydantic vv2 (transitive via FastAPI, pinned in uv.lock) [validation]
- starlette vtransitive via FastAPI [asgi-toolkit]
- PyYAML v>=6.0 [config]
- asyncio (stdlib) [concurrency]
- urllib.request (stdlib) [http-client]
- difflib (stdlib) [matching]

### Databases

- none (n/a)
- S3 / S3-compatible object storage (object store (external system of record))

### Infrastructure

- AWS CLI v2 [hard runtime dependency]
- ComfyUI [host application]
- ComfyUI-Manager registry [external data source]
- systemd [deployment target]
- RunPod [deployment target]
- nginx / Caddy [optional reverse proxy]
- Cloudflare R2 / MinIO / Wasabi [S3-compatible backends]

**Build tools:** uv (Astral) — dependency resolution and virtualenv; `uv sync` to install, `uv run python main.py` to launch, uv.lock — committed lockfile, revision 3, 21 locked packages, requires-python >=3.11, PEP 621 pyproject.toml metadata (no [build-system] section — the project is run, not packaged/published), No frontend build: no npm/package.json, no bundler, no minifier — static/index.html is served verbatim, No linter/formatter config (no ruff/black/flake8/mypy settings), no pre-commit, no Makefile, no Dockerfile, no CI workflows (.github/ absent)

**Package managers:** uv (primary, uv.lock committed), pip (runtime-invoked only: the sync worker runs `pip install -r requirements.txt` inside a freshly synced custom-node directory, main.py:1348)

## Architecture

**Pattern:** Single-service monolith — one self-contained Python file (main.py, ~1490 LOC / 53KB) exposing a FastAPI HTTP+SSE API and serving one static vanilla-JS single-page UI (static/index.html, 938 lines, inline CSS+JS). No packages, no src/ layout, no monorepo/workspace config, no frontend build step. The app is a thin orchestrator around the external `aws` CLI v2 (subprocess); it holds no persistent server state beyond an in-memory JOBS dict and a JSON file cache (.s3-index.json).
**Data flow:** 1) Boot: importing main.py runs load_dotenv('.env') -> reads COMFYUI_PATH / S3_MODELS_BASE / S3_NODES_BASE / AWS_* (RuntimeError if COMFYUI_PATH is missing), optionally merges config.yaml, and loads any cached .s3-index.json. 2) Index: on the first /analyze or /search (or an explicit POST /reindex, or when the cache is older than INDEX_TTL), rebuild_index() fans out three coroutines under _REINDEX_LOCK — `aws s3 ls --recursive` over the models base, per-package `aws s3 cp - ` fetch+regex-parse of custom_nodes __init__.py/nodes.py for *CLASS_MAPPINGS keys, and an HTTPS fetch of ComfyUI-Manager's extension-node-map.json — producing S3_INDEX, written back to .s3-index.json. 3) Analyze: the browser drops workflow JSON -> POST /analyze -> _is_api_format() picks the standard vs API extractor -> model strings are pulled from widgets_values/inputs (recursing into rgthree-style dicts, skipping on:false), folder hints are inferred from class name and API slot key, and _lookup_model() resolves each name against the index; each class name goes through _classify_node()'s 7-step precedence -> JSON {format, models[], custom_nodes[], assumed_builtin[]}, every row already carrying s3_source, local_path and exists_locally computed against COMFYUI_PATH on disk. 4) Sizing: the UI batches all s3_source values into POST /size -> answered from the index where possible, otherwise `aws s3 ls [--recursive --summarize]` behind a 4-way semaphore, with difflib fuzzy candidates when an exact key is missing (renamed lora versions). 5) Sync: the user checks rows (optionally overriding a row's source through the /browse + /search picker) -> POST /sync {items:[{s3_source, local_dest, is_file, expected_bytes}]} -> each local_dest is validated to stay inside COMFYUI_PATH and total expected bytes checked against free disk -> a uuid4 job is registered in the in-memory JOBS dict and run_job() fires up to SYNC_PARALLEL `aws s3 sync --exact-timestamps` subprocesses, whose stdout 'Completed X/Y' lines become byte-progress SSE events on GET /sync/{job_id}/stream; a watchdog flags a source stalled after SYNC_STALL_SECS, a synced custom-node dir gets `pip install -r requirements.txt`, and DELETE /sync/{job_id} terminates the subprocesses (exit 130). Jobs expire from memory after JOB_TTL_SECS=300.

### Modules

| Module | Path | Description |
|--------|------|-------------|
| config & bootstrap | `main.py:18-120` | ROOT/CONFIG_PATH constants, `_expand_env` + `load_dotenv` (hand-rolled .env parser with ${VAR}/$VAR expansion; empty values skipped and os.environ.setdefault used so platform env wins), `load_config`/`_load_config_optional` (optional legacy config.yaml), derived globals COMFY/MODELS_DIR/NODES_DIR/MODEL_EXTS/MODEL_NODE_MAP/MODEL_FOLDERS/NODE_PACKAGES, S3_MODELS_BASE/S3_NODES_BASE, `_S3_ENDPOINT` (AWS_ENDPOINT_URL_S3 or AWS_ENDPOINT_URL) and `_aws_s3()` which builds every `aws s3 …` argv with --endpoint-url injected. Fails fast at import with RuntimeError if COMFYUI_PATH is unset. |
| S3 index builder | `main.py:126-410` | Builds and caches the S3 catalogue in the module-global S3_INDEX {models:{files,by_basename}, nodes:{classes,packages,unparsed_packages}, cm:{classes,builtins,count}, indexed_at}. index_models() runs one `aws s3 ls --recursive`; index_nodes() lists package dirs then per package streams .py files via `aws s3 cp - ` (BFS <=6 levels, 8-way semaphore) and regex-parses any *CLASS_MAPPINGS literal, following `from .X import *_CLASS_MAPPINGS` and `from . import a,b` re-exports; fetch_cm_registry() pulls ComfyUI-Manager's extension-node-map.json over urllib in a thread. rebuild_index() is guarded by _REINDEX_LOCK (coalesces concurrent callers, 5s recency short-circuit); ensure_index_fresh() rebuilds past INDEX_TTL (default 86400s); persisted via _save_index()/_load_index_from_disk(). |
| model resolution heuristics | `main.py:414-527` | Maps a workflow's model filename to a real S3 object. _NODE_FOLDER_HINTS (ordered class-name substring -> folder tuple; specific before generic, e.g. CLIPVision before CLIP, LTXVAudioVAELoader -> checkpoints) and _INPUT_FOLDER_HINTS (API-format slot keys ckpt_name/vae_name/lora_name/…) feed _infer_folder_hints(), authority order config map -> input slot key -> class name. _lookup_model() does exact-path lookup, then basename fallback with nested-subpath preference (prod/X.safetensors), then folder-hint disambiguation with a fewest-path-segments tiebreak; _folder_matches() is lenient (prefix match) to tolerate vae/vae_approx and text_encoders/clip variance. |
| custom-node classifier | `main.py:530-645` | _classify_node() returns {source: s3|override|cm} or None (= stock ComfyUI) via a 7-step precedence: (1) S3 static NODE_CLASS_MAPPINGS parse, (2) the node's own cnr_id/aux_id properties written by ComfyUI-Manager matched against S3 dirs, (2.5) _KNOWN_NODE_PACKAGES hardcoded map for CM/parser blind spots (ComfyUI-LTXVideo AV/tiled-VAE nodes), (3) user node_packages override via match_package(), (4) CM builtin list, (5) CM entry whose pkg exists in S3, (6) vendor tag in a class name '(rgthree)' via _s3_pkg_fuzzy, (7) CM repo -> GitHub-only row. _norm_pkg() (lowercase alnum-only) makes ComfyUI_LayerStyle == comfyui-layer-style. |
| workflow extractors | `main.py:648-861` | extract_models/extract_custom_nodes for standard workflow JSON (nodes[] with type/widgets_values/properties) and extract_models_api/extract_custom_nodes_api for ComfyUI API-format exports (_is_api_format, _iter_api_nodes on class_type/inputs). _iter_model_strings() walks nested lists/dicts to catch structured widgets (rgthree Power Lora Loader) and skips slots with on:false. _build_model_entry() produces UI rows {file, folder, local_path, exists_locally, s3_source, s3_bytes, s3_exists}; _suppress_phantom_models() drops unresolved duplicates of an already-resolved basename (cosmetic only); _resolve_local_node_dir() normalizes on-disk custom_nodes dir names to avoid case-variant duplicate installs. |
| HTTP API layer | `main.py:864-1480` | FastAPI route handlers plus pydantic request models (AnalyzeBody, SyncItem, SyncBody, SizeBody): POST /analyze, GET /status, GET /browse, POST /reindex, GET /search, POST /size, POST /sync, DELETE /sync/{job_id}, GET /sync/{job_id}/stream (SSE), GET / (FileResponse of static/index.html). preflight() probes auth (sts get-caller-identity, or `aws s3 ls` when a custom endpoint is set), bucket access and the ComfyUI path, cached for PREFLIGHT_TTL=120s. |
| size probing | `main.py:1090-1188` | _aws_ls, list_prefix, s3_size (recursive --summarize for prefixes; exact file lookup with a difflib.get_close_matches fuzzy fallback returning up to 5 renamed-file candidates). _size_from_index() answers from the cached index when possible; _bounded_size() limits live probes with SIZE_PROBE_SEM = Semaphore(4). |
| sync job engine | `main.py:1190-1473` | build_command() produces `aws s3 sync --exact-timestamps` argv (single files as parent-prefix sync with --exclude '*' --include <name>). process_item() runs one subprocess under Semaphore(SYNC_PARALLEL, default 3), reads stdout in 4KB chunks split on \r|\n, parses `Completed X MiB/Y MiB` via _PROGRESS_RE/_UNIT_FACTORS into byte-progress events, runs a watchdog emitting stalled/unstalled events after SYNC_STALL_SECS=8, and on success pip-installs a synced package's requirements.txt streaming its output. run_job() gathers items (return_exceptions=True) and emits the terminal done event. Jobs live in the in-memory JOBS dict and are dropped after JOB_TTL_SECS=300. _validate_local_dest() rejects destinations resolving outside COMFY (path-traversal guard); _check_disk_space() refuses a job needing >95% of free space. |
| frontend SPA | `static/index.html` | 938-line single page, no framework/bundler: inline <style> and one inline <script> of plain ES2017+. Drag-and-drop or file-picker of one or many workflow .json files -> POST /analyze per file -> mergeAnalyses() dedups across files -> render() builds models/nodes tables with checkboxes -> fetchSizes() batches POST /size -> buildSyncItems() -> POST /sync -> EventSource on /sync/{job_id}/stream drives per-row progress bars, stalled tags and a console <pre>. Also a modal S3 path picker (openPicker/loadPickerDir/runPickerSearch over /browse and /search) to override unresolved rows, applyOverride, a Stop button (DELETE /sync/{job_id}), Re-analyze, preflight banners from /status and a reindex link (POST /reindex). Client state lives in module-scope arrays/objects/Sets (customRows, sizeCache, overrides, syncedSources, syncRows, lastStatus). |

**Entry points:** `/home/user/comfyui-workflow-sync/main.py — main() at line 1480 under `if __name__ == "__main__"`; imports uvicorn lazily and runs uvicorn.run(app, host=APP_HOST or 0.0.0.0, port=APP_PORT or PORT or 8765). Documented launch: `uv run python main.py`.`, `/home/user/comfyui-workflow-sync/main.py:122 — `app = FastAPI()`, the ASGI application object (usable as `uvicorn main:app`).`, `/home/user/comfyui-workflow-sync/main.py:1475 GET / — serves static/index.html via FileResponse; the browser UI entry point (http://localhost:8765).`, `HTTP API: POST /analyze, GET /status, GET /browse, GET /search, POST /reindex, POST /size, POST /sync, DELETE /sync/{job_id}, GET /sync/{job_id}/stream (SSE).`, `/home/user/comfyui-workflow-sync/static/index.html — the SPA; its script runs on load (fetch('/status') at line 893) with no build step.`, `Import-time bootstrap side effects: load_dotenv(ROOT/'.env') at main.py:50 and _load_index_from_disk() at main.py:205.`

## Team

- **Vitaliy Galkin (git identities: 'Vitaliy Galkin' 19 commits and 'vitaliyga' 11 commits, both vitaliygalkyn@gmail.com)** (sole maintainer / owner — architecture, implementation, docs, ops): main.py: server, S3 index builder, resolution heuristics, node classifier, extractors, sync job engine, static/index.html: the entire UI, including the S3 path picker and search, README.md and .env.example — every documentation commit, Project setup: pyproject.toml, uv.lock, .gitignore, Deployment by hand (systemd on Linux, /workspace/start-sync.sh on RunPod), Self-review; no separate reviewer exists
- **antonl-xcritical-software <antonl@xcritical.software>** (drive-by outside contributor (1 commit)): Cloudflare R2 / S3-compatible endpoint support (fd45f83, main.py + .env.example)

## Workflows

### development

uv-managed Python workflow on a single-file backend. There is no test, lint or build step to run — the loop is edit main.py or static/index.html, restart the server, verify by hand against live S3 and a live ComfyUI install (GET /status preflight, POST /reindex stats and the SSE console are the runtime self-checks).
**Triggers:** manual

1. git clone https://github.com/vitaliyga/comfyui-workflow-sync && cd comfyui-workflow-sync
2. uv sync  (installs from uv.lock; requires Python >= 3.11)
3. cp .env.example .env and fill in COMFYUI_PATH, S3_MODELS_BASE, S3_NODES_BASE and AWS credentials (never commit .env; never echo real values)
4. uv run python main.py
5. open http://localhost:8765 and drop a real workflow JSON to exercise the change
6. If the change alters the /analyze, /size or /sync response contract, update static/index.html in the same commit
7. Commit to main (or a claude/<slug> branch for agent-driven work) with an imperative sentence-case subject and an explanatory body

### git workflow

Trunk-based on main with a strictly linear history: 31 commits, zero merge commits, zero reverts, zero tags, no PRs. Agent work goes on claude/<slug> branches (the only non-main branch in the repo is claude/a5c-babysitter-setup-tmknfi). Code review is optional/self-review — PRs are not currently required.
**Triggers:** manual

1. Work on main, or on claude/<slug> for agent-driven work
2. One logical change per commit (17 of 31 commits touch exactly one file); follow-up fixes are separate named commits, not amends
3. Subject: imperative, sentence-case, no Conventional Commits prefix, optional lightweight scope prefix (README:, Index:, Classifier:, fetchSizes:)
4. Body: explain the upstream root cause before the fix (28 of 31 commits carry a real body)
5. Ship env/config changes together with the code that reads them (.env.example co-changes with main.py)
6. Push to origin https://github.com/vitaliyga/comfyui-workflow-sync
7. Break for confirmation before any destructive git operation

### code review

Optional / self-review by owner choice. No enforced process exists in the repo: no CODEOWNERS, no PR template, no required status checks, no reviewers. Review has happened exactly once in 31 commits, as an after-the-fact sweep (1c98a4c 'Apply review feedback: JOBS TTL, preflight TTL, CSS.escape, path traversal'). README states 'PRs welcome but check spec first'.
**Triggers:** optional / manual

1. Self-review the diff before pushing
2. Check any /analyze, /size or /sync contract change against static/index.html

### release

None — no release process exists. Zero tags, no CHANGELOG, no release automation, and pyproject.toml has been pinned at version 0.1.0 since the initial commit (modified in exactly one commit ever). Deployment is pull-from-main plus a restart, so a bad commit on main is the deployed state.

### deployment: Linux systemd

Documented-only (README prose) — the unit file is NOT committed. Runs the app as a long-lived service that auto-loads .env from beside main.py. Manual; break for confirmation before deploying.
**Triggers:** manual, host boot (systemd WantedBy=multi-user.target)

1. Write /etc/systemd/system/comfyui-sync.service (Type=simple, User=comfy, WorkingDirectory=/opt/comfyui-workflow-sync, ExecStart=/usr/local/bin/uv run python main.py, Restart=on-failure, RestartSec=5, After/Wants=network-online.target)
2. systemctl daemon-reload
3. systemctl enable --now comfyui-sync
4. Tail logs with journalctl -u comfyui-sync -f

### deployment: RunPod pod

Documented-only bootstrap for RunPod GPU pods, which have no systemd and reset the container on every pod start — everything persistent must live under /workspace. Manual; break for confirmation before deploying.
**Triggers:** pod boot (container start command), manual

1. Clone the repo into /workspace
2. Install uv (curl -LsSf https://astral.sh/uv/install.sh | sh), then uv sync
3. Install AWS CLI v2 from https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip (idempotent: gate on `aws --version`)
4. Create .env on the persistent volume with COMFYUI_PATH=/workspace/ComfyUI
5. Expose TCP 8765 in the pod's HTTP ports (proxied at https://<pod-id>-8765.proxy.runpod.net; SSE works) or set APP_PORT to the pod's exposed port
6. Create /workspace/start-sync.sh (execs `uv run python main.py`, appends to /workspace/comfyui-sync.log), chmod +x
7. Run it from the pod Container Start Command alongside ComfyUI, or in a detached tmux session

### deployment: optional reverse proxy

Documented-only. nginx or Caddy in front for remote access; README shows a minimal Caddyfile (sync.example.com { reverse_proxy 127.0.0.1:8765 }). SSE needs no special config over HTTP/1.1. Adds no authentication — the app has none by design.
**Triggers:** manual

1. Install caddy or nginx
2. Point a reverse_proxy at 127.0.0.1:8765

### runtime sync workflow (the product's core function)

The app's own operational pipeline, implemented in main.py rather than in any CI system.
**Triggers:** HTTP request from the web UI, 24h index TTL expiry, POST /reindex

1. Build/refresh the S3 index (aws s3 ls --recursive over S3_MODELS_BASE; list packages under S3_NODES_BASE and parse *_CLASS_MAPPINGS out of each __init__.py), cached to .s3-index.json with INDEX_TTL default 86400s
2. Enrich with the ComfyUI-Manager class->repo registry fetched over HTTPS
3. POST /analyze — parse the dropped workflow JSON (standard and API format), resolve model filenames and node class names, diff against local disk
4. POST /size — aws s3 ls --summarize per source, with fuzzy candidate suggestions for renamed files
5. POST /sync — run up to SYNC_PARALLEL (default 3) `aws s3 sync --exact-timestamps` subprocesses behind a semaphore; parse 'Completed X/Y' for byte progress; a watchdog flags a source stalled after SYNC_STALL_SECS (default 8)
6. Stream progress to the browser over SSE at GET /sync/{job_id}/stream
7. After a successful custom-node sync, run pip install -r requirements.txt in the synced directory
8. DELETE /sync/{job_id} terminates the running subprocesses to cancel

### babysitter orchestration (autonomy policy)

Semi-autonomous, as chosen by the owner: routine steps run without stopping; breakpoints are required for architecture decisions, destructive operations and deploys. ALWAYS break on destructive git operations and on anything that would touch credentials or the user's ComfyUI install. CI/CD generation is explicitly skipped in this install.
**Triggers:** babysitter process execution

1. Run routine, non-destructive steps autonomously
2. Breakpoint before any architecture decision (module extraction, contract change, dependency addition)
3. Breakpoint before any destructive operation, including destructive git (force-push, reset --hard, branch/tag deletion, history rewrite)
4. Breakpoint before any deploy (systemd restart, RunPod start-sync.sh)
5. Breakpoint before anything that reads, writes or echoes credentials (.env stays untracked; only .env.example is edited) or that would modify the user's ComfyUI install under COMFYUI_PATH

## Services

- **comfyui-workflow-sync HTTP server (this app)** (self-hosted web service (FastAPI + uvicorn)) - http://0.0.0.0:8765 by default; host/port overridable via APP_HOST and APP_PORT/PORT
- **Amazon S3 (or S3-compatible object storage)** (object-storage) - s3://<S3_MODELS_BASE>, s3://<S3_NODES_BASE> (values from .env; AWS_ENDPOINT_URL_S3 / AWS_ENDPOINT_URL overrides the endpoint for Cloudflare R2 / MinIO / Wasabi)
- **AWS STS** (cloud-identity) - default AWS STS endpoint (no URL hardcoded)
- **ComfyUI-Manager extension-node-map (GitHub raw)** (third-party HTTP API / static JSON registry) - https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json
- **Local ComfyUI installation** (local-filesystem dependency) - file://$COMFYUI_PATH (with models/ and custom_nodes/ subdirectories)

## CI/CD

**Provider:** none

## Pain Points

- **high** [correctness / external-dependency-fragility]: OWNER PRIORITY 1. Fragile node/model classification. 15 of 31 commits are patches to extract_models / extract_models_api / extract_custom_nodes / _classify_node chasing ComfyUI-Manager and parser blind spots, and they form an escalation ladder rather than a converging design: hand-maintained map (init) -> S3 auto-discovery (db71815) -> CM registry as universe (2cffdcb) -> CM fork duplicates (ad43b64) -> modular *_CLASS_MAPPINGS parsing (c7c78ae) -> ambiguous basenames via loader type (7659218) -> input-slot key over class name (53eb197) -> cnr_id/aux_id/vendor tag before the CM guess (c3909ef) -> a hardcoded ComfyUI-LTXVideo class->package table (548db62, whose own subject says 'CM/parser blind spot'). HEAD (9c310cd, 2026-08-28) is still a resolution fix (nested lora subpaths). The core value depends on reverse-engineering third-party data the project does not control: other authors' custom-node Python sources, the CM extension-node-map, rgthree's Power Lora Loader widget shape, LTXVideo class names.
  - Remediation: Treat classification as the project's contract surface: capture the resolved fixtures from real workflows as regression cases before touching the heuristics, keep _classify_node's 7-step precedence and the ordered *_HINTS tables explicit and commented, and when a heuristic genuinely cannot be generalized keep hardcoding the exception but say so in the subject (the existing house rule) — while tracking those exceptions in one place so the blind-spot list stays visible.
- **medium** [documentation]: OWNER PRIORITY 2. Documentation lags the code. README.md has 6 commits, 5 of them on 2026-05-18/19, and has not been touched since 1f04a23 (2026-05-19) while main.py and static/index.html took 21 further commits and 1060 changed lines (+903/-157) over the following 102 days. Undocumented since the freeze: the S3 folder picker and its name search, R2/S3-compatible endpoint support, ComfyUI API-format workflow support, and the APP_PORT/PORT setting. Three-way env drift: .env.example omits AWS_ENDPOINT_URL and INDEX_TTL (both in the README table), and neither README nor .env.example mentions AWS_ENDPOINT_URL_S3, CM_REGISTRY_URL, APP_PORT, PORT, APP_HOST or CONFIG, all of which main.py reads. README co-changed with code in only 2 of its 6 commits — docs are updated in dedicated bursts, not as part of the change unit.
  - Remediation: Make docs part of the change unit: any commit that adds or renames an env var updates .env.example in the same commit (already the house rule for 3 of its 4 commits), and any commit that adds a user-visible endpoint or UI affordance updates the matching README section. Start with a one-off catch-up pass for the picker/search, R2, API-format and APP_PORT/APP_HOST/INDEX_TTL/CM_REGISTRY_URL/CONFIG gaps.
- **high** [architecture]: OWNER PRIORITY 3. main.py is a 1490-line monolith (451 lines at init, 3.3x growth, never split) touched by 23 of 31 commits and absorbing 2186 of 4529 changed lines — 48% of all repo churn. Churn concentrates in a handful of functions (extract_models 11 hunks, extract_models_api 8, extract_custom_nodes 8, run_job 8, _classify_node 7, _lookup_model 6, _build_node_row 6), so parser, classifier, S3 client, job engine and HTTP routes all contend for one edit surface. It is also hidden-coupled to static/index.html: the strongest co-change pair in the repo (9 shared commits vs 3 for the next pair), 75% of frontend commits also touch main.py, and the coupling is contract-shaped — response-row fields flow straight into DOM state, so 178e53f ('Restore progress bars: query row by s3_source, not by removed _row_key') is a real breakage caused by a backend refactor dropping a key the frontend was keying rows on.
  - Remediation: Short term, enforce the contract rule the owner stated: any change to the /analyze, /size or /sync response shape must be checked against static/index.html in the same commit. Medium term, extract along the seams the file already has (config bootstrap / S3 index builder / resolution heuristics / classifier / extractors / HTTP layer / sync engine are already banner-delimited sections) rather than a wholesale rewrite.
- **high** [quality-assurance]: RECORDED, NOT PRIORITIZED BY THE OWNER. There is no automated verification anywhere in the history: zero test files, zero CI config, zero linter/formatter config across all 31 commits (only 9 paths have ever been tracked). Verification is manual against live S3 and a live ComfyUI install, and the history shows the cost — the 2026-05-25 session shipped the folder picker (c7e8645) and then needed three repair commits within 94 minutes (178e53f, d10e7f3, c43eaa9), and defects such as the browser hang on multi-file drop, phantom rows and dropped progress bars could only surface at runtime with real data.
  - Remediation: If and when this is picked up: pytest + pytest-asyncio with fastapi TestClient, mocking _aws_text/_aws_ls (all S3 access funnels through those two helpers and _aws_s3()), seeded with real workflow JSON fixtures. The owner has deliberately deferred this and CI until real checks exist — do not add cosmetic gates in the meantime.
- **medium** [process-maturity]: No release, versioning or review process to lean on: zero tags, zero merge commits, zero PRs, no CHANGELOG, and pyproject version pinned at 0.1.0 since the initial commit. Review happened exactly once and only as an after-the-fact sweep (1c98a4c 'Apply review feedback: JOBS TTL, preflight TTL, CSS.escape, path traversal' — a path-traversal fix that reached main through no gate but a manual read). There is no rollback point: a bad commit on main is the deployed state.
  - Remediation: The owner has chosen to keep review optional/self-review and to run no release process. A minimal, low-cost hedge that fits: keep the deployed commit recorded (systemd/RunPod host notes) so a rollback target exists, since redeploy is a manual `git pull` plus restart.
- **medium** [bus-factor]: Effectively a solo project with an unmanaged identity split: 30 of 31 commits come from one person committing under two author names against the same email ('Vitaliy Galkin' 19, 'vitaliyga' 11 — the name switched permanently at 7659218 on 2026-06-09, consistent with an unconfigured second machine). The single outside contribution (fd45f83 'add suuport of R2') is the only subject with a typo, one of only three commits with no body, and touched shared config plumbing. Knowledge of the classification heuristics is documented nowhere but in commit bodies and inline comments.
  - Remediation: Set user.name consistently on both machines. Keep the 'why' comments and explanatory commit bodies — they are the only knowledge store for the ComfyUI-ecosystem quirks.
- **medium** [external-dependency-fragility]: External toolchain and third-party data sources are a recurring source of rework beyond classification proper: the app shells out to the aws CLI and parses other projects' Python sources and the CM registry JSON, so upstream shape changes land here as defects — 6381a8d (browser hang on multi-file drop: dozens of aws subprocesses at once, fixed by dedup reindex + bounded /size concurrency), e9ad234 ('from . import sub1, sub2' re-export following), c7c78ae (modular *_CLASS_MAPPINGS), ad43b64 (CM catalogs one class under multiple forks), eaf198c (case-variant install dir names), fd45f83 (second S3-compatible backend). Sync progress is also scraped from aws CLI stdout ('Completed X/Y'), so a CLI output change breaks progress reporting.
  - Remediation: Keep every external touchpoint funnelled through the existing chokepoints (_aws_s3 for argv, _aws_text/_aws_ls for rc-tuples, fetch_cm_registry for the registry) so an upstream change has exactly one repair site, and keep the fail-soft degradation (empty index, non-fatal registry failure) intact.
- **medium** [frontend-maintainability]: Frontend row/state management is a repeat defect site. static/index.html is 938 lines of plain HTML plus one inline script with no component or state abstraction, so every feature adds another ad-hoc re-render path. The 2026-05-25 session is 4 commits, 3 of them corrections to the picker shipped 74 minutes earlier (178e53f row lookup by s3_source; d10e7f3 fetchSizes must repopulate sizes from cache after re-render; c43eaa9 synced custom/picked rows never promoted to OK), with 67efe69 (phantom 'not on S3' rows) a fourth instance on 2026-06-09.
  - Remediation: Keep row identity on one stable key (s3_source is the current one) and re-derive display state from a single source of truth after every re-render, rather than patching individual render paths.
- **low** [hygiene]: Repository hygiene was retrofitted rather than set up: the initial commit added a macOS .DS_Store (8196 bytes) removed one commit later (48f3aab); .gitignore then needed a one-line correction in a commit whose entire message is 'fix' (385d67b) and was amended again in db71815; config.yaml was introduced at init, edited twice, then deleted wholesale (156 insertions and 156 deletions over its life, net zero), yet its loader code (load_config at main.py:54, now dead, superseded by _load_config_optional) and the pyyaml dependency both remain today.
  - Remediation: Opportunistic cleanup only: drop the dead load_config path (and pyyaml with it, if config.yaml support is truly retired) and declare pydantic explicitly in pyproject rather than relying on the transitive FastAPI pin.
- **low** [sustainability]: Development is bursty and increasingly intermittent, which risks context loss on a codebase whose logic lives mostly in the maintainer's head. Median gap inside a session is ~15 minutes (23 of 30 gaps under 40 minutes), but sessions are spaced 13.6h, 5.9d, 11.1d, 4.1d and then 77 days apart; per-day counts decay 7, 8, 4, 1, 8, 1, 1, 1. The project has been effectively dormant since 2026-06-12 apart from a single fix on 2026-08-28.
  - Remediation: This is exactly the gap a deterministic orchestration setup is meant to bridge — a durable project profile plus explicit processes so a cold restart after a 77-day gap does not depend on recalling the heuristics.

## Bottlenecks

- Single-module monolith: main.py is touched by 23 of 31 commits (74%) and absorbs 2186 of 4529 changed lines (48% of churn, +1838/-348), growing monotonically 451 -> 1490 lines with no extraction at any point. Churn concentrates in extract_models (11 touched hunks), extract_models_api (8), extract_custom_nodes (8), run_job (8), _classify_node (7), _lookup_model (6), _build_node_row (6). at main.py (23 of 31 commits (74%); present on every one of the 8 active days)
  Impact: high
- Node-and-model identification heuristics are the true hotspot: 15 of 31 commits correct classification or resolution logic, forming an escalation ladder that ends in a hardcoded class->package table rather than converging. The 2026-06-09 session alone spent 8 commits (+297/-73) entirely here. at main.py :: _classify_node / _lookup_model / extract_models / match_package (15 of 31 commits (48%), recurring in every session after 2026-05-19)
  Impact: high
- Backend and frontend are tightly coupled and change together: main.py <-> static/index.html is the strongest co-change pair (9 shared commits; next strongest is 3), and 9 of 12 frontend commits (75%) also modify main.py. The coupling is contract-shaped — response-row fields flow straight into DOM state, so a backend field rename breaks the UI silently (178e53f). at main.py <-> static/index.html (/analyze, /size, /sync response rows) (9 co-change commits; 75% of all frontend commits)
  Impact: medium
- No automated verification has ever existed: only 9 paths were ever tracked and none is a test, CI workflow, Dockerfile, Makefile or linter config. Verification is manual against live S3 and a live ComfyUI install, producing same-session regression-fix chains (c7e8645 -> 178e53f -> d10e7f3 -> c43eaa9 within 94 minutes). at repository-wide (no test or CI paths in any of the 31 commits) (structural — absent across all 31 commits)
  Impact: high
- Frontend row/state management is a repeat defect site: 4 fix commits clustered around one feature commit, on a 938-line single page with no component or state abstraction, where each feature adds another ad-hoc re-render path. at static/index.html (row identity and re-render state) (4 fix commits around 1 feature commit)
  Impact: medium
- Documentation frozen since 2026-05-19: README.md's last change is 1f04a23, after which main.py and static/index.html took 21 commits and 1060 changed lines, including user-facing features the README cannot describe. README co-changed with code in only 2 of its 6 commits. at README.md (and .env.example drift) (0 README updates across the last 21 code commits (102-day span))
  Impact: medium
- External toolchain and third-party data sources cause recurring rework — the aws CLI, other projects' Python sources and the ComfyUI-Manager registry all land upstream shape changes here as defects; rebuild_index-family functions account for 8+ touched hunks. at main.py :: rebuild_index / _fetch_pkg_class_names / fetch_cm_registry / build_command (6+ commits directly attributable to upstream/tooling behavior)
  Impact: medium

## Conventions

### Naming

- **python:** snake_case for functions and variables; SCREAMING_SNAKE_CASE for module-level constants and caches (ROOT, COMFY, MODELS_DIR, MODEL_EXTS, S3_INDEX, JOBS, PARALLEL_LIMIT, JOB_TTL_SECS); PascalCase only for the pydantic request models (AnalyzeBody, SyncItem, SyncBody, SizeBody).
- **privateHelpers:** A single leading underscore marks module-private helpers and is used heavily and consistently (_expand_env, _aws_s3, _aws_text, _split_s3, _lookup_model, _norm_pkg, _classify_node, _build_model_entry, _iter_model_strings, _suppress_phantom_models, _resolve_local_node_dir, _probe, _size_from_index, _bounded_size, _check_disk_space, _validate_local_dest, _schedule_job_cleanup). Public names are domain operations or route handlers (index_models, rebuild_index, extract_models, match_package, build_command, process_item, run_job, s3_size).
- **regexConstants:** Compiled regexes are module-level, underscore-prefixed and suffixed _RE: _ENV_VAR_RE, _ANY_CM_BLOCK_RE, _NCM_KEY_RE, _NCM_REEXPORT_RE, _NCM_SUBMOD_RE, _PROGRESS_RE.
- **lookupTables:** Ordered heuristic tables are lists of (needle, value) tuples named *_HINTS (_NODE_FOLDER_HINTS, _INPUT_FOLDER_HINTS) carrying an explicit 'first match wins / specific before generic' comment — the ORDER IS LOAD-BEARING and must be preserved; dict-shaped tables are *_MAP / *_PACKAGES (MODEL_NODE_MAP, _KNOWN_NODE_PACKAGES, NODE_PACKAGES).
- **routes:** Lowercase, single-word, noun-or-verb REST paths (/analyze, /status, /browse, /search, /reindex, /size, /sync, /sync/{job_id}, /sync/{job_id}/stream); handler function names mirror the path.
- **jsonKeys:** All API payload keys are snake_case (s3_source, local_dest, expected_bytes, exists_locally, package_hint, node_type, assumed_builtin, job_id, item_index, bytes_downloaded, exit_code) — the JS consumes them unchanged rather than camelCasing.
- **javascript:** camelCase functions and variables (handleFiles, analyzeAll, mergeAnalyses, humanBytes, buildSyncItems, fetchSizes, openPicker, applyOverride); DOM handles cached in module-scope consts; kebab-case HTML ids (file-input, select-all-btn, sync-btn, models-table).

### Git

- **branching:** Trunk-based on main; 31 commits, strictly linear, no merge commits, no reverts, no release tags. Agent/assistant work goes on claude/<slug> branches. No feature/*, release/* or hotfix/* convention exists.
- **mergeStrategy:** None observable — changes land directly on main (fast-forward/rebase). Zero merge commits across the whole history.
- **commitStyle:** No Conventional Commits, no ticket IDs. Imperative, sentence-case one-line subjects (mean ~57, median ~65 chars) naming the user-visible effect, optionally with a lightweight scope prefix (README:, Index:, Classifier:, fetchSizes:) used in 5 of 31 subjects.
- **commitBodies:** 90% of commits (28 of 31) carry a substantial explanatory body (5-27 non-blank lines) stating the upstream root cause before the fix — the same 'why over what' style as the inline comments.
- **commitScope:** One logical change per commit: 17 of 31 commits touch exactly one file, only 3 touch 3+ files; follow-up fixes are separate named commits, never amends.
- **trailers:** Recent commits end with a Co-Authored-By trailer for the AI assistant (e.g. 'Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>').
- **ignored:** .gitignore covers .env, .venv/, __pycache__/, *.pyc, .python-version, json/* (local workflow samples), .DS_Store, .s3-index.json. uv.lock IS committed; .env.example is committed as the documented template.
- **gitignorePolicy:** Owner preference for the babysitter gitignore gate: level 'logs-runs' — ignore .a5c/logs/ and .a5c/runs/, keep the project profile and processes tracked in git.
- **authors:** Effectively single-maintainer: Vitaliy Galkin under two git name identities against one email (vitaliygalkyn@gmail.com, 30 commits) plus one drive-by commit from antonl@xcritical.software.
- **destructiveOps:** Always stop and ask before any destructive git operation (force-push, reset --hard, branch or tag deletion, history rewrite).

**Import order:** `from __future__ import annotations` first, on its own line > stdlib plain `import` block, alphabetized: asyncio, json, os, re, shutil, time, uuid > stdlib `from` imports next: `from pathlib import Path`, `from typing import Any` > third-party last, alphabetized: `import yaml`, `from fastapi import FastAPI, HTTPException`, `from fastapi.responses import FileResponse, StreamingResponse`, `from pydantic import BaseModel` > No local/relative imports at all — the app is one module > Deliberate function-local imports for rarely used or startup-cost dependencies: `import urllib.request` inside fetch_cm_registry(), `import difflib` inside s3_size(), `import uvicorn` inside main() > Frontend has no imports — one inline script, no modules, no CDN dependencies

**Error handling:** Fail-fast on config, fail-soft on the I/O edges. (1) A missing COMFYUI_PATH raises RuntimeError at import time so the process never starts half-configured; config.yaml parse errors are swallowed to {}. (2) Caches and best-effort I/O degrade to a safe default: _load_index_from_disk and fetch_cm_registry use bare `except Exception` returning an empty index, _save_index/dir_bytes/disk_usage catch OSError and continue. (3) Subprocess calls never raise — _aws_text/_aws_ls return (returncode, text) rc-tuples and callers branch on `rc != 0`, typically returning an empty result; _probe converts FileNotFoundError into {'ok': False, 'error': 'aws CLI not found in PATH'} and truncates stderr to 300 chars. (4) HTTP layer: client errors are raised as fastapi.HTTPException with a status and a plain message (10 sites) — 400 for no items / bad prefix / no S3 base / invalid or escaping local_dest / insufficient disk, 404 for unknown job or unlistable prefix, and NOTHING ELSE at the HTTP edge. (5) Sync jobs: asyncio.gather(..., return_exceptions=True) so one failing item cannot abort the batch — exceptions become {'event':'error'} SSE messages and set overall_rc=1; cancellation is exit 130; the watchdog is cancelled in a `finally` absorbing CancelledError; proc.terminate() tolerates ProcessLookupError. (6) Security validation is explicit: _validate_local_dest resolves the path and rejects anything not under COMFY.resolve() (traversal guard), _check_disk_space refuses jobs needing >95% of free space. (7) Frontend: fetch failures surface via alert() after `r.json().catch(() => ({detail: r.statusText}))`, SSE error/exit events append to the console, preflight problems render as banners.

**Testing:** None exist. No test suite, no tests/ directory, no test_*.py, no pytest/unittest usage, no test dependency group, no coverage/tox config, no CI. Verification is manual/operational: the GET /status preflight (auth probe, models/nodes bucket listing, ComfyUI path check, free disk), POST /reindex timing stats and the 'unparsed_packages' list, plus the SSE console during syncs. Any future suite is greenfield; pytest + pytest-asyncio with fastapi TestClient and mocked _aws_text/_aws_ls would fit, since all S3 access funnels through those two helpers and _aws_s3(). The owner has explicitly deferred tests and CI for now.

### Additional Rules

- CONTRACT RULE (owner): any change to main.py that alters the /analyze, /size or /sync response contract must be checked against static/index.html in the same commit — the frontend keys rows straight off backend fields (178e53f was a real breakage from ignoring this).
- CREDENTIALS RULE (owner): never read or echo real credential values. .env stays untracked; only .env.example is edited. Break for confirmation before anything that would touch credentials or the user's ComfyUI install under COMFYUI_PATH.
- All S3 access goes through _aws_s3() so the custom --endpoint-url (R2/MinIO/Wasabi) is applied uniformly; never build a bare ['aws','s3',...] argv.
- Subprocesses are argv lists only, never shell strings.
- No boto3/SDK dependency is intentional — the AWS CLI is the sole S3 client, which is why sync progress is scraped from CLI stdout.
- Heuristic ordering is load-bearing and commented as such: in _NODE_FOLDER_HINTS/_INPUT_FOLDER_HINTS first match wins, so a new specific rule must be inserted ABOVE the broader one; _classify_node's numbered steps 1->7 encode a precedence (S3 parse > cnr_id/aux_id > hardcoded map > user override > CM builtins > CM entry present in S3 > vendor tag > CM/GitHub).
- Zero-config by design: config.yaml was deleted ('everything is auto-discovered now'); prefer inferring new behaviour from the S3 index and the CM registry over adding configuration.
- When a heuristic genuinely cannot be generalized, hardcode the exception and say so in the commit subject (the existing house rule, e.g. 548db62 'CM/parser blind spot').
- Fixes are pushed toward presentation when resolution is already correct — _suppress_phantom_models is explicitly labelled 'purely cosmetic: resolution logic is untouched, so no double downloads'.
- Every long-lived cache must carry a TTL and every unbounded fan-out must carry a Semaphore.
- Writes are confined to the ComfyUI tree: _validate_local_dest is called for every item before a job is created.
- Environment beats config files: secrets live in .env (gitignored) and every var is mirrored into .env.example in the same commit as the code that reads it.
- Keep the 'why' comments that encode ComfyUI ecosystem quirks — they are the project's only knowledge store outside commit bodies.
- Declared out of scope by design (README 'Scope notes'): PNG-embedded workflow parsing, git-based custom-node installation, authentication, multi-user support. There is no auth layer, so the service is assumed to run on a trusted host or behind a reverse proxy.
- Known drift to be aware of: pyproject declares only fastapi/uvicorn/pyyaml although pydantic is imported directly; README documents AWS_ENDPOINT_URL while .env.example and the code prefer AWS_ENDPOINT_URL_S3 (both are read); README states port 8765 without mentioning APP_PORT/PORT/APP_HOST; load_config() at main.py:54 is dead code superseded by _load_config_optional().

## Repositories

- **comfyui-workflow-sync** - https://github.com/vitaliyga/comfyui-workflow-sync [`/home/user/comfyui-workflow-sync`]

## CLAUDE.md Instructions

- Python is snake_case with a single leading underscore for module-private helpers; module-level compiled regexes are named *_RE.
- Ordered heuristic tables are named *_HINTS and their ORDER IS LOAD-BEARING (first match wins, specific before generic) — insert a new specific rule above the broader one, never reorder casually. _classify_node's numbered steps 1->7 encode the same kind of precedence.
- Give every new def modern type hints (`str | None`, `dict[str, Any]`); the file already runs `from __future__ import annotations`.
- Build subprocesses as argv lists only — never shell strings — and always route `aws s3 …` through _aws_s3() so --endpoint-url (R2/MinIO/Wasabi) is applied.
- Fail fast at import for missing COMFYUI_PATH; fail soft at runtime — _aws_text/_aws_ls return (rc, text) tuples and callers branch on rc != 0 rather than raising.
- Raise HTTPException only at the HTTP edge, and only 400 (bad request/insufficient disk/invalid dest) or 404 (unknown job/unlistable prefix).
- Keep the 'why' comments that encode ComfyUI ecosystem quirks — they are the project's knowledge store; explain the upstream root cause in the commit body too.
- Any change to main.py that alters the /analyze, /size or /sync contract must be checked against static/index.html in the same commit.
- Never read or echo real credential values. .env stays untracked; edit only .env.example, and mirror a new env var into it in the same commit as the code that reads it.
- Do not touch the user's ComfyUI install under COMFYUI_PATH without an explicit breakpoint; writes must stay inside COMFY via _validate_local_dest.
- Commit style: imperative sentence-case subject naming the user-visible effect, no Conventional Commits prefix, optional lightweight scope prefix (README:, Index:, Classifier:), plus an explanatory body; one logical change per commit.
- Trunk-based on main with linear history; agent work goes on claude/<slug> branches. Always stop and ask before any destructive git operation.
- Autonomy: semi-autonomous — run routine steps, but breakpoint on architecture decisions, destructive operations and deploys.
- Prefer auto-discovery over configuration: config.yaml was deliberately removed; infer new behaviour from the S3 index and the ComfyUI-Manager registry. If a heuristic truly cannot be generalized, hardcode the exception and say so in the subject.
- Every long-lived cache needs a TTL constant and every unbounded fan-out needs a Semaphore.
- No CI/lint/test gates exist and the owner has deferred them — do not add cosmetic pipelines; verify by running the app against a real workflow JSON.
- Babysitter is installed as the babysitter@a5c.ai Claude Code plugin plus the babysitter CLI; resolve the process library path with `babysitter process-library:active --json` (currently /root/.a5c/process-library/babysitter-repo/library) and treat every library path as relative to that binding.dir.
- Default to /babysitter:plan before /babysitter:call for anything touching main.py structure, the /analyze /size /sync contract, or the classifier; /babysitter:call (with breakpoints) is the normal run mode.
- /babysitter:yolo disables breakpoints — use it only for read-only work (codebase map, audits, docs analysis), never for edits to main.py, .env*, git history, or anything under COMFYUI_PATH.
- methodologies/gsd is the default methodology: quick.js --full for non-trivial work, verify-work.js as the stand-in for the missing reviewer, STATE.md + .planning/debug/<slug>.md as the memory across this project's multi-week gaps.
- Route every classifier misclassification through contrib/rogelsm/generic-bugfix.js composed with processes/shared/prior-attempts-scanner.js and processes/shared/n-strikes-escalation.js, so the third failed attempt escalates to an architecture breakpoint instead of another *_HINTS row.
- contrib/rogelsm/generic-bugfix.js ships a TypeScript Phase-4 gate — repoint it to `uv run python -m compileall -q main.py` (no env needed) or `uv run python -c "import main"` (needs a configured .env) before use.
- processes/shared/deterministic-quality-gate.js needs a hand-written Python gate (the library has no ruff/black/mypy asset); the highest-value check is that every os.environ/os.getenv key in main.py appears in .env.example and README.md.
- From processes/shared/local-dev/install-quality-gates.js install only the gitleaks and typos layers — skip eslint, commitlint and husky — and gate it behind a breakpoint because it commits its own changes.
- Set minTestCoverage low when running specializations/software-architecture/refactoring-plan.js; its default of 70 is unreachable in this repo.
- Do not select cradle/bugfix.js or cradle/bug-report.js (they PR against a5c-ai/babysitter itself) or methodologies/evolutionary.js (a genetic-algorithm loop, not evolutionary architecture).
- Adoption order: gsd/map-codebase.js -> here-be-dragons-audit.js -> classifier characterization tests -> generic-bugfix.js bug loop -> docs-audit.js then docs-testing.js -> refactoring-plan.js before any main.py extraction.
- CI/CD integration was explicitly skipped for this install: create nothing under .github/, and treat processes/shared/ci/*, processes/shared/release/semantic-release-setup.js, qa-testing-automation/continuous-testing.js and local-dev/feedback-loop-optimizer.js as out of scope.
- Add no JS toolchain for static/index.html; the Node-only library components (jest/cypress/stryker, vitest/react-testing-library, jsdoc/docusaurus/storybook skills, ts-check.js, playwright-visual-smoke.js) are deliberately excluded.

## Installed Extensions

- Skills: specializations/code-migration-modernization/skills/characterization-test-generator/SKILL.md, specializations/common-utilities/skills/python-implementation/SKILL.md, specializations/qa-testing-automation/skills/pytest-testing/SKILL.md, methodologies/superpowers/skills/systematic-debugging/SKILL.md, specializations/code-migration-modernization/skills/refactoring-assistant/SKILL.md, specializations/software-architecture/skills/code-complexity-analyzer/SKILL.md, specializations/technical-documentation/skills/code-sample-validator/SKILL.md, specializations/code-migration-modernization/skills/knowledge-extractor/SKILL.md, specializations/code-migration-modernization/skills/test-coverage-analyzer/SKILL.md, specializations/software-architecture/skills/dependency-graph-generator/SKILL.md, specializations/technical-documentation/skills/link-validator/SKILL.md, methodologies/gsd/skills/state-management/SKILL.md, methodologies/gsd/skills/git-integration/SKILL.md, methodologies/gsd/skills/verification-suite/SKILL.md
- Agents: methodologies/gsd/agents/gsd-debugger/AGENT.md, methodologies/gsd/agents/gsd-codebase-mapper/AGENT.md, methodologies/gsd/agents/gsd-executor/AGENT.md, methodologies/gsd/agents/gsd-verifier/AGENT.md, specializations/software-architecture/agents/refactoring-coach/AGENT.md, specializations/code-migration-modernization/agents/regression-detector/AGENT.md, specializations/technical-documentation/agents/tech-writer-expert/AGENT.md, specializations/code-migration-modernization/agents/legacy-system-archaeologist/AGENT.md, specializations/code-migration-modernization/agents/technical-debt-auditor/AGENT.md, specializations/technical-documentation/agents/dx-docs-specialist/AGENT.md, specializations/qa-testing-automation/agents/test-strategy-architect/AGENT.md
- Processes: methodologies/gsd/map-codebase.js, methodologies/gsd/debug.js, contrib/rogelsm/generic-bugfix.js, processes/shared/prior-attempts-scanner.js, processes/shared/n-strikes-escalation.js, specializations/software-architecture/here-be-dragons-audit.js, specializations/technical-documentation/docs-audit.js, specializations/technical-documentation/docs-testing.js, specializations/software-architecture/refactoring-plan.js, methodologies/gsd/quick.js, methodologies/gsd/verify-work.js, processes/shared/deterministic-quality-gate.js, specializations/software-architecture/evil-fallback-audit.js, specializations/code-migration-modernization/legacy-codebase-assessment.js, specializations/code-migration-modernization/technical-debt-remediation.js, specializations/technical-documentation/adr-docs.js, methodologies/gsd/add-tests.js, specializations/qa-testing-automation/test-strategy.js, processes/shared/local-dev/install-quality-gates.js, methodologies/tdd.js
