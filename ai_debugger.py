"""
ai_debugger.py — Automated AI-powered repository debugger.

Recursively scans all .py files in the project, sends each to Google
Gemini for static analysis (async bugs, missing deps, Pydantic/SQLAlchemy
pitfalls, syntax issues), and compiles a markdown report with copy-paste
fixes.

Usage:
    export GEMINI_API_KEY="your-key-here"
    python ai_debugger.py
    python ai_debugger.py --root . --output debug_report.md --concurrency 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import google.generativeai as genai
except ImportError:
    print(
        "ERROR: google-generativeai is not installed.\n"
        "Run: pip install google-generativeai",
        file=sys.stderr,
    )
    sys.exit(1)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__",
    "node_modules", ".mypy_cache", ".pytest_cache",
    "build", "dist", ".idea", ".vscode",
}

MODEL_NAME = "gemini-1.5-pro"
MAX_FILE_CHARS = 60_000   # guard against huge files blowing the context window
DEFAULT_CONCURRENCY = 5   # simultaneous Gemini requests
API_RETRY_ATTEMPTS = 3
API_RETRY_BACKOFF = 4     # seconds, doubles each retry

SYSTEM_PROMPT = """You are a senior Python code auditor specializing in:
- FastAPI + Uvicorn deployment issues
- SQLAlchemy async ORM with asyncpg (Supabase/Postgres)
- python-telegram-bot v21.3 (async API, Application/ApplicationBuilder patterns)
- Pydantic v2 settings/secrets handling
- requirements.txt / dependency correctness

For the given file, check specifically for:
1. Database URLs that are NOT using the async driver
   (e.g. "postgresql://" or "psycopg2" instead of "postgresql+asyncpg://").
2. Sync-only libraries used in an async context (e.g. psycopg2 imported
   directly instead of asyncpg; requests instead of httpx in async code).
3. Pydantic SecretStr misuse — e.g. calling .get_secret_value() on a plain
   str/int, or forgetting to call it on an actual SecretStr, causing the
   raw secret object to leak into a connection string.
4. SQLAlchemy async session misuse (e.g. using Session instead of
   AsyncSession, missing 'await', missing 'async with', calling
   .query() instead of select() in async style).
5. python-telegram-bot v21.x specific bugs (e.g. blocking calls inside
   handlers, missing 'await', deprecated Updater-based patterns from v13).
6. Syntax errors, typos, stray text (e.g. a literal word "python" left
   over from a markdown code fence on line 1), indentation errors,
   unmatched brackets/quotes, undefined names.
7. Obviously missing entries that SHOULD be in requirements.txt given the
   imports used (e.g. code imports asyncpg but it's not installed/listed).

Respond ONLY in the following strict format. If there are NO issues, output
exactly the single line: NO_ISSUES_FOUND

For each issue found, output a block in exactly this format (repeat for
each issue):

### Issue: <short title>
**Severity:** <Critical|High|Medium|Low>
**Explanation:** <1-3 sentence plain-English explanation of the bug and why it breaks>
**Original Code:**
```python
<the exact problematic snippet, minimal but with enough context>
```
**Corrected Code:**
```python
<the fixed, ready-to-paste replacement>
```

Do not include any other commentary, preamble, or sign-off text outside
this format.
"""


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------

@dataclass
class FileResult:
    path: str
    status: str  # "ok" | "issues" | "error" | "skipped_empty"
    analysis: str = ""
    error_message: str = ""


# ------------------------------------------------------------------
# File discovery
# ------------------------------------------------------------------

def discover_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]
        for fname in filenames:
            if fname.endswith(".py"):
                files.append(Path(dirpath) / fname)
    return sorted(files)


# ------------------------------------------------------------------
# Gemini call (with retry + concurrency control)
# ------------------------------------------------------------------

async def analyze_file(
    model: "genai.GenerativeModel",
    filepath: Path,
    root: Path,
    semaphore: asyncio.Semaphore,
) -> FileResult:
    rel_path = str(filepath.relative_to(root))

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return FileResult(path=rel_path, status="error", error_message=f"Could not read file: {e}")

    if not source.strip():
        return FileResult(path=rel_path, status="skipped_empty")

    truncated = False
    if len(source) > MAX_FILE_CHARS:
        source = source[:MAX_FILE_CHARS]
        truncated = True

    user_prompt = (
        f"File path: {rel_path}\n"
        f"{'(NOTE: file was truncated to fit context limits)' if truncated else ''}\n\n"
        f"```python\n{source}\n```"
    )

    async with semaphore:
        last_error: Optional[Exception] = None
        for attempt in range(1, API_RETRY_ATTEMPTS + 1):
            try:
                response = await model.generate_content_async(
                    [SYSTEM_PROMPT, user_prompt],
                    generation_config={"temperature": 0.1},
                )
                text = (response.text or "").strip()

                if not text or text == "NO_ISSUES_FOUND":
                    return FileResult(path=rel_path, status="ok")

                return FileResult(path=rel_path, status="issues", analysis=text)

            except Exception as e:
                last_error = e
                if attempt < API_RETRY_ATTEMPTS:
                    wait = API_RETRY_BACKOFF * attempt
                    print(f"  [retry {attempt}/{API_RETRY_ATTEMPTS}] {rel_path}: {e} — waiting {wait}s")
                    await asyncio.sleep(wait)

        return FileResult(
            path=rel_path,
            status="error",
            error_message=f"Gemini API failed after {API_RETRY_ATTEMPTS} attempts: {last_error}",
        )


# ------------------------------------------------------------------
# Report generation
# ------------------------------------------------------------------

def build_report(results: list[FileResult], root: Path, elapsed: float) -> str:
    total = len(results)
    ok = sum(1 for r in results if r.status == "ok")
    issues = [r for r in results if r.status == "issues"]
    errors = [r for r in results if r.status == "error"]
    skipped = sum(1 for r in results if r.status == "skipped_empty")

    lines: list[str] = []
    lines.append("# AI Debugger Report")
    lines.append("")
    lines.append(f"- **Project root:** `{root}`")
    lines.append(f"- **Generated at:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append(f"- **Scan duration:** {elapsed:.1f}s")
    lines.append(f"- **Files scanned:** {total}")
    lines.append(f"- **Clean files:** {ok}")
    lines.append(f"- **Files with issues:** {len(issues)}")
    lines.append(f"- **Files that failed to analyze:** {len(errors)}")
    lines.append(f"- **Empty files skipped:** {skipped}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if issues:
        lines.append("## 🔴 Files With Identified Issues")
        lines.append("")
        for r in issues:
            lines.append(f"## `{r.path}`")
            lines.append("")
            lines.append(r.analysis)
            lines.append("")
            lines.append("---")
            lines.append("")
    else:
        lines.append("## ✅ No issues found in any scanned file.")
        lines.append("")

    if errors:
        lines.append("## ⚠️ Files That Could Not Be Analyzed")
        lines.append("")
        for r in errors:
            lines.append(f"- `{r.path}` — {r.error_message}")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        return 1

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(MODEL_NAME)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: root path '{root}' is not a directory.", file=sys.stderr)
        return 1

    files = discover_python_files(root)
    if not files:
        print(f"No .py files found under {root}")
        return 0

    print(f"Scanning {len(files)} Python file(s) under {root} ...")
    semaphore = asyncio.Semaphore(args.concurrency)
    start = time.monotonic()

    tasks = [analyze_file(model, f, root, semaphore) for f in files]
    results: list[FileResult] = []

    for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
        result = await coro
        results.append(result)
        status_icon = {"ok": "✅", "issues": "🔴", "error": "⚠️", "skipped_empty": "⏭️"}[result.status]
        print(f"[{i}/{len(files)}] {status_icon} {result.path}")

    elapsed = time.monotonic() - start

    # Keep report output in stable file order rather than completion order
    order = {str(f.relative_to(root)): idx for idx, f in enumerate(files)}
    results.sort(key=lambda r: order.get(r.path, 0))

    report = build_report(results, root, elapsed)
    output_path = Path(args.output)
    output_path.write_text(report, encoding="utf-8")

    print(f"\nReport written to: {output_path.resolve()}")
    print(f"Done in {elapsed:.1f}s — {len(results)} files, "
          f"{sum(1 for r in results if r.status == 'issues')} with issues.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-powered Python repo debugger using Gemini.")
    parser.add_argument("--root", default=".", help="Root directory to scan (default: current dir)")
    parser.add_argument("--output", default="debug_report.md", help="Output markdown file path")
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Max concurrent Gemini requests (default: {DEFAULT_CONCURRENCY})",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()