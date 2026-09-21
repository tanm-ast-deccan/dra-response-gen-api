"""
models.py — Lean, standalone data structures for the unified pipeline.

    Task        → one unit of work for ONE provider + ONE pass
    TurnRecord  → one turn in the agentic loop (full observability)
    ToolCall    → an observability record of a local tool invocation
    RunResult   → the normalized output every provider produces

No imports from the repo root; this is the standalone language of the
dra_harness package.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Task:
    """
    A single provider+pass unit of work, derived from a loaded PromptPackage.

    The same prompt fans out to N providers x K passes, each becoming one Task
    with a distinct run_id so results never collide.
    """
    task_id: str                 # original SME task id (shared across providers)
    prompt: str
    provider: str                # logical name: "claude" | "openai" | "gemini" | "qwen" | "hunyuan" | "doubao"
    model_slug: str              # resolved OpenRouter slug
    pass_index: int = 1
    file_paths: list = field(default_factory=list)
    output_formats: list = field(default_factory=list)
    output_dir: Optional[str] = None   # where generated files for this task go
    drive_url: str = ""
    sme_name: str = ""

    @property
    def run_id(self) -> str:
        """Unique id for this provider+pass run."""
        return f"{self.task_id}__{self.provider}__p{self.pass_index}"


@dataclass
class ToolCall:
    """One local tool invocation — observability for the agentic loop."""
    turn: int
    name: str
    arguments: dict
    result_preview: str = ""     # first ~500 chars of the tool result
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TurnRecord:
    """
    One complete turn in the agentic loop — full observability.

    Captures the model's reasoning, its tool calls, the tool results,
    and per-turn token/cost accounting. The trajectory (list of TurnRecords)
    is the primary artifact for debugging, scoring, and study analysis.
    """
    turn: int
    timestamp: str                                         # ISO 8601

    # What the model said/decided this turn
    assistant_text: str = ""                               # reasoning text (before/alongside tool calls)
    finish_reason: str = ""                                # "stop", "tool_calls", "length", etc.

    # Tool calls the model made
    tool_calls: list[dict] = field(default_factory=list)   # [{name, arguments, id}]

    # Tool results (matched 1:1 with tool_calls)
    tool_results: list[dict] = field(default_factory=list) # [{name, result, server, error}]

    # Per-turn token accounting
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunResult:
    """
    The common output format for every provider. Whatever the model, the
    result always looks like this.
    """
    task_id: str
    run_id: str
    provider: str
    model: str
    pass_index: int

    response_text: str = ""
    citations: list = field(default_factory=list)        # [{url, title, snippet}]
    output_files: list = field(default_factory=list)     # local paths
    output_file_errors: dict = field(default_factory=dict)

    # Legacy flat tool call log (kept for backward compatibility)
    tool_calls: list = field(default_factory=list)       # [ToolCall]

    # NEW: Full trajectory — the primary observability artifact
    trajectory: list = field(default_factory=list)       # [TurnRecord] (serialized as dicts)

    # Accounting
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0

    # Loop / status
    turns: int = 0
    completed: bool = False
    forced_stop: bool = False
    error: Optional[str] = None

    # Timing
    total_duration_sec: float = 0.0
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convert TurnRecord objects if they aren't already dicts
        if self.trajectory and hasattr(self.trajectory[0], 'to_dict'):
            d['trajectory'] = [t.to_dict() for t in self.trajectory]
        return d