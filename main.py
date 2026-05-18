from __future__ import annotations

import asyncio
import json
import os
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


@app.get("/status")
def status():
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
    return {"comfyui_path": str(COMFY), "models": models, "custom_nodes": nodes}


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


def build_command(item: SyncItem) -> list[str]:
    is_file_src = item.is_file or any(
        item.s3_source.lower().endswith(ext) for ext in MODEL_EXTS
    )
    if is_file_src:
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


async def run_job(job_id: str, items: list[SyncItem]):
    job = JOBS[job_id]
    q: asyncio.Queue = job["queue"]
    overall_rc = 0
    try:
        for idx, item in enumerate(items):
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
            # Poll the directory where aws is writing — for single files that's
            # the parent (aws uses a tempfile next to the target then renames).
            poll_target = local_target.parent if is_file_dest else local_target
            start_bytes = dir_bytes(poll_target)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async def poll_progress():
                while True:
                    await asyncio.sleep(1.5)
                    cur = dir_bytes(poll_target)
                    await q.put({
                        "event": "progress",
                        "item_index": idx,
                        "bytes_downloaded": max(0, cur - start_bytes),
                        "bytes_local": cur,
                    })

            poller = asyncio.create_task(poll_progress())
            assert proc.stdout is not None
            try:
                async for line in proc.stdout:
                    await q.put({
                        "event": "log",
                        "item_index": idx,
                        "line": line.decode("utf-8", "replace").rstrip(),
                    })
                rc = await proc.wait()
            finally:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass

            final_cur = dir_bytes(poll_target)
            await q.put({
                "event": "progress",
                "item_index": idx,
                "bytes_downloaded": max(0, final_cur - start_bytes),
                "bytes_local": final_cur,
            })
            await q.put({"event": "exit", "item_index": idx, "exit_code": rc})
            if rc != 0:
                overall_rc = rc
                continue
            # post-sync hook: pip install -r requirements.txt for custom nodes
            if not (item.is_file or any(
                item.s3_source.lower().endswith(ext) for ext in MODEL_EXTS
            )):
                req = Path(item.local_dest) / "requirements.txt"
                if req.exists():
                    await q.put({"event": "log", "line": f"installing {req}"})
                    pip = await asyncio.create_subprocess_exec(
                        "pip", "install", "-r", str(req),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    assert pip.stdout is not None
                    async for line in pip.stdout:
                        await q.put({
                            "event": "log",
                            "line": line.decode("utf-8", "replace").rstrip(),
                        })
                    await pip.wait()
    except Exception as e:
        await q.put({"event": "error", "message": str(e)})
        overall_rc = 1
    finally:
        job["exit_code"] = overall_rc
        job["done"] = True
        await q.put({"event": "done", "exit_code": overall_rc})


@app.post("/sync")
async def sync(body: SyncBody):
    if not body.items:
        raise HTTPException(400, "no items")
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"queue": asyncio.Queue(), "done": False, "exit_code": None}
    asyncio.create_task(run_job(job_id, body.items))
    return {"job_id": job_id}


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
