"""
api.py — HTTP entry point for dra_harness.

Three endpoints:

    POST /run           submit a prompt or a CSV path  → {"run_id": ...}
    GET  /run/{id}      status, cost, error
    GET  /run/{id}/result   report text, citations, output file paths

Plus GET / — a dashboard page that polls those three for you.

Submission does not wait. A run takes minutes to hours, far longer than an HTTP
request can stay open, so POST /run queues the work and returns a run_id
immediately. Each job runs on its own worker thread with its own event loop,
because the pipeline calls subprocess.run() synchronously inside async code —
running it on the server's loop would freeze every other request.

No authentication is needed from the machine running it. Requests arriving
from any other address must send X-API-Key matching DRA_API_KEY, and are
refused if that variable is unset — so exposing this beyond localhost is a
deliberate act, not an accident.

Start it:
    uvicorn dra_harness.api:app --port 8000 --workers 1
"""

from __future__ import annotations

import os
import json
import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

try:
    from .config import PipelineConfig, GenParams, load_env, DEFAULT_PROVIDERS
    from .csv_loader import row_to_package
    from .file_resolver import FileResolver
    from .pipeline import run_batch, run_packages, prepare_run_dirs, save_results
    from .provider import MODEL_REGISTRY
except ImportError:
    from config import PipelineConfig, GenParams, load_env, DEFAULT_PROVIDERS
    from csv_loader import row_to_package
    from file_resolver import FileResolver
    from pipeline import run_batch, run_packages, prepare_run_dirs, save_results
    from provider import MODEL_REGISTRY

logger = logging.getLogger("dra.api")
load_env()

API_KEY = os.environ.get("DRA_API_KEY", "")
RUN_ROOT = os.path.abspath(os.environ.get("DRA_RUN_ROOT", "./runs_dir"))
MAX_JOBS = int(os.environ.get("DRA_MAX_PARALLEL_JOBS", "2"))

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=MAX_JOBS, thread_name_prefix="dra-job")

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class RunRequest(BaseModel):
    # exactly one of these two
    prompt: Optional[str] = Field(None, description="A single task, inline")
    csv_path: Optional[str] = Field(None, description="A CSV of tasks, by path")

    # single-task extras
    task_id: Optional[str] = None
    drive_url: str = ""
    output_format: str = Field("", description="xlsx|docx|pptx|pdf — omit to detect from prompt")

    # batch extras
    max_rows: Optional[int] = Field(None, ge=1)
    task_ids: Optional[list[str]] = None

    # shared
    providers: Optional[list[str]] = None
    passes: int = Field(1, ge=1, le=10)
    concurrency: int = Field(4, ge=1, le=32)
    live: bool = False                 # default costs nothing
    resolve_files: bool = True         # Drive inputs are the norm
    web_search: Optional[bool] = None
    max_turns: Optional[int] = Field(None, ge=1, le=500)
    max_cost_usd: Optional[float] = Field(None, gt=0)
    model_overrides: Optional[dict[str, str]] = None

    @model_validator(mode="after")
    def _one_of(self):
        if bool(self.prompt) == bool(self.csv_path):
            raise ValueError("Send exactly one of 'prompt' or 'csv_path'.")
        return self


def _config(req: RunRequest, run_id: str) -> PipelineConfig:
    """Fresh config per job — run_packages() mutates it."""
    providers = req.providers or list(DEFAULT_PROVIDERS)
    unknown = [p for p in providers if p not in MODEL_REGISTRY]
    if unknown:
        raise HTTPException(400, f"Unknown provider(s) {unknown}. Known: {sorted(MODEL_REGISTRY)}")

    params = GenParams()
    for name, value in (("web_search", req.web_search), ("max_turns", req.max_turns),
                        ("max_cost_usd", req.max_cost_usd)):
        if value is not None:
            params = replace(params, **{name: value})

    return PipelineConfig(
        providers=providers, passes_per_provider=req.passes,
        max_concurrent=req.concurrency, dry_run=not req.live,
        resolve_files=req.resolve_files, run_root=RUN_ROOT, run_id=run_id,
        defaults=params, model_overrides=req.model_overrides or {},
    )


def _set(run_id: str, **fields) -> None:
    with _LOCK:
        _JOBS[run_id].update(fields)


def _work(run_id: str, req: RunRequest) -> None:
    """One job, on a worker thread with its own event loop."""
    _set(run_id, status="running", started_at=datetime.now(timezone.utc).isoformat())
    try:
        cfg = _config(req, run_id)
        if req.csv_path:
            output = asyncio.run(run_batch(req.csv_path, cfg,
                                           max_rows=req.max_rows, task_ids=req.task_ids))
        else:
            prepare_run_dirs(cfg)   # staging must exist before Drive resolution
            files = []
            if req.resolve_files and req.drive_url:
                files = FileResolver(staging_dir=cfg.staging_dir).resolve([req.drive_url])
                logger.info("[%s] resolved %d file(s) from Drive", run_id, len(files))
            pkg = row_to_package({"task_id": req.task_id or run_id, "prompt": req.prompt,
                                  "drive_url": req.drive_url,
                                  "output_format": req.output_format},
                                 resolved_files=files or None)
            output = asyncio.run(run_packages([pkg], cfg, source=f"inline:{pkg.task_id}"))

        _set(run_id, status="succeeded", summary=output["summary"],
             results_path=save_results(output),
             finished_at=datetime.now(timezone.utc).isoformat())
        logger.info("[%s] done", run_id)
    except Exception as e:  # noqa: BLE001 — a failed job must not kill the worker
        logger.error("[%s] failed: %s", run_id, e, exc_info=True)
        _set(run_id, status="failed", error=f"{type(e).__name__}: {e}",
             finished_at=datetime.now(timezone.utc).isoformat())


def _parent_writable() -> bool:
    """RUN_ROOT may not exist yet; a job creates it. What matters is whether
    the nearest existing ancestor allows that."""
    p = RUN_ROOT
    while not os.path.isdir(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return os.access(p, os.W_OK)


app = FastAPI(title="DRA Harness", version="3.2.0")


@app.on_event("startup")
def _startup() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")
    logger.info("Run root: %s (writable: %s) | parallel jobs: %d",
                RUN_ROOT, _parent_writable(), MAX_JOBS)
    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.warning("OPENROUTER_API_KEY unset — only dry runs will be accepted.")


@app.middleware("http")
async def gate(request: Request, call_next):
    """
    No key needed from your own machine. Requests from anywhere else must
    present X-API-Key, and are refused outright if no key is configured.

    This is the whole auth story. It is safe because uvicorn bound to
    127.0.0.1 accepts nothing but local connections in the first place — the
    check is a backstop for the day someone starts it with --host 0.0.0.0 or
    puts a tunnel in front of it. In that case set DRA_API_KEY and the old
    header behaviour returns automatically.
    """
    if (request.client.host if request.client else None) not in LOOPBACK:
        if not API_KEY:
            return JSONResponse(
                {"detail": "Remote access requires DRA_API_KEY to be set on the server."},
                status_code=403)
        if request.headers.get("X-API-Key") != API_KEY:
            return JSONResponse({"detail": "Bad or missing X-API-Key header."},
                                status_code=401)
    return await call_next(request)


@app.post("/run", status_code=202)
def submit(req: RunRequest) -> dict:
    """Queue a run. Returns immediately — poll GET /run/{run_id}."""
    if req.live and not os.environ.get("OPENROUTER_API_KEY"):
        raise HTTPException(400, "live=true but OPENROUTER_API_KEY is not set.")
    if req.csv_path and not os.path.isfile(req.csv_path):
        raise HTTPException(404, f"No such CSV: {os.path.abspath(req.csv_path)}")

    _config(req, "validate")            # bad providers → 400 before anything is queued

    run_id = f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    with _LOCK:
        while run_id in _JOBS:          # same-second collision
            run_id += "x"
        _JOBS[run_id] = {"run_id": run_id, "status": "queued", "error": None,
                         "summary": None, "results_path": None,
                         "started_at": None, "finished_at": None,
                         "created_at": datetime.now(timezone.utc).isoformat(),
                         "request": req.model_dump(exclude_none=True)}
    _POOL.submit(_work, run_id, req)
    logger.info("[%s] queued (live=%s)", run_id, req.live)
    return {"run_id": run_id, "status": "queued"}


@app.get("/run/{run_id}")
def status(run_id: str) -> dict:
    with _LOCK:
        job = _JOBS.get(run_id)
    if not job:
        raise HTTPException(404, f"No such run: {run_id}")
    return job


@app.get("/run/{run_id}/result")
def result(run_id: str) -> dict:
    """Report text, citations, cost and output file paths, per model."""
    job = status(run_id)
    if job["status"] != "succeeded":
        raise HTTPException(409, f"Run is '{job['status']}'. No results yet.")
    with open(job["results_path"], encoding="utf-8") as f:
        full = json.load(f)
    return {
        "run_id": run_id,
        "summary": full["summary"],
        "results_path": job["results_path"],
        "responses": [{k: r.get(k) for k in
                       ("task_id", "provider", "model", "pass_index", "completed",
                        "error", "turns", "total_cost_usd", "response_text",
                        "citations", "output_files")}
                      for r in full["results"]],
    }


@app.get("/runs")
def recent() -> list[dict]:
    with _LOCK:
        return sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)[:50]


@app.get("/health")
def health() -> dict:
    with _LOCK:
        active = sum(1 for j in _JOBS.values() if j["status"] in ("queued", "running"))
    return {"status": "ok", "remote_key_set": bool(API_KEY),
            "live_ready": bool(os.environ.get("OPENROUTER_API_KEY")),
            "run_root": RUN_ROOT, "run_root_writable": os.access(RUN_ROOT, os.W_OK)
            if os.path.isdir(RUN_ROOT) else _parent_writable(),
            "active": active, "providers": sorted(MODEL_REGISTRY)}


_PAGE = """<!doctype html><meta charset=utf-8><title>DRA runs</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#272b35;--fg:#e6e8ec;--dim:#8b93a3;--a:#5b9dff}
@media(prefers-color-scheme:light){:root{--bg:#f7f8fa;--card:#fff;--line:#e3e6ec;--fg:#14171f;--dim:#697186;--a:#1f6feb}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,-apple-system,'Segoe UI',sans-serif;padding:24px}
h1{font-size:15px;font-weight:600;margin:0 0 14px}
input{background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:7px 10px;font:inherit;min-width:300px}
button{background:var(--a);color:#fff;border:0;border-radius:6px;padding:7px 14px;font:inherit;cursor:pointer}
table{width:100%;border-collapse:collapse;margin-top:16px;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
td,th{padding:9px 14px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px;font-weight:500}
tbody tr{cursor:pointer}tbody tr:hover{background:rgba(127,127,127,.08)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px}
.succeeded .dot{background:#4ade80}.running .dot{background:#fbbf24}
.failed .dot{background:#f87171}.queued .dot{background:#8b93a3}
pre{white-space:pre-wrap;word-break:break-word;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px;max-height:400px;overflow:auto;font-size:12px;margin-top:16px}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.dim{color:var(--dim)}.err{color:#f87171}
</style>
<h1>DRA runs <span id=h class=dim></span></h1>
<table><thead><tr><th>Status</th><th>Run</th><th>Cost</th><th>Detail</th></tr></thead>
<tbody id=t><tr><td colspan=4 class=dim>Loading…</td></tr></tbody></table>
<pre id=d class=dim>Select a run.</pre>
<script>
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function api(p){const r=await fetch(p);
if(!r.ok)throw new Error(r.status+' '+(await r.text()).slice(0,120));return r.json()}
async function go(){
 try{const x=await(await fetch('/health')).json();
  h.textContent='· '+x.active+' active · '+(x.live_ready?'live ready':'dry run only')}catch{}
 try{const js=await api('/runs');
  t.innerHTML=js.length?'':'<tr><td colspan=4 class=dim>No runs yet.</td></tr>';
  for(const j of js){const c=(j.summary?.total_cost_usd??0);
   const tr=document.createElement('tr');tr.className=j.status;
   tr.innerHTML='<td><span class=dot></span>'+j.status+'</td><td><code>'+esc(j.run_id)+
    '</code></td><td>$'+c+'</td><td class=dim>'+(j.error?'<span class=err>'+esc(j.error.slice(0,60))+'</span>':(j.summary?j.summary.succeeded+'/'+j.summary.total_runs+' ok':'—'))+'</td>';
   tr.onclick=()=>show(j);t.appendChild(tr)}
 }catch(e){t.innerHTML='<tr><td colspan=4 class=err>'+esc(e.message)+'</td></tr>'}}
async function show(j){
 if(j.status!=='succeeded'){d.textContent=JSON.stringify(j,null,2);return}
 try{const r=await api('/run/'+j.run_id+'/result');
  d.textContent=r.responses.map(x=>'── '+x.provider+' · '+(x.turns??0)+' turns · $'+
   (x.total_cost_usd??0)+' · '+(x.citations||[]).length+' citations\\n'+
   (x.output_files||[]).join('\\n')+'\\n\\n'+(x.response_text||x.error||'')).join('\\n\\n')
 }catch(e){d.textContent=e.message}}
go();setInterval(go,10000);
</script>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return _PAGE


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
