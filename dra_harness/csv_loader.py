"""
csv_loader.py — Standalone SME prompt loader (dra_harness).

A self-contained version of the CSV loader. It has NO dependency on any
module in the project root — everything it needs is either the Python
standard library or a sibling module inside this package
(`file_resolver.py`).

It loads three columns from the SME CSV:

    1. id          → task_id        (the unique task identifier)
    2. prompt      → prompt         (the research question text)
    3. file link   → file_paths     (a Google Drive folder/file URL,
                                      resolved to local files on demand)

It also derives one extra field automatically from the prompt text:

    output_formats → ["xlsx"] / ["docx"] / ["pptx"] / []  (file deliverable)

Column matching is alias-based and case-insensitive, so headers like
"task_id", "Prompt", and "Drive Link" all resolve without configuration.

Usage:
    # Preview what would be loaded
    python csv_loader.py --csv prompt_data.csv --preview

    # Load and write the packages to a JSON file (no GDrive download)
    python csv_loader.py --csv prompt_data.csv --output packages.json

    # Load, download GDrive folders to a staging dir, then write JSON
    python csv_loader.py --csv prompt_data.csv \
        --resolve-files --staging-dir /tmp/eval_files \
        --output packages.json

    # Programmatic use
    from csv_loader import load_packages
    packages = load_packages("prompt_data.csv")
    for pkg in packages:
        print(pkg.task_id, pkg.prompt[:60], pkg.file_paths)
"""

from __future__ import annotations

import os
import re
import csv
import json
import uuid
import logging
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional

# Sibling module inside this package — NOT the project root one.
# Dual import so it works both as a package (dra_harness.csv_loader)
# and as a script run from inside this directory.
try:
    from .file_resolver import FileResolver, parse_gdrive_reference
except ImportError:
    from file_resolver import FileResolver, parse_gdrive_reference

logger = logging.getLogger("dra.csv_loader")


# ─── Minimal data model ───────────────────────────────────────────────────────

@dataclass
class PromptPackage:
    """
    A single unit of work loaded from one CSV row.

    Only the fields this standalone loader cares about are kept.
    """
    task_id: str
    prompt: str
    file_paths: list = field(default_factory=list)   # local paths or a GDrive URL
    output_formats: list = field(default_factory=list)  # e.g. ["xlsx"], detected from prompt
    drive_url: str = ""                              # the original Drive link (reference)
    sme_name: str = ""                               # optional, used for id fallback only

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Column alias map (only the 3 columns we need) ────────────────────────────
# logical field name → ordered list of accepted CSV header strings.
# Matching is case-insensitive after stripping whitespace; first match wins.

COLUMN_ALIASES: dict[str, list[str]] = {
    "task_id":  ["task_id", "task id", "taskid", "id", "task"],
    "prompt":   ["prompt", "prompts", "prompt_text", "prompt text",
                 "question", "research question"],
    "drive_url": ["drive_link", "drive link", "drive", "drive_url",
                  "drive url", "gdrive", "gdrive_url", "gdrive url",
                  "files", "attachments", "file link", "google drive"],
    # Optional — explicit deliverable format authored by the SME (APEX-style).
    # When present, this is AUTHORITATIVE and the prompt-regex is only a fallback.
    "output_format": ["output_format", "output format", "deliverable",
                      "deliverable_format", "deliverable format", "artifact",
                      "output_type", "output type", "expected_output"],
    # Optional — only used to make a nicer auto-generated id when task_id is blank.
    "sme_name": ["full_name", "full name", "name", "poc name", "poc_name",
                 "sme_name", "sme name", "sme", "author"],
}

# Fields that MUST resolve for the loader to proceed.
REQUIRED_FIELDS = {"task_id", "prompt"}


# ─── Output format detection (inlined, no external dependency) ────────────────
# Detects when a prompt explicitly asks for a generated file deliverable.
# Sentence-level + verb-gated to avoid matching input-file references.

_OUTPUT_VERBS = r'(?:present|output|create|produce|generate|deliver|build|save|write|export)'

_FORMAT_PATTERNS: dict[str, re.Pattern] = {
    "xlsx": re.compile(rf'(?i)\b{_OUTPUT_VERBS}\b[^.!?\n]{{0,120}}\b(?:excel|xlsx|spreadsheet|workbook)\b'),
    "docx": re.compile(r'(?i)\bword\s+doc(?:ument)?\b'),
    "pptx": re.compile(rf'(?i)\b{_OUTPUT_VERBS}\b[^.!?\n]{{0,120}}\b(?:powerpoint|pptx|slide\s?deck|presentation)\b'),
    "pdf":  re.compile(rf'(?i)\b{_OUTPUT_VERBS}\b[^.!?\n]{{0,120}}\b(?:pdf)\b'),
    "csv":  re.compile(rf'(?i)\b{_OUTPUT_VERBS}\b[^.!?\n]{{0,120}}\b(?:csv|comma[- ]separated)\b'),
    "txt":  re.compile(rf'(?i)\b{_OUTPUT_VERBS}\b[^.!?\n]{{0,120}}\b(?:text\s+file|\.txt|plain\s+text)\b'),
}


def detect_output_formats(prompt: str) -> list[str]:
    """Return a sorted list of file formats the prompt requires as output."""
    return sorted(
        fmt for fmt, pattern in _FORMAT_PATTERNS.items()
        if pattern.search(prompt or "")
    )


# ─── Column resolution ────────────────────────────────────────────────────────

def resolve_columns(actual_headers: list[str]) -> dict[str, str]:
    """
    Map each logical field name → actual CSV column name.

    Raises ValueError if a required field (task_id, prompt) cannot be found.
    """
    norm = {h.strip().lower(): h for h in actual_headers}

    resolved: dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias.lower() in norm:
                resolved[field_name] = norm[alias.lower()]
                break

    missing = REQUIRED_FIELDS - set(resolved)
    if missing:
        raise ValueError(
            f"Required column(s) not found: {missing}.\n"
            f"Available headers: {actual_headers}\n"
            f"Add an alias to COLUMN_ALIASES or rename the column."
        )
    return resolved


# ─── CSV parsing ──────────────────────────────────────────────────────────────

def load_csv(csv_path: str) -> list[dict]:
    """
    Read the CSV and return cleaned rows with keys: task_id, prompt,
    drive_url, sme_name.

    Handles BOM-encoded UTF-8 (Excel exports), blank rows, missing task_id
    (auto-generated), and Excel float-formatted ids ("28115.0B" → "28115B").
    """
    rows: list[dict] = []

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        col = resolve_columns(list(reader.fieldnames or []))
        logger.info("Column mapping: %s", col)

        for i, raw in enumerate(reader):
            # Skip fully empty rows
            if not any(v.strip() for v in raw.values() if isinstance(v, str)):
                continue

            prompt = (raw.get(col["prompt"], "") or "").strip()
            if not prompt:
                logger.warning("Row %d: empty prompt, skipping", i + 1)
                continue

            task_id = (raw.get(col.get("task_id", ""), "") or "").strip()
            # Normalize Excel float-formatted ids: "28115.0B" → "28115B"
            task_id = re.sub(r'^(\d+)\.0([A-Za-z]*)$', r'\1\2', task_id)
            sme_name = (raw.get(col.get("sme_name", ""), "") or "").strip()
            if not task_id:
                slug = sme_name.lower().replace(" ", "_")[:15] if sme_name else "anon"
                task_id = f"{slug}_{uuid.uuid4().hex[:6]}"

            rows.append({
                "task_id":   task_id,
                "prompt":    prompt,
                "drive_url": (raw.get(col.get("drive_url", ""), "") or "").strip(),
                "sme_name":  sme_name,
                "output_format": (raw.get(col.get("output_format", ""), "") or "").strip(),
            })

    logger.info("Loaded %d valid rows from %s", len(rows), csv_path)
    return rows


def row_to_package(
    row: dict,
    resolved_files: Optional[list[str]] = None,
) -> PromptPackage:
    """Convert a parsed CSV row into a PromptPackage."""
    file_paths = resolved_files or []
    if not file_paths and row.get("drive_url"):
        # Keep the raw URL as a placeholder when not resolving to local files.
        file_paths = [row["drive_url"]]

    # Deliverable format: explicit SME-authored column wins (APEX-style, robust);
    # fall back to prompt-regex only when the column is absent/blank.
    explicit = (row.get("output_format") or "").strip().lower()
    if explicit:
        # normalize common spellings/synonyms → canonical extension
        norm = {
            "excel": "xlsx", "xls": "xlsx", "xlsx": "xlsx", "spreadsheet": "xlsx",
            "workbook": "xlsx",
            "word": "docx", "docx": "docx", "doc": "docx",
            "powerpoint": "pptx", "pptx": "pptx", "slides": "pptx", "deck": "pptx",
            "pdf": "pdf",
            "csv": "csv",
            "txt": "txt", "text": "txt",
            "json": "json", "md": "md", "markdown": "md",
        }
        output_formats = []
        for tok in re.split(r"[,\s/|]+", explicit):
            fmt = norm.get(tok.strip())
            if fmt and fmt not in output_formats:
                output_formats.append(fmt)
        if not output_formats:  # column present but unrecognized → fall back
            output_formats = detect_output_formats(row["prompt"])
    else:
        output_formats = detect_output_formats(row["prompt"])
    if output_formats:
        logger.info("[%s] Output formats: %s%s", row["task_id"], output_formats,
                    " (explicit)" if explicit else " (detected)")

    return PromptPackage(
        task_id=row["task_id"],
        prompt=row["prompt"],
        file_paths=file_paths,
        output_formats=output_formats,
        drive_url=row.get("drive_url", ""),
        sme_name=row.get("sme_name", ""),
    )


# ─── Batch loading ────────────────────────────────────────────────────────────

def load_packages(
    csv_path: str,
    resolve_files: bool = False,
    staging_dir: Optional[str] = None,
    max_rows: Optional[int] = None,
    task_ids: Optional[list[str]] = None,
) -> list[PromptPackage]:
    """
    Load all prompt packages from a CSV file.

    Args:
        csv_path:      path to the prompt CSV
        resolve_files: if True, download GDrive files to the staging dir
        staging_dir:   local directory for downloaded files
        max_rows:      cap to the first N rows (applied BEFORE file resolution)
        task_ids:      if set, only load rows whose task_id is in this list
    """
    rows = load_csv(csv_path)

    if task_ids:
        wanted = set(task_ids)
        rows = [r for r in rows if r["task_id"] in wanted]
        logger.info("Filtered to %d rows by task_ids", len(rows))
    if max_rows:
        rows = rows[:max_rows]
        logger.info("Limited to %d rows", max_rows)

    resolver = None
    if resolve_files:
        staging = staging_dir or f"/tmp/dra_staging_{uuid.uuid4().hex[:8]}"
        resolver = FileResolver(staging_dir=staging)
        logger.info("File resolver staging: %s", staging)

    packages: list[PromptPackage] = []
    for row in rows:
        resolved_files = None
        if resolver and row.get("drive_url"):
            try:
                resolved_files = resolver.resolve([row["drive_url"]])
                logger.info("[%s] Resolved %d file(s) from GDrive",
                            row["task_id"], len(resolved_files))
            except Exception as e:
                logger.error("[%s] File resolution failed: %s", row["task_id"], e)
                resolved_files = []
        packages.append(row_to_package(row, resolved_files))

    logger.info("Created %d packages", len(packages))
    return packages


# ─── Preview & export ─────────────────────────────────────────────────────────

def preview_csv(csv_path: str) -> None:
    """Print a summary of what would be loaded from the CSV."""
    rows = load_csv(csv_path)

    print(f"\n{'═' * 70}")
    print(f"  CSV Preview: {csv_path}")
    print(f"  {len(rows)} prompt packages")
    print(f"{'═' * 70}\n")

    file_links = folder_links = file_output = 0
    for r in rows:
        ref = parse_gdrive_reference(r["drive_url"]) if r["drive_url"] else None
        if ref and ref.get("type") == "file":
            file_links += 1
        elif ref and ref.get("type") == "folder":
            folder_links += 1
        if detect_output_formats(r["prompt"]):
            file_output += 1

    print(f"  GDrive links:           {file_links} file(s), {folder_links} folder(s)")
    print(f"  File output required:   {file_output} prompt(s)")

    print(f"\n{'─' * 70}\n  Sample packages (first 3):\n{'─' * 70}")
    for row in rows[:3]:
        pkg = row_to_package(row)
        print(f"\n  [{pkg.task_id}]")
        print(f"    Prompt:      {pkg.prompt[:80]}...")
        print(f"    Drive:       {pkg.drive_url[:70]}")
        print(f"    Output fmts: {pkg.output_formats or 'none'}")
    print(f"\n{'═' * 70}\n")


def save_packages(packages: list[PromptPackage], output_path: str) -> None:
    """Write the loaded packages to a JSON file."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in packages], f, indent=2, ensure_ascii=False)
    logger.info("Wrote %d packages to %s", len(packages), output_path)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone SME prompt CSV loader (id, prompt, file link).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--csv", required=True, help="Path to the prompt CSV")
    parser.add_argument("--preview", action="store_true",
                        help="Print a summary; do not load/export")
    parser.add_argument("--resolve-files", action="store_true",
                        help="Download GDrive files to the staging dir")
    parser.add_argument("--staging-dir", default=None,
                        help="Directory for downloaded GDrive files")
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Only load the first N rows")
    parser.add_argument("--task-ids", default=None,
                        help="Comma-separated task ids to load (skips the rest)")
    parser.add_argument("--output", "-o", default=None,
                        help="Write loaded packages to this JSON file")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.preview:
        preview_csv(args.csv)
        return

    task_ids = None
    if args.task_ids:
        task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]

    packages = load_packages(
        args.csv,
        resolve_files=args.resolve_files,
        staging_dir=args.staging_dir,
        max_rows=args.max_rows,
        task_ids=task_ids,
    )

    if args.output:
        save_packages(packages, args.output)
        print(f"\n  Wrote {len(packages)} packages → {args.output}\n")
    else:
        print(f"\n  Loaded {len(packages)} packages "
              f"(use --output to save as JSON):\n")
        for p in packages[:5]:
            fmts = f"  [{','.join(p.output_formats)}]" if p.output_formats else ""
            print(f"    {p.task_id:20s}  {len(p.prompt):5d} chars  "
                  f"{len(p.file_paths)} file-ref{fmts}")
        if len(packages) > 5:
            print(f"    ... and {len(packages) - 5} more")
        print()


if __name__ == "__main__":
    main()