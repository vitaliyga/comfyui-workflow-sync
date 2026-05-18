from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CONFIG", ROOT / "config.yaml"))


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


load_dotenv(ROOT / ".env")


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _strip_slash(s: str) -> str:
    return s.rstrip("/")


CFG = load_config()
_comfy_env = os.environ.get("COMFYUI_PATH")
COMFY = Path(_comfy_env or CFG.get("comfyui_path", ""))
if not _comfy_env and not CFG.get("comfyui_path"):
    raise RuntimeError("Set COMFYUI_PATH in .env (or comfyui_path in config.yaml)")
MODELS_DIR = COMFY / "models"
NODES_DIR = COMFY / "custom_nodes"
MODEL_EXTS = tuple(CFG.get("model_extensions", [".safetensors"]))
BUILTIN = set(CFG.get("builtin_nodes", []))
MODEL_NODE_MAP: dict[str, tuple[int, str]] = {
    k: (v[0], v[1]) for k, v in CFG.get("model_node_map", {}).items()
}
MODEL_FOLDERS: list[str] = CFG.get("model_folders", [])
NODE_PACKAGES: dict[str, str] = CFG.get("node_packages", {})

S3_MODELS_BASE = _strip_slash(os.environ.get("S3_MODELS_BASE", ""))
S3_NODES_BASE = _strip_slash(os.environ.get("S3_NODES_BASE", ""))


def model_s3_url(folder: str, rel: str) -> str | None:
    if not S3_MODELS_BASE:
        return None
    return f"{S3_MODELS_BASE}/{folder}/{rel}"


def node_s3_url(package: str) -> str | None:
    if not S3_NODES_BASE:
        return None
    return f"{S3_NODES_BASE}/{package}/"

app = FastAPI()

JOBS: dict[str, dict[str, Any]] = {}


def extract_models(workflow: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in workflow.get("nodes", []):
        nt = node.get("type")
        if nt not in MODEL_NODE_MAP:
            continue
        idx, folder = MODEL_NODE_MAP[nt]
        wv = node.get("widgets_values") or []
        if idx >= len(wv):
            continue
        val = wv[idx]
        if not isinstance(val, str):
            continue
        if not val.lower().endswith(MODEL_EXTS):
            continue
        rel = val.replace("\\", "/")
        key = (folder, rel)
        if key in seen:
            continue
        seen.add(key)
        local_path = MODELS_DIR / folder / rel
        out.append({
            "file": rel,
            "folder": folder,
            "local_path": str(local_path),
            "exists_locally": local_path.exists(),
            "s3_source": model_s3_url(folder, rel),
            "s3_exists": None,
        })
    return out


def match_package(node_type: str) -> str | None:
    for hint, pkg in NODE_PACKAGES.items():
        if hint in node_type:
            return pkg
    return None


def extract_custom_nodes(workflow: dict) -> list[dict]:
    out: list[dict] = []
    seen_pkg: set[str] = set()
    seen_unknown: set[str] = set()
    for node in workflow.get("nodes", []):
        nt = node.get("type")
        if not nt or nt in BUILTIN:
            continue
        pkg = match_package(nt)
        if pkg is None:
            if nt in seen_unknown:
                continue
            seen_unknown.add(nt)
            out.append({
                "node_type": nt,
                "package_hint": None,
                "local_path": None,
                "exists_locally": None,
                "s3_source": None,
                "s3_exists": None,
                "status": "unknown",
            })
            continue
        if pkg in seen_pkg:
            continue
        seen_pkg.add(pkg)
        local_path = NODES_DIR / pkg
        out.append({
            "node_type": nt,
            "package_hint": pkg,
            "local_path": str(local_path),
            "exists_locally": local_path.exists(),
            "s3_source": node_s3_url(pkg),
            "s3_exists": None,
            "status": "ok" if local_path.exists() else "missing",
        })
    return out


class AnalyzeBody(BaseModel):
    workflow: dict


@app.post("/analyze")
def analyze(body: AnalyzeBody):
    return {
        "models": extract_models(body.workflow),
        "custom_nodes": extract_custom_nodes(body.workflow),
    }


PREFLIGHT_CACHE: dict[str, Any] | None = None
PREFLIGHT_TS: float = 0.0
PREFLIGHT_TTL = 120.0


async def _probe(args: list[str]) -> dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        return {
            "ok": proc.returncode == 0,
            "error": None if proc.returncode == 0 else err.decode("utf-8", "replace").strip()[:300],
        }
    except FileNotFoundError:
        return {"ok": False, "error": "aws CLI not found in PATH"}


async def preflight() -> dict[str, Any]:
    global PREFLIGHT_CACHE, PREFLIGHT_TS
    if PREFLIGHT_CACHE is not None and (time.monotonic() - PREFLIGHT_TS) < PREFLIGHT_TTL:
        return PREFLIGHT_CACHE
    auth = await _probe(["aws", "sts", "get-caller-identity"])
    models_check = (
        await _probe(["aws", "s3", "ls", S3_MODELS_BASE + "/"])
        if S3_MODELS_BASE else {"ok": False, "error": "S3_MODELS_BASE not set"}
    )
    nodes_check = (
        await _probe(["aws", "s3", "ls", S3_NODES_BASE + "/"])
        if S3_NODES_BASE else None
    )
    comfy_ok = COMFY.exists() and (COMFY / "models").exists()
    PREFLIGHT_CACHE = {
        "auth": auth,
        "models_bucket": models_check,
        "nodes_bucket": nodes_check,
        "comfyui": {"ok": comfy_ok, "path": str(COMFY),
                    "error": None if comfy_ok else "ComfyUI path missing or has no models/ subdir"},
    }
    PREFLIGHT_TS = time.monotonic()
    return PREFLIGHT_CACHE


@app.get("/status")
async def status(refresh: bool = False):
    global PREFLIGHT_CACHE
    if refresh:
        PREFLIGHT_CACHE = None
    models: dict[str, list[str]] = {}
    if MODELS_DIR.exists():
        for folder in MODEL_FOLDERS:
            p = MODELS_DIR / folder
            if p.exists():
                models[folder] = sorted(
                    str(f.relative_to(p))
                    for f in p.rglob("*")
                    if f.is_file() and f.suffix.lower() in MODEL_EXTS
                )
            else:
                models[folder] = []
    nodes: list[str] = []
    if NODES_DIR.exists():
        nodes = sorted(d.name for d in NODES_DIR.iterdir() if d.is_dir())
    try:
        free = shutil.disk_usage(COMFY if COMFY.exists() else COMFY.parent).free
    except OSError:
        free = None
    return {
        "comfyui_path": str(COMFY),
        "models": models,
        "custom_nodes": nodes,
        "disk_free": free,
        "preflight": await preflight(),
        "parallel": PARALLEL_SEM_SIZE,
    }


class SyncItem(BaseModel):
    s3_source: str
    local_dest: str
    is_file: bool = False
    expected_bytes: int | None = None


class SyncBody(BaseModel):
    items: list[SyncItem]


class SizeBody(BaseModel):
    sources: list[str]


async def _aws_ls(uri: str, recursive: bool = False, summarize: bool = False) -> tuple[int, str]:
    args = ["aws", "s3", "ls", uri]
    if recursive:
        args.append("--recursive")
    if summarize:
        args.append("--summarize")
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode()


async def list_prefix(prefix: str) -> list[tuple[str, int]]:
    """List immediate-file entries under prefix: [(name, bytes), ...]."""
    rc, out = await _aws_ls(prefix)
    if rc != 0:
        return []
    items: list[tuple[str, int]] = []
    for line in out.splitlines():
        parts = line.split(maxsplit=3)
        # Format: DATE TIME SIZE NAME  (PRE for dirs — skipped via isdigit check)
        if len(parts) == 4 and parts[2].isdigit():
            items.append((parts[3], int(parts[2])))
    return items


async def s3_size(source: str) -> dict[str, Any]:
    """For a file: try exact, fall back to fuzzy candidates in parent.
    For a prefix (ends with /): recursive summary."""
    if source.endswith("/"):
        rc, out = await _aws_ls(source, recursive=True, summarize=True)
        if rc != 0:
            return {"bytes": None, "files": None, "candidates": []}
        total_bytes = 0
        file_count = 0
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Total Size:"):
                total_bytes = int(line.split(":", 1)[1].strip())
            elif line.startswith("Total Objects:"):
                file_count = int(line.split(":", 1)[1].strip())
        return {"bytes": total_bytes, "files": file_count, "candidates": []}

    # single file
    rc, out = await _aws_ls(source)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) == 4 and parts[2].isdigit():
                return {"bytes": int(parts[2]), "files": 1, "candidates": []}

    # not found: fuzzy match in parent
    import difflib
    parent, _, filename = source.rpartition("/")
    parent += "/"
    listing = await list_prefix(parent)
    name_to_bytes = dict(listing)
    matches = difflib.get_close_matches(filename, list(name_to_bytes.keys()), n=5, cutoff=0.4)
    candidates = [
        {"name": m, "s3_source": parent + m, "bytes": name_to_bytes[m]}
        for m in matches
    ]
    return {"bytes": None, "files": 0, "candidates": candidates}


@app.post("/size")
async def size(body: SizeBody):
    results = await asyncio.gather(*(s3_size(s) for s in body.sources))
    return {"sizes": dict(zip(body.sources, results))}


def dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def is_file_source(item: SyncItem) -> bool:
    return item.is_file or any(
        item.s3_source.lower().endswith(ext) for ext in MODEL_EXTS
    )


def build_command(item: SyncItem) -> list[str]:
    if is_file_source(item):
        src = item.s3_source
        parent, _, filename = src.rpartition("/")
        parent += "/"
        local_dir = str(Path(item.local_dest).parent)
        return [
            "aws", "s3", "sync", parent, local_dir,
            "--exact-timestamps",
            "--exclude", "*",
            "--include", filename,
        ]
    src = item.s3_source if item.s3_source.endswith("/") else item.s3_source + "/"
    dst = item.local_dest if item.local_dest.endswith("/") else item.local_dest + "/"
    return ["aws", "s3", "sync", src, dst, "--exact-timestamps"]


_UNIT_FACTORS = {
    "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
    "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
}
_PROGRESS_RE = re.compile(r"Completed\s+([\d.]+)\s+(\w+)/([\d.]+)\s+(\w+)")


def _to_bytes(value: float, unit: str) -> int:
    return int(value * _UNIT_FACTORS.get(unit, 1))


PARALLEL_LIMIT = int(os.environ.get("SYNC_PARALLEL", "3"))
STALL_THRESHOLD = float(os.environ.get("SYNC_STALL_SECS", "8"))
PARALLEL_SEM_SIZE = max(1, PARALLEL_LIMIT)


async def process_item(idx: int, item: SyncItem, q: asyncio.Queue,
                       sem: asyncio.Semaphore, job: dict[str, Any]) -> int:
    async with sem:
        if job["cancelled"]:
            await q.put({"event": "exit", "item_index": idx, "exit_code": 130})
            return 130

        cmd = build_command(item)
        await q.put({
            "event": "start",
            "item_index": idx,
            "s3_source": item.s3_source,
            "local_dest": item.local_dest,
            "expected_bytes": item.expected_bytes,
            "cmd": " ".join(cmd),
        })

        local_target = Path(item.local_dest)
        is_file_dest = bool(local_target.suffix)
        mkdir_target = local_target.parent if is_file_dest else local_target
        mkdir_target.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        job["procs"].append(proc)

        state = {"bytes": 0, "ts": time.monotonic(), "stalled": False}

        async def watchdog():
            while True:
                await asyncio.sleep(2)
                idle = time.monotonic() - state["ts"]
                if idle > STALL_THRESHOLD and not state["stalled"]:
                    state["stalled"] = True
                    await q.put({"event": "stalled", "item_index": idx, "stalled": True})
                elif idle <= STALL_THRESHOLD and state["stalled"]:
                    state["stalled"] = False
                    await q.put({"event": "stalled", "item_index": idx, "stalled": False})

        wd = asyncio.create_task(watchdog())

        try:
            assert proc.stdout is not None
            buf = b""
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts[-1]
                for raw in parts[:-1]:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    m = _PROGRESS_RE.search(line)
                    if m:
                        cur = _to_bytes(float(m.group(1)), m.group(2))
                        if cur > state["bytes"]:
                            state["bytes"] = cur
                            state["ts"] = time.monotonic()
                            await q.put({
                                "event": "progress",
                                "item_index": idx,
                                "bytes_downloaded": cur,
                            })
                    else:
                        await q.put({
                            "event": "log",
                            "item_index": idx,
                            "line": line,
                        })
            rc = await proc.wait()
        finally:
            wd.cancel()
            try:
                await wd
            except asyncio.CancelledError:
                pass

        # final bump to 100% on success
        if rc == 0 and item.expected_bytes:
            await q.put({
                "event": "progress",
                "item_index": idx,
                "bytes_downloaded": item.expected_bytes,
            })

        await q.put({"event": "exit", "item_index": idx, "exit_code": rc})

        # post-sync: pip install for custom node dirs
        if rc == 0 and not is_file_source(item):
            req = Path(item.local_dest) / "requirements.txt"
            if req.exists() and not job["cancelled"]:
                await q.put({
                    "event": "log", "item_index": idx,
                    "line": f"installing {req}"
                })
                pip = await asyncio.create_subprocess_exec(
                    "pip", "install", "-r", str(req),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                job["procs"].append(pip)
                assert pip.stdout is not None
                async for line in pip.stdout:
                    await q.put({
                        "event": "log", "item_index": idx,
                        "line": line.decode("utf-8", "replace").rstrip(),
                    })
                await pip.wait()
        return rc


async def run_job(job_id: str, items: list[SyncItem]):
    job = JOBS[job_id]
    q: asyncio.Queue = job["queue"]
    sem = asyncio.Semaphore(PARALLEL_SEM_SIZE)
    overall_rc = 0
    try:
        results = await asyncio.gather(
            *(process_item(i, it, q, sem, job) for i, it in enumerate(items)),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                await q.put({"event": "error", "message": str(r)})
                overall_rc = 1
            elif isinstance(r, int) and r != 0:
                overall_rc = r
    finally:
        if job["cancelled"]:
            overall_rc = 130
        job["exit_code"] = overall_rc
        job["done"] = True
        await q.put({"event": "done", "exit_code": overall_rc})
        _schedule_job_cleanup(job_id)


def _check_disk_space(items: list[SyncItem]) -> tuple[int, int]:
    total = sum((it.expected_bytes or 0) for it in items)
    try:
        free = shutil.disk_usage(COMFY if COMFY.exists() else COMFY.parent).free
    except OSError:
        free = 0
    return total, free


def _validate_local_dest(dest: str) -> None:
    """Reject paths that escape COMFY (prevents .. traversal)."""
    try:
        resolved = Path(dest).resolve()
    except (OSError, RuntimeError) as e:
        raise HTTPException(400, f"invalid local_dest: {e}")
    root = COMFY.resolve()
    if resolved != root and root not in resolved.parents:
        raise HTTPException(400, f"local_dest outside ComfyUI path: {dest}")


JOB_TTL_SECS = 300.0


def _schedule_job_cleanup(job_id: str) -> None:
    loop = asyncio.get_event_loop()
    loop.call_later(JOB_TTL_SECS, JOBS.pop, job_id, None)


@app.post("/sync")
async def sync(body: SyncBody):
    if not body.items:
        raise HTTPException(400, "no items")
    for it in body.items:
        _validate_local_dest(it.local_dest)
    needed, free = _check_disk_space(body.items)
    if needed and free and needed > free * 0.95:
        raise HTTPException(
            400,
            f"not enough disk space: need {needed} bytes, free {free} bytes",
        )
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "queue": asyncio.Queue(),
        "done": False,
        "exit_code": None,
        "cancelled": False,
        "procs": [],
    }
    asyncio.create_task(run_job(job_id, body.items))
    return {"job_id": job_id, "parallel": PARALLEL_SEM_SIZE}


@app.delete("/sync/{job_id}")
async def cancel_sync(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    job["cancelled"] = True
    killed = 0
    for proc in job["procs"]:
        if proc.returncode is None:
            try:
                proc.terminate()
                killed += 1
            except ProcessLookupError:
                pass
    await job["queue"].put({"event": "log", "line": f"[cancelled — {killed} proc(s) terminated]"})
    return {"cancelled": True, "terminated": killed}


@app.get("/sync/{job_id}/stream")
async def stream(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    q: asyncio.Queue = JOBS[job_id]["queue"]

    async def gen():
        while True:
            msg = await q.get()
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("event") == "done":
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)


if __name__ == "__main__":
    main()
