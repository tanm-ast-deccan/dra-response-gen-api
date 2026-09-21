"""
dra_harness — standalone, modular DRA response-generation pipeline.

Self-contained: no dependency on any module in the project root. Every
provider (Claude, OpenAI, Gemini, Qwen) runs through ONE OpenRouter driver,
and every hyperparameter lives in a single config object.

Quick start:
    from dra_harness import PipelineConfig, run_batch, save_results
    import asyncio

    cfg = PipelineConfig(providers=["claude", "qwen"], dry_run=True)
    out = asyncio.run(run_batch("prompts.csv", cfg, max_rows=3))
    save_results(out)

Loading only:
    from dra_harness import load_packages
"""

# ── Loader (CSV → PromptPackage) ──────────────────────────────────────────────
from .csv_loader import (
    PromptPackage,
    load_packages,
    load_csv,
    row_to_package,
    detect_output_formats,
)

# ── Config + data models ──────────────────────────────────────────────────────
from .config import PipelineConfig, GenParams, load_env, DEFAULT_PROVIDERS
from .models import Task, RunResult, ToolCall

# ── Providers + tools ─────────────────────────────────────────────────────────
from .provider import OpenRouterDriver, MODEL_REGISTRY, resolve_slug, supports_tools
from .tools import TOOL_REGISTRY, register_tool

# ── Runner + orchestration ────────────────────────────────────────────────────
from .runner import run_task, build_messages
from .pipeline import run_batch, run_packages, build_tasks, save_results, prepare_run_dirs

__all__ = [
    # loader
    "PromptPackage", "load_packages", "load_csv", "row_to_package",
    "detect_output_formats",
    # config + models
    "PipelineConfig", "GenParams", "load_env", "DEFAULT_PROVIDERS",
    "Task", "RunResult", "ToolCall",
    # provider + tools
    "OpenRouterDriver", "MODEL_REGISTRY", "resolve_slug", "supports_tools",
    "TOOL_REGISTRY", "register_tool",
    # runner + pipeline
    "run_task", "build_messages", "run_batch", "run_packages", "build_tasks",
    "save_results", "prepare_run_dirs",
]
