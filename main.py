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


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env(v: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return os.environ.get(name, "")
    return _ENV_VAR_RE.sub(repl, v)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        v = _expand_env(v)
        # empty values from unresolved ${VAR} are skipped so a real env
        # var from the platform wins instead of being clobbered
        if v == "":
            continue
        os.environ.setdefault(k, v)


load_dotenv(ROOT / ".env")


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _strip_slash(s: str) -> str:
    return s.rstrip("/")


def _load_config_optional() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}


CFG = _load_config_optional()
_comfy_env = os.environ.get("COMFYUI_PATH")
COMFY = Path(_comfy_env or CFG.get("comfyui_path", ""))
if not _comfy_env and not CFG.get("comfyui_path"):
    raise RuntimeError("Set COMFYUI_PATH in .env (or comfyui_path in config.yaml)")
MODELS_DIR = COMFY / "models"
NODES_DIR = COMFY / "custom_nodes"
MODEL_EXTS = tuple(CFG.get("model_extensions", [
    ".safetensors", ".gguf", ".pt", ".bin", ".ckpt", ".pth",
]))
MODEL_NODE_MAP: dict[str, tuple[int, str]] = {
    k: (v[0], v[1]) for k, v in CFG.get("model_node_map", {}).items()
}
MODEL_FOLDERS: list[str] = CFG.get("model_folders", [])
NODE_PACKAGES: dict[str, str] = CFG.get("node_packages", {})

S3_MODELS_BASE = _strip_slash(os.environ.get("S3_MODELS_BASE", ""))
S3_NODES_BASE = _strip_slash(os.environ.get("S3_NODES_BASE", ""))

# Custom S3-compatible endpoint (Cloudflare R2, MinIO, Wasabi, …).
# AWS CLI v2 honours AWS_ENDPOINT_URL_S3 natively, but we also inject
# --endpoint-url explicitly so older CLI versions and edge-cases work.
_S3_ENDPOINT = (
    os.environ.get("AWS_ENDPOINT_URL_S3")
    or os.environ.get("AWS_ENDPOINT_URL")
    or ""
).rstrip("/")


def _aws_s3(*args: str) -> list[str]:
    """Build an `aws s3 …` command, injecting --endpoint-url when configured."""
    cmd = ["aws", "s3"]
    if _S3_ENDPOINT:
        cmd += ["--endpoint-url", _S3_ENDPOINT]
    cmd += list(args)
    return cmd


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


# ---------- S3 index (auto-discovered) ----------

INDEX_PATH = ROOT / ".s3-index.json"
INDEX_TTL = float(os.environ.get("INDEX_TTL", str(24 * 3600)))
# Catch ANY *CLASS_MAPPINGS = { ... } literal — covers the common
# ComfyUI_essentials pattern where NODE_CLASS_MAPPINGS is empty and the
# actual keys live in IMAGE_CLASS_MAPPINGS, MASK_CLASS_MAPPINGS, etc.
_ANY_CM_BLOCK_RE = re.compile(
    r"\b\w*CLASS_MAPPINGS\b\s*(?::\s*[^=]+)?=\s*\{(.*?)\n\}",
    re.DOTALL,
)
_NCM_KEY_RE = re.compile(r"""["']([^"']+)["']\s*:""")
# from .module import ANYTHING_CLASS_MAPPINGS  (or NODE_CLASS_MAPPINGS, or *)
_NCM_REEXPORT_RE = re.compile(
    r"from\s+\.([\w.]+)\s+import\s+([^#\n]+)",
)
# from . import foo, bar, baz  — generic submodule import
_NCM_SUBMOD_RE = re.compile(
    r"from\s+\.\s+import\s+([\w,\s]+)",
)

S3_INDEX: dict[str, Any] = {
    "models": {"files": {}, "by_basename": {}},
    "nodes": {"classes": {}, "packages": []},
    "cm": {"classes": {}, "count": 0},
    "indexed_at": 0,
}

CM_REGISTRY_URL = os.environ.get(
    "CM_REGISTRY_URL",
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json",
)


async def fetch_cm_registry() -> dict[str, Any]:
    """Pull ComfyUI-Manager's class→repo map. Returns
    {class_name: {repo, pkg}}."""
    import urllib.request

    def _fetch() -> bytes:
        req = urllib.request.Request(
            CM_REGISTRY_URL,
            headers={"User-Agent": "comfyui-workflow-sync"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    try:
        raw_bytes = await asyncio.to_thread(_fetch)
        raw = json.loads(raw_bytes)
    except Exception:
        return {"classes": {}, "count": 0}

    COMFY_CORE_REPO = "https://github.com/comfyanonymous/ComfyUI"
    classes: dict[str, list[dict[str, str]]] = {}
    builtins: set[str] = set()
    for repo_url, payload in raw.items():
        if not isinstance(payload, list) or not payload:
            continue
        nodes = payload[0]
        if not isinstance(nodes, list):
            continue
        if repo_url == COMFY_CORE_REPO:
            for n in nodes:
                if isinstance(n, str):
                    builtins.add(n)
            continue
        pkg = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        for n in nodes:
            if not isinstance(n, str):
                continue
            classes.setdefault(n, []).append({"repo": repo_url, "pkg": pkg})
    return {
        "classes": classes,
        "builtins": sorted(builtins),
        "count": len(classes),
        "builtin_count": len(builtins),
    }


def _load_index_from_disk() -> None:
    global S3_INDEX
    if INDEX_PATH.exists():
        try:
            S3_INDEX = json.loads(INDEX_PATH.read_text())
        except Exception:
            pass


_load_index_from_disk()


def _save_index() -> None:
    try:
        INDEX_PATH.write_text(json.dumps(S3_INDEX))
    except OSError:
        pass


def _split_s3(uri: str) -> tuple[str, str]:
    no_scheme = uri.removeprefix("s3://")
    bucket, _, prefix = no_scheme.partition("/")
    return bucket, prefix


async def _aws_text(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def index_models() -> dict[str, Any]:
    if not S3_MODELS_BASE:
        return {"files": {}, "by_basename": {}}
    base = S3_MODELS_BASE.rstrip("/") + "/"
    _bucket, prefix = _split_s3(base)
    rc, out = await _aws_text(_aws_s3("ls", "--recursive", base))
    if rc != 0:
        return {"files": {}, "by_basename": {}}
    files: dict[str, dict[str, Any]] = {}
    by_basename: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        size = int(parts[2])
        key = parts[3]
        if not key.startswith(prefix):
            continue
        rel = key[len(prefix):]
        segments = rel.split("/")
        if any(s.startswith(".") or s.startswith("__") for s in segments):
            continue
        if not any(rel.lower().endswith(ext) for ext in MODEL_EXTS):
            continue
        folder = segments[0]
        files[rel] = {
            "folder": folder,
            "bytes": size,
            "s3_url": base + rel,
        }
        bn = segments[-1]
        by_basename.setdefault(bn, []).append(rel)
    return {"files": files, "by_basename": by_basename}


def _parse_ncm_keys(text: str) -> list[str]:
    """Pull keys from ANY *CLASS_MAPPINGS dict literal in the source."""
    out: list[str] = []
    for m in _ANY_CM_BLOCK_RE.finditer(text):
        out.extend(_NCM_KEY_RE.findall(m.group(1)))
    return out


async def _fetch_py(base: str, pkg: str, rel: str) -> str:
    """Fetch a .py file via aws s3 cp - to stdout. Empty on miss."""
    rc, text = await _aws_text(
        _aws_s3("cp", f"{base}/{pkg}/{rel}", "-")
    )
    return text if rc == 0 else ""


async def _fetch_pkg_class_names(pkg: str) -> list[str]:
    if not S3_NODES_BASE:
        return []
    base = S3_NODES_BASE.rstrip("/")
    seen_paths: set[str] = set()
    queue: list[str] = ["__init__.py", "nodes.py"]
    collected: list[str] = []
    depth = 0
    while queue and depth < 6:
        depth += 1
        next_queue: list[str] = []
        # batch-fetch this layer in parallel
        texts = await asyncio.gather(*(
            _fetch_py(base, pkg, p) for p in queue if p not in seen_paths
        ))
        paths_this_round = [p for p in queue if p not in seen_paths]
        for path, text in zip(paths_this_round, texts):
            seen_paths.add(path)
            if not text:
                continue
            keys = _parse_ncm_keys(text)
            if keys:
                collected.extend(keys)
                continue  # inline mapping found here — don't chase reexports
            # follow imports of anything that looks like class-mappings.
            # cover all three patterns:
            #   from .X import NODE_CLASS_MAPPINGS
            #   from .X import IMAGE_CLASS_MAPPINGS, MASK_CLASS_MAPPINGS, ...
            #   from .X import *
            for mod, imports in _NCM_REEXPORT_RE.findall(text):
                names = imports
                if "CLASS_MAPPINGS" not in names and "*" not in names:
                    continue
                p = mod.replace(".", "/") + ".py"
                if p not in seen_paths:
                    next_queue.append(p)
                p2 = mod.replace(".", "/") + "/__init__.py"
                if p2 not in seen_paths:
                    next_queue.append(p2)
            # generic `from . import a, b, c` — try each submodule
            for group in _NCM_SUBMOD_RE.findall(text):
                for name in (n.strip() for n in group.split(",")):
                    if not name:
                        continue
                    for cand in (f"{name}.py", f"{name}/__init__.py"):
                        if cand not in seen_paths:
                            next_queue.append(cand)
        if collected:
            break  # got something from this package, stop crawling
        queue = next_queue
    # dedup
    return list(dict.fromkeys(collected))


async def index_nodes() -> dict[str, Any]:
    if not S3_NODES_BASE:
        return {"classes": {}, "packages": []}
    base = S3_NODES_BASE.rstrip("/") + "/"
    rc, out = await _aws_text(_aws_s3("ls", base))
    if rc != 0:
        return {"classes": {}, "packages": []}
    packages: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("PRE "):
            continue
        pkg = line[4:].rstrip("/")
        if pkg.startswith(".") or pkg.startswith("__"):
            continue
        packages.append(pkg)

    sem = asyncio.Semaphore(8)
    classes: dict[str, str] = {}
    failed: list[str] = []

    async def go(pkg: str):
        async with sem:
            names = await _fetch_pkg_class_names(pkg)
            if not names:
                failed.append(pkg)
                return
            for n in names:
                classes.setdefault(n, pkg)

    await asyncio.gather(*(go(p) for p in packages))
    return {
        "classes": classes,
        "packages": sorted(packages),
        "unparsed_packages": sorted(failed),
    }


_REINDEX_LOCK = asyncio.Lock()


async def rebuild_index() -> dict[str, Any]:
    """Coalesces concurrent callers — only one rebuild runs at a time."""
    global S3_INDEX
    async with _REINDEX_LOCK:
        # second waiter may find a fresh index already
        if (time.time() - S3_INDEX.get("indexed_at", 0)) < 5:
            return S3_INDEX
        models, nodes, cm = await asyncio.gather(
            index_models(), index_nodes(), fetch_cm_registry()
        )
        S3_INDEX = {
            "models": models,
            "nodes": nodes,
            "cm": cm,
            "indexed_at": time.time(),
        }
        _save_index()
    return S3_INDEX


async def ensure_index_fresh() -> None:
    age = time.time() - S3_INDEX.get("indexed_at", 0)
    if age > INDEX_TTL or not S3_INDEX.get("models", {}).get("files"):
        await rebuild_index()


# ComfyUI loader class-name substrings → the model folder(s) they load from.
# First match wins, so more specific needles come before broader ones
# (CLIPVision before CLIP, UpscaleModel before Upscale). Folder values are
# matched leniently against the actual S3 folders (see _folder_matches), so
# naming variance like vae/vae_approx or text_encoders/clip is tolerated.
# This replaces the old config.yaml `model_node_map` — the workflow already
# carries the node type, so the right folder is inferable without config.
_NODE_FOLDER_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("CLIPVision", ("clip_vision",)),
    ("StyleModel", ("style_models",)),
    ("ControlNet", ("controlnet",)),
    ("Hypernetwork", ("hypernetworks",)),
    ("GLIGEN", ("gligen",)),
    ("UpscaleModel", ("upscale_models",)),
    ("Upscale", ("upscale_models",)),
    ("DualCLIP", ("text_encoders", "clip")),
    ("TripleCLIP", ("text_encoders", "clip")),
    ("QuadrupleCLIP", ("text_encoders", "clip")),
    ("TextEncoder", ("text_encoders", "clip")),
    ("CLIP", ("text_encoders", "clip")),
    ("VAE", ("vae",)),
    ("Lora", ("loras",)),
    ("LoRA", ("loras",)),
    ("UNET", ("diffusion_models", "unet")),
    ("Unet", ("diffusion_models", "unet")),
    ("Diffusion", ("diffusion_models", "unet")),
    ("Checkpoint", ("checkpoints",)),
    ("Ckpt", ("checkpoints",)),
]

# API-format input keys → folder hints (the dict key names the slot, e.g.
# {"vae_name": "..."} on a node whose class_type we may not recognise).
_INPUT_FOLDER_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("clip_vision", ("clip_vision",)),
    ("style_model", ("style_models",)),
    ("control_net", ("controlnet",)),
    ("controlnet", ("controlnet",)),
    ("hypernetwork", ("hypernetworks",)),
    ("gligen", ("gligen",)),
    ("upscale_model", ("upscale_models",)),
    ("vae_name", ("vae",)),
    ("lora_name", ("loras",)),
    ("clip_name", ("text_encoders", "clip")),
    ("unet_name", ("diffusion_models", "unet")),
    ("ckpt_name", ("checkpoints",)),
]


def _infer_folder_hints(node_type: str | None, input_key: str | None = None) -> tuple[str, ...]:
    """Best-effort: which model folder(s) does this loader pull from?
    Prefers an explicit config map, then the node class name, then the
    API input-slot key. Returns folder keywords (empty = no idea)."""
    if node_type and node_type in MODEL_NODE_MAP:
        return (MODEL_NODE_MAP[node_type][1],)
    if node_type:
        nt = node_type.lower()
        for needle, folders in _NODE_FOLDER_HINTS:
            if needle.lower() in nt:
                return folders
    if input_key:
        ik = input_key.lower()
        for needle, folders in _INPUT_FOLDER_HINTS:
            if needle in ik:
                return folders
    return ()


def _folder_matches(folder: str, hints: tuple[str, ...]) -> bool:
    f = folder.lower()
    return any(f == h or f.startswith(h) for h in hints)


def _lookup_model(filename: str, hints: tuple[str, ...] = ()) -> dict | None:
    files = S3_INDEX.get("models", {}).get("files", {})
    by_bn = S3_INDEX.get("models", {}).get("by_basename", {})
    fn = filename.replace("\\", "/")
    if fn in files:
        return {"rel": fn, **files[fn]}
    bn = fn.split("/")[-1]
    matches = by_bn.get(bn, [])
    if not matches:
        return None
    if len(matches) == 1:
        return {"rel": matches[0], **files[matches[0]]}
    # ambiguous basename (same file lives in several folders, e.g. a VAE
    # mirrored under both checkpoints/ and vae/). Disambiguate by the folder
    # the loader node implies.
    if hints:
        pref = [m for m in matches if _folder_matches(files[m]["folder"], hints)]
        if pref:
            # deterministic when >1 still match: shortest folder = most specific
            best = min(pref, key=lambda m: (len(files[m]["folder"]), files[m]["folder"]))
            return {"rel": best, **files[best]}
    return None


def _classify_node(class_name: str) -> dict | None:
    """Returns one of:
      {"source":"s3",     "pkg": ...}                — in our S3
      {"source":"override","pkg": ...}               — user's node_packages
      {"source":"cm",     "pkg": ..., "repo": ...}   — in CM registry
      None                                            — stock ComfyUI / unknown
    """
    pkg = S3_INDEX.get("nodes", {}).get("classes", {}).get(class_name)
    if pkg:
        return {"source": "s3", "pkg": pkg}
    pkg = match_package(class_name)
    if pkg:
        return {"source": "override", "pkg": pkg}
    cm_obj = S3_INDEX.get("cm", {})
    # explicit ComfyUI-core node → builtin
    if class_name in cm_obj.get("builtins", []):
        return None
    cm_entries = cm_obj.get("classes", {}).get(class_name)
    if not cm_entries:
        return None
    # CM may catalog the same class under several repos (forks, copies).
    # Prefer an entry whose pkg name matches a package already in user's S3.
    # Normalise both sides (lowercase, alnum-only) so "ComfyUI_LayerStyle"
    # and "comfyui-layer-style" match.
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    s3_pkgs_norm = {
        _norm(p): p for p in S3_INDEX.get("nodes", {}).get("packages", [])
    }
    preferred = None
    for e in cm_entries:
        if _norm(e["pkg"]) in s3_pkgs_norm:
            # Override CM pkg with the actual S3 directory name so syncs
            # target the right key.
            actual_pkg = s3_pkgs_norm[_norm(e["pkg"])]
            return {
                "source": "s3",  # promote: it IS in S3, just had naming variance
                "pkg": actual_pkg,
            }
    chosen = preferred or cm_entries[0]
    return {"source": "cm", "pkg": chosen["pkg"], "repo": chosen["repo"]}


# ---------- workflow extractors (now index-driven) ----------


def _build_model_entry(value: str, hints: tuple[str, ...]) -> dict | None:
    """Resolve a workflow model reference via S3 index; return row dict or None."""
    if not isinstance(value, str) or not value.lower().endswith(MODEL_EXTS):
        return None
    info = _lookup_model(value, hints)
    if info:
        folder = info["folder"]
        rel = info["rel"]  # includes folder as first segment
        local_path = MODELS_DIR / rel
        # filename shown in UI: path within the folder, without folder prefix
        rel_within = rel[len(folder) + 1:] if rel.startswith(folder + "/") else rel
        return {
            "file": rel_within,
            "folder": folder,
            "local_path": str(local_path),
            "exists_locally": local_path.exists(),
            "s3_source": info["s3_url"],
            "s3_bytes": info["bytes"],
            "s3_exists": True,
        }
    # not in index — best-effort placeholder. Use the first hinted folder so
    # the row at least lands in a sensible place if the user picks manually.
    bn = value.replace("\\", "/").split("/")[-1]
    hint_folder = hints[0] if hints else None
    folder = hint_folder or "?"
    local_path = MODELS_DIR / folder / bn if hint_folder else None
    return {
        "file": value,
        "folder": folder,
        "local_path": str(local_path) if local_path else None,
        "exists_locally": local_path.exists() if local_path else False,
        "s3_source": None,
        "s3_bytes": None,
        "s3_exists": False,
    }


def _iter_model_strings(value: Any):
    """Yield candidate filename strings from a widget/input value. Handles
    plain strings, rgthree-style dicts ({"on":.., "lora":"...", ..}), and any
    nested list/dict. Non-model strings get filtered downstream by extension.
    Without this, loaders that store models in structured widgets — notably
    the rgthree Power Lora Loader — contribute nothing to the analysis."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_model_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_model_strings(v)


def extract_models(workflow: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in workflow.get("nodes", []):
        nt = node.get("type")
        hints = _infer_folder_hints(nt)
        wv = node.get("widgets_values") or []
        for v in wv:
            for s in _iter_model_strings(v):
                row = _build_model_entry(s, hints)
                if not row:
                    continue
                key = (row["folder"], row["file"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
    return out


def _is_api_format(workflow: dict) -> bool:
    """API export: top-level dict keyed by node id strings, values with class_type."""
    if "nodes" in workflow and isinstance(workflow["nodes"], list):
        return False
    for v in workflow.values():
        if isinstance(v, dict) and "class_type" in v:
            return True
    return False


def _iter_api_nodes(workflow: dict):
    """Yield (node_id, class_type, inputs_dict) for each node in API format."""
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not ct:
            continue
        yield node_id, ct, node.get("inputs") or {}


def extract_models_api(workflow: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for _id, ct, inputs in _iter_api_nodes(workflow):
        for slot, v in inputs.items():
            # API format names the slot ("vae_name", "ckpt_name", …) — use it
            # as a second hint source after the node class_type.
            hints = _infer_folder_hints(ct, slot)
            for s in _iter_model_strings(v):
                row = _build_model_entry(s, hints)
                if not row:
                    continue
                key = (row["folder"], row["file"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
    return out


def match_package(node_type: str) -> str | None:
    for hint, pkg in NODE_PACKAGES.items():
        if hint in node_type:
            return pkg
    return None


def _build_node_row(class_name: str, seen_pkg: set, assumed_builtin: set) -> dict | None:
    info = _classify_node(class_name)
    if info is None:
        assumed_builtin.add(class_name)
        return None
    pkg = info["pkg"]
    if pkg in seen_pkg:
        return None
    seen_pkg.add(pkg)
    local_path = NODES_DIR / pkg
    source = info["source"]
    base = {
        "node_type": class_name,
        "package_hint": pkg,
        "local_path": str(local_path),
        "exists_locally": local_path.exists(),
        "source": source,
    }
    if source in ("s3", "override"):
        base["s3_source"] = node_s3_url(pkg)
        base["github_url"] = None
        base["status"] = "ok" if local_path.exists() else "missing"
    else:  # "cm" — known custom node but not in our S3 yet
        base["s3_source"] = None
        base["github_url"] = info["repo"]
        base["status"] = "github_only"
    return base


def extract_custom_nodes(workflow: dict) -> tuple[list[dict], list[str]]:
    out: list[dict] = []
    seen_pkg: set[str] = set()
    assumed_builtin: set[str] = set()
    for node in workflow.get("nodes", []):
        nt = node.get("type")
        if not nt:
            continue
        row = _build_node_row(nt, seen_pkg, assumed_builtin)
        if row:
            out.append(row)
    return out, sorted(assumed_builtin)


def extract_custom_nodes_api(workflow: dict) -> tuple[list[dict], list[str]]:
    out: list[dict] = []
    seen_pkg: set[str] = set()
    assumed_builtin: set[str] = set()
    for _id, ct, _inputs in _iter_api_nodes(workflow):
        row = _build_node_row(ct, seen_pkg, assumed_builtin)
        if row:
            out.append(row)
    return out, sorted(assumed_builtin)


class AnalyzeBody(BaseModel):
    workflow: dict


@app.post("/analyze")
async def analyze(body: AnalyzeBody):
    await ensure_index_fresh()
    if _is_api_format(body.workflow):
        nodes, assumed = extract_custom_nodes_api(body.workflow)
        return {
            "format": "api",
            "models": extract_models_api(body.workflow),
            "custom_nodes": nodes,
            "assumed_builtin": assumed,
        }
    nodes, assumed = extract_custom_nodes(body.workflow)
    return {
        "format": "workflow",
        "models": extract_models(body.workflow),
        "custom_nodes": nodes,
        "assumed_builtin": assumed,
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
    # For S3-compatible endpoints (R2, MinIO…) STS is not available.
    # Use `aws s3 ls` against the models bucket as the auth probe instead.
    if _S3_ENDPOINT and S3_MODELS_BASE:
        auth = await _probe(_aws_s3("ls", S3_MODELS_BASE + "/"))
    else:
        auth = await _probe(["aws", "sts", "get-caller-identity"])
    models_check = (
        await _probe(_aws_s3("ls", S3_MODELS_BASE + "/"))
        if S3_MODELS_BASE else {"ok": False, "error": "S3_MODELS_BASE not set"}
    )
    nodes_check = (
        await _probe(_aws_s3("ls", S3_NODES_BASE + "/"))
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
    idx = S3_INDEX
    return {
        "comfyui_path": str(COMFY),
        "models": models,
        "custom_nodes": nodes,
        "disk_free": free,
        "preflight": await preflight(),
        "parallel": PARALLEL_SEM_SIZE,
        "index": {
            "indexed_at": idx.get("indexed_at", 0),
            "model_files": len(idx.get("models", {}).get("files", {})),
            "node_packages": len(idx.get("nodes", {}).get("packages", [])),
            "node_classes": len(idx.get("nodes", {}).get("classes", {})),
            "cm_classes": idx.get("cm", {}).get("count", 0),
            "unparsed_packages": idx.get("nodes", {}).get("unparsed_packages", []),
        },
    }


def _resolve_browse_prefix(p: str | None) -> str:
    """If caller passed a bucket-relative path or empty, expand to a sensible
    default. Always returns an s3:// URI ending in /."""
    if not p:
        # default: parent of both bases so user can choose between trees
        base = S3_MODELS_BASE or S3_NODES_BASE or ""
        if not base:
            raise HTTPException(400, "no S3 base configured")
        parent = base.rstrip("/").rsplit("/", 1)[0]
        return parent + "/"
    if not p.startswith("s3://"):
        raise HTTPException(400, "prefix must start with s3://")
    return p if p.endswith("/") else p + "/"


@app.get("/browse")
async def browse(prefix: str | None = None):
    uri = _resolve_browse_prefix(prefix)
    rc, out = await _aws_ls(uri)
    if rc != 0:
        raise HTTPException(404, f"cannot list {uri}")
    folders: list[str] = []
    files: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.rstrip()
        if line.startswith("                           PRE "):
            folders.append(line.split("PRE ", 1)[1].rstrip("/"))
            continue
        parts = line.split(maxsplit=3)
        if len(parts) == 4 and parts[2].isdigit():
            files.append({"name": parts[3], "size": int(parts[2])})
    parent = uri.rstrip("/").rsplit("/", 1)[0] + "/" if uri.count("/") > 3 else None
    return {
        "prefix": uri,
        "parent": parent,
        "folders": sorted(folders),
        "files": sorted(files, key=lambda f: f["name"]),
    }


@app.post("/reindex")
async def reindex():
    t0 = time.monotonic()
    await rebuild_index()
    return {
        "took": round(time.monotonic() - t0, 2),
        "model_files": len(S3_INDEX["models"]["files"]),
        "node_packages": len(S3_INDEX["nodes"]["packages"]),
        "node_classes": len(S3_INDEX["nodes"]["classes"]),
        "unparsed_packages": S3_INDEX["nodes"].get("unparsed_packages", []),
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
    args = _aws_s3("ls", uri)
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


SIZE_PROBE_SEM = asyncio.Semaphore(4)


def _size_from_index(src: str) -> dict[str, Any] | None:
    """If the URL maps onto something in our S3 index, answer from cache."""
    # Models: index by full s3_url
    for rec in S3_INDEX.get("models", {}).get("files", {}).values():
        if rec.get("s3_url") == src:
            return {"bytes": rec["bytes"], "files": 1, "candidates": []}
    # Custom-node packages: prefix match against node_s3_url(pkg)
    nodes_idx = S3_INDEX.get("nodes", {})
    for pkg in nodes_idx.get("packages", []):
        if node_s3_url(pkg) == src:
            # We don't precompute total bytes per package; need a probe.
            return None
    return None


async def _bounded_size(src: str) -> dict[str, Any]:
    cached = _size_from_index(src)
    if cached is not None:
        return cached
    async with SIZE_PROBE_SEM:
        return await s3_size(src)


@app.post("/size")
async def size(body: SizeBody):
    results = await asyncio.gather(*(_bounded_size(s) for s in body.sources))
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
        return _aws_s3(
            "sync", parent, local_dir,
            "--exact-timestamps",
            "--exclude", "*",
            "--include", filename,
        )
    src = item.s3_source if item.s3_source.endswith("/") else item.s3_source + "/"
    dst = item.local_dest if item.local_dest.endswith("/") else item.local_dest + "/"
    return _aws_s3("sync", src, dst, "--exact-timestamps")


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
