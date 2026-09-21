"""
pipeline.py — Orchestration: packages → tasks → concurrent runs → results JSON.

    load_packages()  (csv_loader)        one PromptPackage per CSV row
        → fan out to providers x passes   one Task each
        → run_task() concurrently         one RunResult each
        → aggregate + save                results JSON

Two entry points:

    run_batch(csv_path, cfg)      load a CSV, then run it
    run_packages(packages, cfg)   run PromptPackages you built yourself —
                                  single-task API calls, notebooks, tests

run_batch is a thin wrapper over run_packages, so both paths produce identical
run layouts and results dicts.

Concurrency is bounded by PipelineConfig.max_concurrent. One failing run never
affects the others.
"""

from __future__ import annotations

import os
import json
import asyncio
import logging
from datetime import datetime, timezone

try:
    from .config import PipelineConfig, load_env
    from .csv_loader import load_packages
    from .provider import OpenRouterDriver, resolve_slug, driver_for
    from .runner import run_task
    from .models import Task
except ImportError:  # run as a script from inside the package dir
    from config import PipelineConfig, load_env
    from csv_loader import load_packages
    from provider import OpenRouterDriver, resolve_slug, driver_for
    from runner import run_task
    from models import Task

logger = logging.getLogger("dra.pipeline")


def build_tasks(packages, cfg: PipelineConfig) -> list[Task]:
    """Fan out each package to every provider and pass."""
    tasks: list[Task] = []
    for pkg in packages:
        for provider in cfg.providers:
            slug = resolve_slug(provider, cfg.model_for(provider))
            for p in range(1, cfg.passes_per_provider + 1):
                run_dir = os.path.abspath(os.path.join(
                    cfg.staging_dir, pkg.task_id, "runs", f"{provider}__p{p}"
                ))
                tasks.append(Task(
                    task_id=pkg.task_id,
                    prompt=pkg.prompt,
                    provider=provider,
                    model_slug=slug,
                    pass_index=p,
                    file_paths=list(pkg.file_paths),
                    output_formats=list(pkg.output_formats),
                    output_dir=run_dir,
                    drive_url=pkg.drive_url,
                    sme_name=pkg.sme_name,
                ))
    return tasks


def prepare_run_dirs(cfg: PipelineConfig) -> str:
    """
    Stamp cfg with a run_id and point staging/output at this run's folder.

    MUTATES cfg — give every concurrent run its own PipelineConfig instance.
    An already-set cfg.run_id is respected, so a caller (the HTTP API) can
    make its own job id name the folder. Returns the run directory.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    prov = "_".join(cfg.providers)
    if not cfg.run_id:
        cfg.run_id = f"run_{ts}_{prov}"
    run_dir = os.path.join(cfg.run_root, cfg.run_id)
    cfg.staging_dir = os.path.join(run_dir, "staging")
    cfg.output_dir = run_dir                       # trace.json lands here
    os.makedirs(cfg.staging_dir, exist_ok=True)
    return run_dir


async def run_packages(
    packages: list,
    cfg: PipelineConfig,
    source: str = "inline",
) -> dict:
    """
    Run an already-built list of PromptPackages and return a results dict.

    This is the core orchestrator. run_batch() calls it after loading a CSV;
    the HTTP API calls it directly for single-task runs.

    Args:
        packages: list of PromptPackage
        cfg:      a PipelineConfig instance owned by this run (it gets mutated)
        source:   label recorded in the results dict ("inline", a CSV path, ...)
    """
    load_env()
    if not packages:
        raise ValueError("run_packages called with no packages")
    prepare_run_dirs(cfg)

    tasks = build_tasks(packages, cfg)
    logger.info("Built %d run(s): %d package(s) x %d provider(s) x %d pass(es)",
                len(tasks), len(packages), len(cfg.providers), cfg.passes_per_provider)

    # ── Per-provider drivers (handles different base_urls/keys) ───────
    drivers: dict[str, OpenRouterDriver] = {}
    for provider in cfg.providers:
        drivers[provider] = driver_for(provider, dry_run=cfg.dry_run)

    # Per-provider params resolved once.
    params_by_provider = {p: cfg.params_for(p) for p in cfg.providers}

    sem = asyncio.Semaphore(cfg.max_concurrent)
    started = datetime.now(timezone.utc)

    async def _run(task: Task):
        async with sem:
            return await run_task(task, params_by_provider[task.provider],
                                  drivers[task.provider])

    results = await asyncio.gather(*[_run(t) for t in tasks], return_exceptions=True)

    # Normalize any unexpected exceptions into a record.
    run_dicts = []
    for task, res in zip(tasks, results):
        if isinstance(res, Exception):
            logger.error("[%s] unhandled: %s", task.run_id, res)
            run_dicts.append({
                "task_id": task.task_id, "run_id": task.run_id,
                "provider": task.provider, "model": task.model_slug,
                "pass_index": task.pass_index, "completed": False,
                "error": str(res),
            })
        else:
            run_dicts.append(res.to_dict())

    completed_at = datetime.now(timezone.utc)
    return {
        "source": source,
        "csv": source,                 # kept for backward compatibility
        "run_id": cfg.run_id,
        "started_at": started.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_sec": (completed_at - started).total_seconds(),
        "config": cfg.to_dict(),
        "summary": _summarize(run_dicts, cfg),
        "results": run_dicts,
    }


async def run_batch(
    csv_path: str,
    cfg: PipelineConfig,
    max_rows: int | None = None,
    task_ids: list[str] | None = None,
) -> dict:
    """Run the full pipeline for a CSV and return a results dict."""
    load_env()
    prepare_run_dirs(cfg)   # staging dir must exist before GDrive resolution

    packages = load_packages(
        csv_path,
        resolve_files=cfg.resolve_files,
        staging_dir=cfg.staging_dir,
        max_rows=max_rows,
        task_ids=task_ids,
    )
    logger.info("Loaded %d package(s)", len(packages))

    return await run_packages(packages, cfg, source=csv_path)


def _summarize(run_dicts: list[dict], cfg: PipelineConfig) -> dict:
    by_provider: dict = {}
    total_cost = 0.0
    succeeded = 0
    for r in run_dicts:
        prov = r.get("provider", "?")
        slot = by_provider.setdefault(prov, {"runs": 0, "succeeded": 0, "cost": 0.0})
        slot["runs"] += 1
        if r.get("completed"):
            slot["succeeded"] += 1
            succeeded += 1
        c = r.get("total_cost_usd", 0.0) or 0.0
        slot["cost"] = round(slot["cost"] + c, 6)
        total_cost += c
    return {
        "total_runs": len(run_dicts),
        "succeeded": succeeded,
        "failed": len(run_dicts) - succeeded,
        "total_cost_usd": round(total_cost, 6),
        "by_provider": by_provider,
    }


def save_results(output: dict, path: str | None = None) -> str:
    out_dir = output["config"].get("output_dir", "./results")
    if path is None:
        results = output.get("results", [])
        providers = sorted(set(r.get("provider", "") for r in results))
        prov_str = "_".join(providers) or "noprov"
        n = len(set(r.get("task_id", "") for r in results))
        # start-time from the run, not "now", so the file name matches the run
        ts = output.get("started_at", "").replace(":", "").replace("-", "")[:15] or \
             datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = os.path.join(out_dir, f"run_{ts}_{prov_str}_{n}tasks.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)
    logger.info("Saved results → %s", path)
    return path
