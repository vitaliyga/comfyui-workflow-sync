# CLAUDE.md

Guidance for Claude Code (and any agent) working in this repository.

## What this project is

**ComfyUI S3 Sync** — a single-operator, self-hosted web app that lives next to a ComfyUI install.
You drop a ComfyUI workflow JSON (standard or API export) into the browser UI; the app resolves
every model file and custom-node package the workflow needs, diffs that against what is already on
disk under `COMFYUI_PATH`, and on a button click fetches only the missing pieces by shelling out to
AWS CLI v2 (`aws s3 sync`) against S3 or an S3-compatible endpoint (Cloudflare R2, MinIO, Wasabi).

Resolution is index-driven: an S3 catalogue built from `aws s3 ls --recursive` plus per-package
`*_CLASS_MAPPINGS` parsing, enriched by the ComfyUI-Manager `extension-node-map.json` registry, and
cached to `.s3-index.json`.

There is no database, no auth, no multi-user story. Deployment is manual (a systemd unit, or a
RunPod `/workspace` install), documented in `README.md` prose only.

## Stack and layout

- **Python >= 3.11**, managed with **uv** (`uv.lock` is committed).
- **FastAPI + uvicorn[standard]** — *not* aiohttp, whatever an older brief may say. Pydantic v2 for
  request bodies (imported directly, but only a transitive dependency — known drift).
- **AWS CLI v2 is a hard runtime dependency.** Every S3 operation is a subprocess. There is no boto3.
- `main.py` — the entire backend, ~1490 lines, one file.
- `static/index.html` — the whole frontend: 938 lines of HTML + inline CSS + one inline vanilla-JS
  script. **No npm, no bundler, no build step.** It is served verbatim by `GET /`.
- `pyproject.toml`, `uv.lock`, `.env.example`, `README.md`. Nothing else.
- Not present, deliberately: tests, linter/formatter config, `Makefile`, `Dockerfile`, `.github/`,
  any CI configuration.

## Commands

```bash
uv sync                       # install from uv.lock
cp .env.example .env          # then fill it in — never commit .env
uv run python main.py         # serve on 0.0.0.0:8765
```

- Port: `APP_PORT` wins, then `PORT`, else `8765`. Host: `APP_HOST`, default `0.0.0.0`.
  `APP_PORT` exists so the app can match a RunPod-exposed port.
- The ASGI app object is `main:app`, so `uv run uvicorn main:app --reload` also works for iteration.
- Syntax check without any environment: `uv run python -m compileall -q main.py`.
- Import check (requires a configured `.env`): `uv run python -c "import main"` — importing `main.py`
  raises at import time when `COMFYUI_PATH` is unset. That is intentional.

Environment variables actually read by `main.py`: `COMFYUI_PATH` (required), `S3_MODELS_BASE`,
`S3_NODES_BASE`, `AWS_ENDPOINT_URL_S3` / `AWS_ENDPOINT_URL`, the standard `AWS_*` credential vars,
`INDEX_TTL` (default 86400), `SYNC_PARALLEL` (3), `SYNC_STALL_SECS` (8), `CM_REGISTRY_URL`,
`APP_PORT` / `PORT` / `APP_HOST`, and the legacy `CONFIG`.
Any new env var must be mirrored into `.env.example` **and** `README.md` in the same commit as the
code that reads it — this has drifted before.

## How it works

1. **Boot** — importing `main.py` runs `load_dotenv('.env')` (a hand-rolled parser with `${VAR}`
   expansion that uses `os.environ.setdefault`, so real platform env always wins), derives
   `COMFY` / `MODELS_DIR` / `NODES_DIR`, and loads any cached `.s3-index.json`.
2. **Index** (`main.py:126-410`) — `rebuild_index()` fans out three coroutines under `_REINDEX_LOCK`:
   one `aws s3 ls --recursive` over the models base; per-package `aws s3 cp - ` streaming of
   `__init__.py` / `nodes.py` to regex-parse `*CLASS_MAPPINGS` keys (BFS, semaphore-bounded); and an
   HTTPS fetch of ComfyUI-Manager's `extension-node-map.json` off-loop via `asyncio.to_thread`.
   Result is the module-global `S3_INDEX`, persisted to `.s3-index.json`, refreshed past `INDEX_TTL`.
3. **Analyze** — `POST /analyze`: `_is_api_format()` picks the extractor, model strings are pulled
   from `widgets_values` / `inputs` (recursing into rgthree-style dicts, skipping `on: false` slots),
   folder hints come from the class name and the API slot key, `_lookup_model()` resolves against the
   index, and each class name goes through `_classify_node()`'s 7-step precedence ladder.
4. **Size** — `POST /size`: answered from the index where possible, else `aws s3 ls` behind a 4-way
   semaphore, with `difflib` fuzzy candidates for renamed files.
5. **Sync** — `POST /sync`: every `local_dest` is validated to stay inside `COMFYUI_PATH` by
   `_validate_local_dest`, total expected bytes are checked against free disk, then `run_job()` runs
   up to `SYNC_PARALLEL` `aws s3 sync --exact-timestamps` subprocesses whose `Completed X/Y` stdout
   lines become byte-progress SSE events on `GET /sync/{job_id}/stream`. A watchdog flags a stalled
   source after `SYNC_STALL_SECS`; a synced custom-node dir gets `pip install -r requirements.txt`;
   `DELETE /sync/{job_id}` kills the subprocesses. Jobs expire from memory after `JOB_TTL_SECS=300`.

HTTP surface: `POST /analyze`, `GET /status`, `GET /browse`, `GET /search`, `POST /reindex`,
`POST /size`, `POST /sync`, `DELETE /sync/{job_id}`, `GET /sync/{job_id}/stream`, `GET /`.

## Conventions

- **Naming** — `snake_case`; a single leading underscore marks module-private helpers; module-level
  compiled regexes are named `*_RE`.
- **Ordered heuristic tables** — `_NODE_FOLDER_HINTS`, `_INPUT_FOLDER_HINTS` and friends are named
  `*_HINTS` and **their order is load-bearing**: first match wins, specific before generic
  (`CLIPVision` before `CLIP`). Insert a new specific rule *above* the broader one; never reorder
  casually. `_classify_node`'s numbered steps 1→7 encode the same kind of precedence.
- **Type hints** — every new `def` gets modern hints (`str | None`, `dict[str, Any]`); the file
  already runs `from __future__ import annotations`.
- **Subprocesses** — argv lists only, never shell strings, and every `aws s3 …` goes through
  `_aws_s3()` so `--endpoint-url` is applied for R2/MinIO/Wasabi.
- **Error handling** — fail fast at import for a missing `COMFYUI_PATH`; fail soft at runtime:
  `_aws_text` / `_aws_ls` return `(rc, text)` tuples and callers branch on `rc != 0` rather than
  raising. Raise `HTTPException` only at the HTTP edge, and only `400` (bad request, insufficient
  disk, invalid dest) or `404` (unknown job, unlistable prefix).
- **Comments** — keep the "why" comments that encode ComfyUI ecosystem quirks. They are this
  project's knowledge store. Explain the upstream root cause in the commit body too.
- **Caches and fan-out** — every long-lived cache gets a TTL constant (`INDEX_TTL`, `JOB_TTL_SECS`,
  `PREFLIGHT_TTL`); every unbounded fan-out gets an `asyncio.Semaphore`.
- **Auto-discovery over configuration** — `config.yaml` was deliberately removed. Infer new
  behaviour from the S3 index and the ComfyUI-Manager registry. If a heuristic genuinely cannot be
  generalized, hardcode the exception and say so in the commit subject.

### The one coupling rule

**Any change to `main.py` that alters the `/analyze`, `/size` or `/sync` response contract must be
checked against `static/index.html` in the same commit.** The SPA reads those JSON fields directly
with no schema, no types and no tests. A refactor has already removed a field the progress bars read
and shipped the breakage.

## Git

- Trunk-based on `main`, strictly linear history — zero merges, zero tags, no PR requirement.
  Agent work goes on `claude/<slug>` branches.
- One logical change per commit. Follow-up fixes are separate named commits, not amends.
- Subject: imperative, sentence-case, **no** Conventional Commits prefix; an optional lightweight
  scope prefix is fine (`README:`, `Index:`, `Classifier:`). Body: explain the upstream root cause
  before the fix.
- **Always stop and ask before any destructive git operation.**

## Safety

- **Credentials**: never read, print, echo or log a real value from `.env`, `~/.aws/credentials` or
  the environment. `.env` is untracked and stays that way; edit only `.env.example`.
- **The user's ComfyUI install**: do not touch anything under `COMFYUI_PATH` without an explicit
  breakpoint. Writes must stay inside `COMFY` via `_validate_local_dest` — never widen that check.
- **Verification is manual.** No CI, no linter, no tests exist, and the owner has deferred them —
  do not add cosmetic pipelines to fake a gate. Verify by running the app against a real workflow
  JSON and checking `GET /status` preflight, `POST /reindex` stats, and the SSE console.

## Babysitter

This repo is set up for the a5c **Babysitter** orchestrator: the `babysitter@a5c.ai` Claude Code
plugin (slash commands + the `babysitter:babysit` skill) and the `babysitter` CLI.
The process library it draws from is a local clone; resolve its path at any time with:

```bash
babysitter process-library:active --json     # -> .binding.dir, currently
                                             #    /root/.a5c/process-library/babysitter-repo/library
```

All library paths below are relative to that `binding.dir`. Every one of them was verified to
exist on disk at install time.

### Commands

Slash commands (inside Claude Code):

| Command | Use it for |
| --- | --- |
| `/babysitter:plan <goal>` | Draft the process **without running it**. Default first step for anything that touches `main.py` structure, the `/analyze` `/size` `/sync` contract, or the classifier. |
| `/babysitter:call <goal>` | Run a process with human breakpoints. **This is the normal mode for this repo.** |
| `/babysitter:yolo <goal>` | Run with no breakpoints. Only acceptable for read-only work (codebase map, audits, docs analysis). Never for edits to `main.py`, `.env*`, git history, or anything under `COMFYUI_PATH`. |
| `/babysitter:resume [run-id]` | Resume an incomplete run — the right entry point after one of this project's multi-day gaps. |
| `/babysitter:observe` | Real-time run dashboard (`npx @a5c-ai/babysitter-observer-dashboard`). Watch long `aws s3 sync` orchestrations here rather than in scrollback. |
| `/babysitter:retrospect [run-id\|--all]` | Post-run analysis and process improvements. |
| `/babysitter:doctor [run-id]` | 14-point health check on a run (journal integrity, state cache, effects, locks, logs). |
| `/babysitter:cleanup [--dry-run] [--keep-days N]` | Aggregate insights from old runs, then prune `.a5c/runs`. Run with `--dry-run` first. |
| `/babysitter:blueprints` | List/install/update blueprint packages. |
| `/babysitter:help`, `/babysitter:forever` | Plugin help; never-ending scheduled loops (not used here). |

CLI (for inspecting runs; the slash commands drive them):

```bash
babysitter process-library:active --json        # where the process library lives
babysitter profile:read --project --json        # this project's babysitter profile (.a5c/ in the repo)
babysitter skill:discover --json                # skills visible to a run
babysitter run:status  <runDir> --json          # one run's status
babysitter run:events  <runDir> --json --limit 50
babysitter task:list   <runDir> --pending --json
babysitter run:iterate <runDir> --verbose       # push a stalled run forward
```

Runs are stored under `~/.a5c/runs` by default; set `BABYSITTER_RUNS_SCOPE=repo` to keep them in
`<repo>/.a5c/runs` instead.

### Methodology: GSD (default)

`methodologies/gsd` (see `methodologies/gsd/README.md`) is the default operating methodology here,
because its shape matches this repo exactly: solo maintainer, trunk-based on `main`, no PRs, no
reviewers, no releases, no CI. It gives atomic commits, a persistent `.planning/STATE.md`, and
explicit plan/execute/verify breakpoints — the semi-autonomous posture below — without assuming a
pipeline that does not exist.

It also directly answers this project's biggest sustainability risk (bursty sessions, once 77 days
apart, on a codebase whose logic lives in the maintainer's head): `STATE.md`, the
`.planning/debug/<slug>.md` sessions from `gsd/debug.js`, and the codebase map mean a session
resumed months later starts from written context instead of re-reading 1490 lines of `main.py`.

Use `methodologies/gsd/quick.js --full` for anything non-trivial, so its plan-check plus
`methodologies/gsd/verify-work.js` goal-backward verification stands in for the reviewer this
project does not have. GSD atomic commits are the rollback granularity — there are no tags.

**Note on the usual heuristic:** zero test coverage would normally mean "adopt TDD".
The owner explicitly deprioritized tests, so `methodologies/tdd.js` is scoped to the four hot
classifier functions only (`extract_models`, `extract_models_api`, `extract_custom_nodes`,
`_classify_node`) — not to the project as a whole.

### Installed processes and when to invoke them

Invoke via `/babysitter:call` (or `/babysitter:plan` first) naming the process path.

**Core loop**

- `methodologies/gsd/map-codebase.js` — start here. Produces the explicit `main.py` ↔
  `static/index.html` coupling map. Read-only, so `/babysitter:yolo` is fine.
- `methodologies/gsd/debug.js` — any bug not resolved in one pass; the hypothesis survives the gaps
  between sessions.
- `methodologies/gsd/quick.js` — small scoped changes; `--full` for anything non-trivial.
- `methodologies/gsd/verify-work.js` — goal-backward verification, the stand-in for code review.

**The standing bug loop (owner pain point #1: the classifier)**

- `contrib/rogelsm/generic-bugfix.js` — route every misclassification report through this:
  diagnostic checklist → human breakpoint on the diagnosis → implement → gate → edge-case matrix →
  human breakpoint on the outcome. **Repoint its Phase-4 gate before use** (see configuration notes).
- `processes/shared/prior-attempts-scanner.js` — compose so attempt N reads attempts 1..N-1.
  15 of 31 commits patch the same four functions; this is what stops attempt 16 from re-treading them.
- `processes/shared/n-strikes-escalation.js` — compose so the third failed attempt raises a
  breakpoint about the classification *architecture* instead of adding another `*_HINTS` row.

**Audits**

- `specializations/software-architecture/here-be-dragons-audit.js` — mark the dangerous zones,
  especially the JSON contract between `run_job` and the SPA. Also surfaces the dead `config.yaml`
  loader and the still-declared `pyyaml` dependency.
- `specializations/software-architecture/evil-fallback-audit.js` — the classifier's failure mode is
  silent degradation (registry miss → heuristics → hardcoded table). Inventory every catch-and-swallow
  and make the unknown case explicit in the UI instead of guessed.

**Documentation (owner pain point #2)**

- `specializations/technical-documentation/docs-audit.js` — diagnose: `README.md` has not moved
  since `1f04a23` while `main.py` kept changing, and `.env.example` is missing vars `main.py` reads.
- `specializations/technical-documentation/docs-testing.js` — enforce: it can actually run the
  documented setup/run commands and confirm documented env vars and endpoints still exist.
- `specializations/technical-documentation/adr-docs.js` — fix the bus-factor knowledge in place as
  short ADRs (why the hardcoded per-package table exists, how fork-duplicate registry entries are
  resolved, why nested lora subpaths are special-cased).

**Refactoring (owner pain point #3)**

- `specializations/software-architecture/refactoring-plan.js` — budgeted extraction roadmap for the
  1490-line `main.py`, along the seams already identified: classifier, S3 index builder, sync runner.
  Every extraction is an architecture change and therefore hits the architecture breakpoint by design.
- `specializations/code-migration-modernization/legacy-codebase-assessment.js`,
  `specializations/code-migration-modernization/technical-debt-remediation.js` — optional, for
  sizing the debt before committing to the roadmap.

**Optional / offered, not pushed**

- `processes/shared/deterministic-quality-gate.js` — the CI-less substitute, run locally.
- `processes/shared/local-dev/install-quality-gates.js` — install **only** the gitleaks and typos
  layers (see configuration notes).
- `methodologies/gsd/add-tests.js`, `specializations/qa-testing-automation/test-strategy.js` —
  parked by owner decision. Do not open a general test-coverage program.

### Skills

Skill files under the library; reference them by path when a process asks for a skill.

- `specializations/common-utilities/skills/python-implementation/SKILL.md` — the default
  implementation skill for this repo.
- `specializations/code-migration-modernization/skills/characterization-test-generator/SKILL.md` —
  build the classifier regression corpus from real workflow JSON: every historical misclassification
  (rgthree Power Lora Loader widget shape, LTXVideo class names, ComfyUI-Manager duplicate repo
  entries, nested `prod/X` lora subpaths) becomes a pinned case.
- `specializations/qa-testing-automation/skills/pytest-testing/SKILL.md` — executes those
  characterization tests. This is how the safety net arrives: as a by-product of classifier work.
- `methodologies/superpowers/skills/systematic-debugging/SKILL.md` — pairs with `gsd/debug.js`.
- `specializations/code-migration-modernization/skills/refactoring-assistant/SKILL.md` — one
  extraction at a time out of `main.py`.
- `specializations/code-migration-modernization/skills/knowledge-extractor/SKILL.md` — mine the 31
  commit bodies, which are where this project's ecosystem knowledge currently lives.
- `methodologies/gsd/skills/state-management/SKILL.md` — `.planning/STATE.md` upkeep.
- `methodologies/gsd/skills/git-integration/SKILL.md` — atomic commits in this repo's commit style.
- `methodologies/gsd/skills/verification-suite/SKILL.md`,
  `specializations/software-architecture/skills/code-complexity-analyzer/SKILL.md`,
  `specializations/software-architecture/skills/dependency-graph-generator/SKILL.md`,
  `specializations/technical-documentation/skills/code-sample-validator/SKILL.md`,
  `specializations/technical-documentation/skills/link-validator/SKILL.md`,
  `specializations/code-migration-modernization/skills/test-coverage-analyzer/SKILL.md` —
  situational.

### Agents

- `methodologies/gsd/agents/gsd-codebase-mapper/AGENT.md` — drives `map-codebase.js`.
- `methodologies/gsd/agents/gsd-debugger/AGENT.md` — drives `debug.js`.
- `methodologies/gsd/agents/gsd-executor/AGENT.md` — executes planned steps (checkpoints built in).
- `methodologies/gsd/agents/gsd-verifier/AGENT.md` — drives `verify-work.js`.
- `specializations/software-architecture/agents/refactoring-coach/AGENT.md` — `main.py` extractions.
- `specializations/code-migration-modernization/agents/regression-detector/AGENT.md` —
  behavior comparison after each extraction; the guard against another `index.html` breakage.
- `specializations/technical-documentation/agents/tech-writer-expert/AGENT.md` — drives the docs processes.
- Situational: `.../legacy-system-archaeologist/AGENT.md`, `.../technical-debt-auditor/AGENT.md`,
  `.../dx-docs-specialist/AGENT.md`, `.../test-strategy-architect/AGENT.md`.

### Autonomy policy: semi-autonomous

Run routine steps without asking. **Stop and raise a breakpoint** for:

1. **Architecture decisions** — splitting `main.py`, changing the `/analyze` `/size` `/sync` JSON
   contract, changing how the S3 index is built or cached, adding a dependency, or adding a
   configuration file (auto-discovery is a deliberate design choice — `config.yaml` was removed).
2. **Destructive operations** — and *always* for destructive git: no `push --force`, `reset --hard`,
   `rebase`, `commit --amend` on pushed work, branch/tag deletion, `clean -fdx`, or history rewriting
   without explicit confirmation. Deleting or overwriting local files outside the repo counts too.
3. **Deploys** — restarting the systemd unit, touching the RunPod `/workspace` install, or anything
   that changes what is running for the user.
4. **Credentials — always.** Never read, print, echo, log or paste a real value from `.env`,
   `~/.aws/credentials`, or the environment. `.env` stays untracked; only `.env.example` is edited,
   and a new env var is mirrored into it in the same commit as the code that reads it. If a process
   step would surface a secret, stop first.
5. **The user's ComfyUI install — always.** Anything under `COMFYUI_PATH` (`models/`,
   `custom_nodes/`), any real `aws s3 sync` into it, and the sync worker's
   `pip install -r requirements.txt` inside a freshly synced node directory. Writes must stay inside
   `COMFY` via `_validate_local_dest`; never widen that check to get a run unstuck.

`/babysitter:yolo` disables breakpoints and therefore must not be used for any of the above.

The chosen processes already carry these breakpoints natively — GSD executor checkpoints, the two
human breakpoints in `contrib/rogelsm/generic-bugfix.js`, and the escalation breakpoint in
`processes/shared/n-strikes-escalation.js`. Two calls need a breakpoint added explicitly:
`install-quality-gates.js` (it commits its own changes) and any `pip install` of synced node
requirements (it touches the user's ComfyUI install).

### Project-specific configuration notes

- **No CI/CD. CI/CD integration was explicitly skipped by the owner for this install.** There is no
  pipeline, no `.github/` directory, and none should be created. Do not add cosmetic pipelines or
  status badges. Consequently these library components are out of scope:
  `processes/shared/ci/build-failure-triage.js`, `processes/shared/ci/build-fixer.js`,
  `processes/shared/ci/ci-health-trends.js`, `processes/shared/ci/conflict-resolution.js`,
  `processes/shared/release/semantic-release-setup.js`,
  `specializations/qa-testing-automation/continuous-testing.js`,
  `.../agents/cicd-test-integration`, and `processes/shared/local-dev/feedback-loop-optimizer.js`
  (it audits CI workflows and opens GitHub issues; this repo runs neither).
- **Verification is manual and must stay real.** There is no test suite, linter or type checker to
  hide behind. Verify a change by running the app against a real workflow JSON: `uv sync`,
  `uv run python main.py`, open `http://localhost:8765`, drop the workflow, check `GET /status`
  preflight, `POST /reindex` stats and the SSE console.
- **`contrib/rogelsm/generic-bugfix.js` Phase-4 gate is TypeScript.** On this uv/Python repo that
  gate is a no-op or an outright failure — repoint it before first use. Working substitutes:
  `uv run python -m compileall -q main.py` (syntax gate, needs no environment) and
  `uv run python -c "import main"` (full import gate — only works with a configured `.env`, since
  `main.py` raises at import when `COMFYUI_PATH` is unset).
- **`processes/shared/deterministic-quality-gate.js` needs a hand-written Python gate.** The library's
  `assets/code-quality` holds only `eslint.config.js`, `typos.toml` and commitlint/husky configs —
  there is no ruff/black/mypy asset. The most valuable hand-written check for this project is the
  docs-drift one: every `os.environ` / `os.getenv` key read in `main.py` must appear in
  `.env.example` and in `README.md`.
- **`processes/shared/local-dev/install-quality-gates.js`: install only the gitleaks and typos
  layers.** Skip eslint, commitlint and husky — they need an npm toolchain this uv project does not
  and should not have. Gitleaks is the one with real value: this app handles AWS/R2 credentials.
  The process commits its own changes, so gate it behind a breakpoint.
- **`refactoring-plan.js`: set `minTestCoverage` low.** Its default of 70 is unreachable here.
- **No JS toolchain for `static/index.html`.** It is one 938-line vanilla-JS file with no npm
  project. Node-specific library components are deliberately excluded: the jest/cypress/stryker,
  vitest/react-testing-library/github-actions-web/docker-web, and jsdoc-tsdoc/docusaurus/storybook
  skills, plus `processes/shared/ts-check.js`. `processes/shared/playwright-visual-smoke.js` is the
  only library component that would have caught the historical progress-bar breakage in
  `index.html`, but pulling Playwright into a uv project for one file is not justified today — the
  substitute is the hard rule that a contract change in `main.py` is checked against
  `static/index.html` in the same commit.
- **Naming traps — do not select these on the strength of their names:** `cradle/bugfix.js` and
  `cradle/bug-report.js` fork and PR against `a5c-ai/babysitter` itself (upstream contribution, not
  project bugfixing); `methodologies/evolutionary.js` is a genetic-algorithm loop, not evolutionary
  architecture.
- **Known library gaps** (nothing is recommended rather than inventing a name): no README-vs-code
  drift detector, no Python lint/format quality-gate asset, and no process for hardening a shell-out
  integration such as this project's dependence on the AWS CLI v2 binary.

### Suggested adoption order

Smallest first, each step earning the next:

1. `methodologies/gsd/map-codebase.js` — get the coupling map written down.
2. `specializations/software-architecture/here-be-dragons-audit.js` — mark the dangerous zones.
3. Characterization tests for the classifier, via the characterization-test-generator and
   pytest-testing skills.
4. Wire `contrib/rogelsm/generic-bugfix.js` (with the Python gate) plus `prior-attempts-scanner.js`
   and `n-strikes-escalation.js` as the standing bug loop.
5. `docs-audit.js`, then `docs-testing.js`, for the README / `.env.example` gap.
6. `refactoring-plan.js` — and only then begin extracting modules from `main.py`.

### Reference

- Process library README: `README.md` at the `binding.dir` from `babysitter process-library:active --json`.
- GSD methodology: `methodologies/gsd/README.md` (plus its `*.js` processes, `skills/` and `agents/`).
- Plugin commands: `/babysitter:help`; per-command docs in the installed `babysitter@a5c.ai` plugin's
  `commands/` directory.
- CLI reference: `babysitter --help` (agent-facing) or `babysitter --help-human`.
