#!/usr/bin/env python3
"""Dispatch manual 10-codes to concrete local commands."""

from __future__ import annotations

import argparse
from collections import Counter
import datetime as dt
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional

from env_utils import (
    load_workspace_env,
    resolve_hermes_home,
    resolve_session_path,
    get_or_create_session_id,
    HERMES_SESSION_ID_ENV,
)
from api_requirements import validate_preflight

ROOT = Path(os.environ.get("HERMES_10_CODES_ROOT", Path(__file__).resolve().parent.parent))
FINTEL_CONTEXT_FILE = ROOT / "tmp" / "fintel_context_from_query.json"

# Prefer the workspace venv so installed packages (yfinance, etc.) are available
_VENV_PY = ROOT / ".venv" / "bin" / "python3"
PYTHON = str(_VENV_PY) if _VENV_PY.exists() else sys.executable
FINTEL_CONTEXT_FILE_ENV = "FINTEL_CONTEXT_FILE"
FINTEL_CONTEXT_JSON_ENV = "FINTEL_CONTEXT_JSON"
FINTEL_CONTEXT_STATUS_ENV = "FINTEL_CONTEXT_STATUS"
OBSIDIAN_VAULT_ENV = "OBSIDIAN_VAULT_PATH"
OBSIDIAN_EXPORT_ENABLED_ENV = "OBSIDIAN_EXPORT_ENABLED"
OBSIDIAN_EXPORT_SUBDIR_ENV = "OBSIDIAN_EXPORT_SUBDIR"
OBSIDIAN_DAILY_SUBDIR_ENV = "OBSIDIAN_DAILY_SUBDIR"
OBSIDIAN_WEEKLY_SUBDIR_ENV = "OBSIDIAN_WEEKLY_SUBDIR"
RUN_CODE_ENV = "HERMES_RUN_CODE"
RUN_QUERY_ENV = "HERMES_RUN_QUERY"
RUN_MODE_ENV = "HERMES_RUN_MODE"
RUN_JSON_ENV = "HERMES_RUN_JSON"
TRACE_ID_ENV = "HERMES_TRACE_ID"
RUN_TASK_ID_ENV = "HERMES_TASK_ID"
API_PREFLIGHT_ENV = "HERMES_API_PREFLIGHT"
WEEKLY_SUMMARY_START = "<!-- HERMES_WEEKLY_SUMMARY_START -->"
WEEKLY_SUMMARY_END = "<!-- HERMES_WEEKLY_SUMMARY_END -->"

CodeRunner = Callable[[argparse.Namespace, Optional[str]], int]


def resolve_trace_id() -> str:
    existing = (os.getenv(TRACE_ID_ENV) or "").strip()
    if existing:
        return existing
    trace_id = f"trace_{uuid.uuid4().hex[:12]}"
    os.environ[TRACE_ID_ENV] = trace_id
    return trace_id


def current_fintel_context_file() -> Path:
    configured = (os.getenv(FINTEL_CONTEXT_FILE_ENV) or "").strip()
    if configured:
        return Path(configured)
    return FINTEL_CONTEXT_FILE


def initialize_run_context(args: argparse.Namespace) -> str:
    trace_id = resolve_trace_id()
    session_id = get_or_create_session_id()
    os.environ[RUN_CODE_ENV] = args.code.strip().lower()
    os.environ[RUN_QUERY_ENV] = args.query or ""
    os.environ[RUN_MODE_ENV] = args.mode or ""
    os.environ[RUN_JSON_ENV] = "1" if args.json else "0"

    fintel_path = ROOT / "tmp" / "fintel_context" / f"{trace_id}.json"
    fintel_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ[FINTEL_CONTEXT_FILE_ENV] = str(fintel_path)
    os.environ.pop(FINTEL_CONTEXT_JSON_ENV, None)
    os.environ[FINTEL_CONTEXT_STATUS_ENV] = "not-requested"

    code = args.code.strip().lower()
    preflight = validate_preflight(code)
    os.environ[API_PREFLIGHT_ENV] = json.dumps(preflight, ensure_ascii=True)
    
    # Ensure session ID is available to child processes
    os.environ[HERMES_SESSION_ID_ENV] = session_id
    return trace_id


def cleanup_run_context() -> None:
    fintel_path = current_fintel_context_file()
    try:
        if fintel_path != FINTEL_CONTEXT_FILE and fintel_path.exists():
            fintel_path.unlink()
    except OSError:
        pass

def query_requests_fintel(query: str | None) -> bool:
    if not query:
        return False
    return bool(
        re.search(
            r"fintel|institutional\\s+ownership|insider\\s+trading|13f|short[\\s-]+volume|short[\\s-]+interest|holders|current\\s+price|\\bprice\\b|\\bquote\\b|squeeze|gamma|max\\s+pain|efur",
            query,
            flags=re.IGNORECASE,
        )
    )

def build_fintel_context(query: str | None) -> tuple[bool, str]:
    script_path = ROOT / "scripts" / "10_103_fintel_snapshot.py"
    command = [PYTHON, str(script_path), "--json"]
    if query:
        command.extend(["--query", query])

    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        os.environ[FINTEL_CONTEXT_STATUS_ENV] = "timeout"
        return False, "Fintel context timed out"

    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "fintel context failed").strip()
        os.environ[FINTEL_CONTEXT_STATUS_ENV] = f"error:{completed.returncode}"
        return False, details

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        os.environ[FINTEL_CONTEXT_STATUS_ENV] = "empty"
        return False, "Fintel context returned no output"

    raw_json = lines[-1]
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        os.environ[FINTEL_CONTEXT_STATUS_ENV] = "parse-error"
        return False, "Fintel context returned non-JSON output"

    fintel_context_file = current_fintel_context_file()
    fintel_context_file.parent.mkdir(parents=True, exist_ok=True)
    fintel_context_file.write_text(json.dumps(parsed, indent=2), encoding="utf-8")

    # Strip the `raw` owners blob before storing in the env var — downstream scripts
    # only need the normalized fields and it can be ~200 KB of owner records.
    context_for_env = {k: v for k, v in parsed.items() if k != "raw"}

    os.environ[FINTEL_CONTEXT_FILE_ENV] = str(fintel_context_file)
    os.environ[FINTEL_CONTEXT_JSON_ENV] = json.dumps(context_for_env, ensure_ascii=True)
    os.environ[FINTEL_CONTEXT_STATUS_ENV] = "ok"
    return True, str(fintel_context_file)

def extract_option_id(query: str | None) -> str | None:
    if not query:
        return None
    patterns = [
        r"option[_\\s-]*id\\s*[:=]\\s*([^\\s,;]+)",
        r"option[_\\s-]*id\\s+([^\\s,;]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def obsidian_export_enabled() -> bool:
    raw = (os.getenv(OBSIDIAN_EXPORT_ENABLED_ENV, "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}

def resolve_obsidian_vault_path() -> Path | None:
    configured = (os.getenv(OBSIDIAN_VAULT_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()

    obsidian_config = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    if obsidian_config.exists():
        try:
            parsed = json.loads(obsidian_config.read_text(encoding="utf-8"))
            vaults = parsed.get("vaults", {}) if isinstance(parsed, dict) else {}
            if isinstance(vaults, dict) and vaults:
                best = sorted(
                    [v for v in vaults.values() if isinstance(v, dict) and v.get("path")],
                    key=lambda item: (1 if item.get("open") else 0, int(item.get("ts") or 0)),
                    reverse=True,
                )
                if best:
                    return Path(str(best[0]["path"]))
        except Exception:
            pass

    default_path = Path.home() / "Documents" / "Obsidian Vault"
    if default_path.exists():
        return default_path
    return None

def _slugify(text: str, max_len: int = 64) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    if not cleaned:
        return "run"
    return cleaned[:max_len]

def _trim_text(text: str, max_chars: int = 30000) -> str:
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    return text[:keep] + "\n\n... output truncated ...\n\n" + text[-keep:]

def _infer_ecosystem_from_query(query: str) -> str | None:
    q = (query or "").strip().lower()
    if not q or q == "-":
        return None
    patterns = {
        "ethereum": ("ethereum", " eth", "eth ", "eth/"),
        "solana": ("solana", " sol", "sol "),
        "bitcoin": ("bitcoin", " btc", "btc "),
        "bnb": ("bnb", "bsc", "binance"),
        "tron": ("tron", " trx", "trx "),
        "avalanche": ("avalanche", "avax"),
        "polygon": ("polygon", "matic", " pol"),
        "arbitrum": ("arbitrum", " arb"),
        "base": (" base", "base "),
    }
    for name, tokens in patterns.items():
        if any(tok in q for tok in tokens):
            return name
    return None

def _extract_weekly_rows(lines: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in lines:
        s = line.strip()
        if not s.startswith("|"):
            continue
        if s.startswith("| ---"):
            continue
        cells = [c.strip() for c in s.split("|")[1:-1]]
        if len(cells) < 8:
            continue
        if cells[0] == "Date" and cells[1] == "Time UTC":
            continue
        rows.append(
            {
                "date": cells[0],
                "time": cells[1],
                "code": cells[2],
                "status": cells[3],
                "exit": cells[4],
                "query": cells[5],
                "daily": cells[6],
                "run": cells[7],
            }
        )
    return rows

def _weekly_summary_block(lines: list[str]) -> list[str]:
    rows = _extract_weekly_rows(lines)
    total = len(rows)
    success = sum(1 for r in rows if r.get("status") == "success")
    success_pct = (success / total * 100.0) if total else 0.0

    stamped_rows: list[tuple[dt.datetime, dict[str, str]]] = []
    for row in rows:
        date_txt = (row.get("date") or "").strip()
        time_txt = (row.get("time") or "").strip()
        try:
            stamp = dt.datetime.strptime(f"{date_txt} {time_txt}", "%Y-%m-%d %H:%M:%S")
            stamped_rows.append((stamp, row))
        except ValueError:
            continue

    latest_line = "none"
    if stamped_rows:
        latest_stamp, latest_row = max(stamped_rows, key=lambda item: item[0])
        latest_line = (
            f"{latest_stamp.strftime('%Y-%m-%d %H:%M:%S')} UTC "
            f"({latest_row.get('code', '?')}, {latest_row.get('status', '?')})"
        )

    code_counts = Counter(r.get("code", "") for r in rows if r.get("code"))
    top_codes = ", ".join(f"{k} x{v}" for k, v in code_counts.most_common(3)) if code_counts else "none"

    eco_counts = Counter()
    for r in rows:
        eco = _infer_ecosystem_from_query(r.get("query", ""))
        if eco:
            eco_counts[eco] += 1
    top_ecos = ", ".join(f"{k} x{v}" for k, v in eco_counts.most_common(3)) if eco_counts else "none detected"

    failing_rows = [item for item in stamped_rows if item[1].get("status") != "success"]
    last_failing_line = "none"
    if failing_rows:
        fail_stamp, fail_row = max(failing_rows, key=lambda item: item[0])
        last_failing_line = (
            f"{fail_stamp.strftime('%Y-%m-%d %H:%M:%S')} UTC "
            f"({fail_row.get('code', '?')}, exit {fail_row.get('exit', '?')}, query: {fail_row.get('query', '-')})"
        )

    return [
        "## Weekly Summary",
        "",
        WEEKLY_SUMMARY_START,
        f"- Total runs: {total}",
        f"- Success rate: {success}/{total} ({success_pct:.1f}%)",
        f"- Top codes: {top_codes}",
        f"- Top queried ecosystems: {top_ecos}",
        f"- Most recent run: {latest_line}",
        f"- Last failing code: {last_failing_line}",
        WEEKLY_SUMMARY_END,
        "",
    ]

def _upsert_weekly_summary(lines: list[str]) -> list[str]:
    summary = _weekly_summary_block(lines)

    start_idx = next((i for i, ln in enumerate(lines) if ln.strip() == WEEKLY_SUMMARY_START), None)
    end_idx = next((i for i, ln in enumerate(lines) if ln.strip() == WEEKLY_SUMMARY_END), None)

    if start_idx is not None and end_idx is not None and end_idx >= start_idx:
        # Replace existing summary block, preserving heading immediately above if present.
        heading_idx = start_idx - 2 if start_idx >= 2 and lines[start_idx - 2].strip() == "## Weekly Summary" else None
        if heading_idx is not None:
            return lines[:heading_idx] + summary + lines[end_idx + 1 :]
        return lines[:start_idx] + summary + lines[end_idx + 1 :]

    run_log_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "## Run Log"), None)
    if run_log_idx is None:
        return lines + [""] + summary
    return lines[:run_log_idx] + summary + lines[run_log_idx:]

def update_obsidian_daily_rollup(
    vault: Path,
    run_note_path: Path,
    timestamp_utc: dt.datetime,
    code: str,
    query: str,
    mode: str,
    status: str,
    returncode: int,
) -> Path:
    daily_subdir = (os.getenv(OBSIDIAN_DAILY_SUBDIR_ENV) or "Hermes/10-codes/Daily").strip("/")
    day = timestamp_utc.strftime("%Y-%m-%d")
    daily_dir = vault / daily_subdir
    daily_dir.mkdir(parents=True, exist_ok=True)
    daily_path = daily_dir / f"{day}.md"

    ts_short = timestamp_utc.strftime("%H:%M:%S")
    run_rel = run_note_path.relative_to(vault).as_posix()
    wiki_link = run_rel[:-3] if run_rel.endswith(".md") else run_rel
    query_text = query if query else "-"
    mode_text = mode if mode else "-"
    log_line = (
        f"| {ts_short} | {code} | {status} | {returncode} | {mode_text} | {query_text} | [[{wiki_link}|open]] |"
    )

    if not daily_path.exists():
        header = [
            "---",
            'type: "hermes-daily-rollup"',
            f'date: "{day}"',
            f'generated_at: "{timestamp_utc.isoformat(timespec="seconds").replace("+00:00", "Z")}"',
            'tags: ["hermes", "daily", "10-codes"]',
            "---",
            "",
            f"# Hermes Daily Rollup {day}",
            "",
            "## Run Log",
            "",
            "| Time UTC | Code | Status | Exit | Mode | Query | Note |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            log_line,
            "",
            "## Notes",
            "",
        ]
        daily_path.write_text("\n".join(header), encoding="utf-8")
        print(f"[obsidian] daily rollup created {daily_path}", file=sys.stderr)
        return daily_path

    existing = daily_path.read_text(encoding="utf-8")
    if log_line in existing:
        return

    lines = existing.splitlines()
    insert_at = None
    for idx, line in enumerate(lines):
        if line.strip() == "| --- | --- | --- | --- | --- | --- | --- |":
            insert_at = idx + 1
            break

    if insert_at is None:
        lines.extend(
            [
                "",
                "## Run Log",
                "",
                "| Time UTC | Code | Status | Exit | Mode | Query | Note |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                log_line,
            ]
        )
    else:
        lines.insert(insert_at, log_line)

    daily_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[obsidian] daily rollup updated {daily_path}", file=sys.stderr)
    return daily_path

def update_obsidian_weekly_rollup(
    vault: Path,
    daily_rollup_path: Path,
    run_note_path: Path,
    timestamp_utc: dt.datetime,
    code: str,
    query: str,
    status: str,
    returncode: int,
) -> None:
    weekly_subdir = (os.getenv(OBSIDIAN_WEEKLY_SUBDIR_ENV) or "Hermes/10-codes/Weekly").strip("/")
    iso = timestamp_utc.isocalendar()
    week_id = f"{iso.year}-W{iso.week:02d}"
    weekly_dir = vault / weekly_subdir
    weekly_dir.mkdir(parents=True, exist_ok=True)
    weekly_path = weekly_dir / f"{week_id}.md"

    day = timestamp_utc.strftime("%Y-%m-%d")
    ts_short = timestamp_utc.strftime("%H:%M:%S")
    query_text = query if query else "-"

    daily_rel = daily_rollup_path.relative_to(vault).as_posix()
    run_rel = run_note_path.relative_to(vault).as_posix()
    daily_link = daily_rel[:-3] if daily_rel.endswith(".md") else daily_rel
    run_link = run_rel[:-3] if run_rel.endswith(".md") else run_rel

    log_line = (
        f"| {day} | {ts_short} | {code} | {status} | {returncode} | {query_text} "
        f"| [[{daily_link}|daily]] | [[{run_link}|run]] |"
    )

    if not weekly_path.exists():
        header = [
            "---",
            'type: "hermes-weekly-rollup"',
            f'week: "{week_id}"',
            f'generated_at: "{timestamp_utc.isoformat(timespec="seconds").replace("+00:00", "Z")}"',
            'tags: ["hermes", "weekly", "10-codes"]',
            "---",
            "",
            f"# Hermes Weekly Rollup {week_id}",
            "",
            "## Weekly Summary",
            "",
            WEEKLY_SUMMARY_START,
            "- Total runs: 0",
            "- Success rate: 0/0 (0.0%)",
            "- Top codes: none",
            "- Top queried ecosystems: none detected",
            "- Most recent run: none",
            "- Last failing code: none",
            WEEKLY_SUMMARY_END,
            "",
            "## Run Log",
            "",
            "| Date | Time UTC | Code | Status | Exit | Query | Daily | Run |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
            log_line,
            "",
            "## Notes",
            "",
        ]
        header = _upsert_weekly_summary(header)
        weekly_path.write_text("\n".join(header), encoding="utf-8")
        print(f"[obsidian] weekly rollup created {weekly_path}", file=sys.stderr)
        return

    existing = weekly_path.read_text(encoding="utf-8")
    if log_line in existing:
        return

    lines = existing.splitlines()
    insert_at = None
    for idx, line in enumerate(lines):
        if line.strip() == "| --- | --- | --- | --- | --- | --- | --- | --- |":
            insert_at = idx + 1
            break

    if insert_at is None:
        lines.extend(
            [
                "",
                "## Run Log",
                "",
                "| Date | Time UTC | Code | Status | Exit | Query | Daily | Run |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
                log_line,
            ]
        )
    else:
        lines.insert(insert_at, log_line)

    lines = _upsert_weekly_summary(lines)
    weekly_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[obsidian] weekly rollup updated {weekly_path}", file=sys.stderr)

def export_run_to_obsidian(script_path: Path, command: list[str], returncode: int, stdout: str, stderr: str) -> None:
    if not obsidian_export_enabled():
        return

    vault = resolve_obsidian_vault_path()
    if vault is None:
        return

    code = (os.getenv(RUN_CODE_ENV) or script_path.stem).strip()
    query = (os.getenv(RUN_QUERY_ENV) or "").strip()
    mode = (os.getenv(RUN_MODE_ENV) or "").strip()
    json_requested = (os.getenv(RUN_JSON_ENV) or "").strip() == "1"
    status = "success" if returncode == 0 else "failed"

    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    date_dir = now.strftime("%Y-%m-%d")
    subdir = (os.getenv(OBSIDIAN_EXPORT_SUBDIR_ENV) or "Hermes/10-codes/runs").strip("/")
    out_dir = vault / subdir / date_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    query_slug = _slugify(query) if query else "no-query"
    filename = f"{stamp}_{_slugify(code)}_{query_slug}.md"
    note_path = out_dir / filename

    frontmatter = {
        "type": "hermes-run",
        "trace_id": (os.getenv(TRACE_ID_ENV) or "").strip(),
        "task_id": (os.getenv(RUN_TASK_ID_ENV) or "").strip(),
        "code": code,
        "status": status,
        "exit_code": returncode,
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "host": socket.gethostname(),
        "query": query,
        "mode": mode,
        "json": json_requested,
        "script": str(script_path.relative_to(ROOT)) if script_path.is_relative_to(ROOT) else str(script_path),
    }

    lines = ["---"]
    lines.extend(f"{k}: {json.dumps(v, ensure_ascii=True)}" for k, v in frontmatter.items())
    lines.extend(
        [
            "---",
            "",
            f"# {code} Run",
            "",
            "## Command",
            "",
            "```bash",
            shlex.join(command),
            "```",
            "",
            "## Stdout",
            "",
            "```text",
            _trim_text(stdout or ""),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            _trim_text(stderr or ""),
            "```",
            "",
        ]
    )
    note_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[obsidian] wrote {note_path}", file=sys.stderr)
    daily_path = update_obsidian_daily_rollup(
        vault=vault,
        run_note_path=note_path,
        timestamp_utc=now,
        code=code,
        query=query,
        mode=mode,
        status=status,
        returncode=returncode,
    )
    update_obsidian_weekly_rollup(
        vault=vault,
        daily_rollup_path=daily_path,
        run_note_path=note_path,
        timestamp_utc=now,
        code=code,
        query=query,
        status=status,
        returncode=returncode,
    )

def run_script(script_path: Path, extra_args: list[str] | None = None) -> int:
    command = [PYTHON, str(script_path)]
    if extra_args:
        command.extend(extra_args)
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env=os.environ.copy())
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    try:
        export_run_to_obsidian(
            script_path=script_path,
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
    except Exception as exc:
        print(f"[obsidian] export failed: {exc}", file=sys.stderr)
    return completed.returncode

def run_10_100(query: str | None = None) -> int:
    """10-100: Crypto DCF valuation alias to 10-101 v14.

    query may be a single ticker or comma/space-separated list, e.g.
    "ETH", "ETH BTC", "ETH,BTC,SOL".
    """
    import re as _re
    script_path = ROOT / "10-101_eth_val_v14.py"
    if not query:
        return run_script(script_path)

    raw_tickers = [t.strip().upper() for t in _re.split(r'[,\s]+', query.strip()) if t.strip()]
    if not raw_tickers:
        return run_script(script_path)

    if len(raw_tickers) == 1:
        return run_script(script_path, extra_args=["--ticker", raw_tickers[0]])

    worst_rc = 0
    for ticker in raw_tickers:
        rc = run_script(script_path, extra_args=["--ticker", ticker])
        if rc != 0:
            worst_rc = rc
    return worst_rc

def run_10_011(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_011_research_intel.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    else:
        extra_args.extend(["--query", "latest market intelligence"])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_101() -> int:
    script_path = ROOT / "10-101_eth_val_v14.py"
    return run_script(script_path)

def run_10_097(query: str | None = None) -> int:
    script_path = ROOT / "scripts" / "10_097_multi_dex.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    return run_script(script_path, extra_args=extra_args)

def run_10_004(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_004_crypto_catalyst_brief.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_099(query: str | None = None) -> int:
    """10-99: Crypto DCF valuation (multi-ticker alias to 10-101 v14).

    query may be a single ticker or comma/space-separated list, e.g.
    "ETH", "ETH BTC", "ETH,BTC,SOL".
    """
    import re as _re
    script_path = ROOT / "10-101_eth_val_v14.py"
    if not query:
        return run_script(script_path)

    # Parse comma or space separated tickers
    raw_tickers = [t.strip().upper() for t in _re.split(r'[,\s]+', query.strip()) if t.strip()]
    if not raw_tickers:
        return run_script(script_path)

    if len(raw_tickers) == 1:
        return run_script(script_path, extra_args=["--ticker", raw_tickers[0]])

    # Multiple tickers — run sequentially and return worst RC
    worst_rc = 0
    for ticker in raw_tickers:
        rc = run_script(script_path, extra_args=["--ticker", ticker])
        if rc != 0:
            worst_rc = rc
    return worst_rc

def run_10_102(option_id: str | None, query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "crypto_sfr.py"
    extra_args: list[str] = []
    if option_id:
        extra_args.extend(["--option-id", option_id])
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_103(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_103_fintel_snapshot.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_077(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_077_squeeze_multi_tool.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_073(query: str | None, symbol: str | None = None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_073_short_interest_compare.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if symbol:
        extra_args.extend(["--symbol", symbol])
        return run_script(script_path, extra_args=extra_args)
    # no explicit symbol: extract all tickers from query and pass as --symbol list
    if query:
        import re as _re
        tickers = [t for t in _re.findall(r'\\b[A-Z]{1,5}\\b', query.upper())
                   if t not in {"SI", "SHORT", "INTEREST", "FINTEL", "YF", "COMPARE", "FOR", "VS", "AND", "OR", "THE", "US"}]
        if tickers:
            extra_args.extend(["--symbol", ",".join(tickers)])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_104(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_104_eth_ecosystem_health.py"
    extra_args: list[str] = ["--allow-partial"]
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_105(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_105_avax_ecosystem_health.py"
    extra_args: list[str] = ["--allow-partial"]
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_203(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_203_zero_human_backtest.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_300(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_300_wrrc_pipeline.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_301(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_301_wrrc_synthesizer.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_305(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_305_rag_query.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    else:
        extra_args.append("--reindex")
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_306(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_306_market_strategy_fusion.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    else:
        extra_args.extend(["--query", "NVDA"])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_307(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_307_hftg_checklist.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_308(
    query: str | None,
    json_output: bool = False,
    mode: str | None = None,
    tickers: str | None = None,
    watchlist_source: str | None = None,
    core_review_mode: str | None = None,
    max_core_changes: int | None = None,
    rotational_slots: int | None = None,
    no_daily: bool = False,
    no_weekly: bool = False,
) -> int:
    script_path = ROOT / "scripts" / "10_308_wrrc_orchestrator.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if mode:
        extra_args.extend(["--mode", mode])
    if tickers:
        extra_args.extend(["--tickers", tickers])
    if watchlist_source:
        extra_args.extend(["--watchlist-source", watchlist_source])
    if core_review_mode:
        extra_args.extend(["--core-review-mode", core_review_mode])
    if max_core_changes is not None:
        extra_args.extend(["--max-core-changes", str(max_core_changes)])
    if rotational_slots is not None:
        extra_args.extend(["--rotational-slots", str(rotational_slots)])
    if no_daily:
        extra_args.append("--no-daily")
    if no_weekly:
        extra_args.append("--no-weekly")
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_309(
    query: str | None,
    json_output: bool = False,
    mode: str | None = None,
    profile: str | None = None,
) -> int:
    script_path = ROOT / "scripts" / "10_309_ankh_scope_manager.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if mode:
        extra_args.extend(["--mode", mode])
    if profile:
        extra_args.extend(["--profile", profile])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_310(
    query: str | None,
    json_output: bool = False,
    mode: str | None = None,
) -> int:
    script_path = ROOT / "scripts" / "10_310_hermes_skills_catalog.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if mode:
        extra_args.extend(["--mode", mode])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_200(
    query: str | None,
    json_output: bool = False,
    mode: str | None = None,
    watchlist_master: str | None = None,
    tickers: str | None = None,
    watchlist_source: str | None = None,
    core_review_mode: str | None = None,
    max_core_changes: int | None = None,
    rotational_slots: int | None = None,
) -> int:
    script_path = ROOT / "10-200_ceo.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    if mode:
        extra_args.extend(["--mode", mode])
    if watchlist_master:
        extra_args.extend(["--watchlist-master", watchlist_master])
    if tickers:
        extra_args.extend(["--tickers", tickers])
    if watchlist_source:
        extra_args.extend(["--watchlist-source", watchlist_source])
    if core_review_mode:
        extra_args.extend(["--core-review-mode", core_review_mode])
    if max_core_changes is not None:
        extra_args.extend(["--max-core-changes", str(max_core_changes)])
    if rotational_slots is not None:
        extra_args.extend(["--rotational-slots", str(rotational_slots)])
    return run_script(script_path, extra_args=extra_args)

def run_10_201(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_201_ceo_status.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_150(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts/sfr_v42.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_311(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_311_emerging_sector_scanner.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_312(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_312_emerging_player_discovery.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_313(
    query: str | None,
    json_output: bool = False,
    mode: str | None = None,
    core_review_mode: str | None = None,
    max_core_changes: int | None = None,
    rotational_slots: int | None = None,
) -> int:
    script_path = ROOT / "scripts" / "10_313_dynamic_watchlist_manager.py"
    extra_args: list[str] = []
    _mode = (mode or "").strip().lower()
    if _mode:
        extra_args.extend(["--mode", _mode])
    if query:
        extra_args.extend(["--query", query])
    if core_review_mode:
        extra_args.extend(["--core-review-mode", core_review_mode])
    if max_core_changes is not None:
        extra_args.extend(["--max-core-changes", str(max_core_changes)])
    if rotational_slots is not None:
        extra_args.extend(["--slots", str(rotational_slots)])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_314(
    query: str | None,
    json_output: bool = False,
    mode: str | None = None,
    master_file: str | None = None,
    lookback_days: int | None = None,
    file_limit: int | None = None,
    retain_proposals: int | None = None,
) -> int:
    script_path = ROOT / "scripts" / "10_314_thesis_development_manager.py"
    extra_args: list[str] = []
    _mode = (mode or "").strip().lower()
    if _mode:
        extra_args.extend(["--mode", _mode])
    if query:
        extra_args.extend(["--query", query])

    inferred_master = None
    if _mode in {"checklist", "summary", "complete-merge"} and not master_file:
        state_path = ROOT / "data" / "10-314" / "master_thesis_guard_state.json"
        if state_path.exists() and state_path.is_file():
            try:
                state_payload = json.loads(state_path.read_text(encoding="utf-8"))
                locked_rel = str(state_payload.get("locked_master_rel") or "").strip()
                if locked_rel:
                    locked_path = (ROOT / locked_rel).resolve()
                    if locked_path.exists() and locked_path.is_file() and locked_path.parent == ROOT:
                        inferred_master = locked_rel
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                inferred_master = None
        if not inferred_master:
            root_masters = sorted(ROOT.glob("UNIFIED_MASTER_THESIS*.md"))
            root_masters = [p for p in root_masters if p.is_file()]
            if len(root_masters) == 1:
                inferred_master = root_masters[0].name

    effective_master = master_file or inferred_master
    if effective_master:
        extra_args.extend(["--master-file", effective_master])

    if lookback_days is not None:
        extra_args.extend(["--lookback-days", str(lookback_days)])
    if file_limit is not None:
        extra_args.extend(["--file-limit", str(file_limit)])
    if retain_proposals is not None:
        extra_args.extend(["--retain-proposals", str(retain_proposals)])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_204(query: str | None, json_output: bool = False, mode: str | None = None) -> int:
    script_path = ROOT / "scripts" / "10_204_daily_scheduler.py"
    extra_args: list[str] = []
    _mode = (mode or "").strip().lower()
    if _mode:
        extra_args.extend(["--mode", _mode])
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_315(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_315_paperclip_bridge_health.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_316(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_316_paperclip_policy_health.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_318(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_318_execution_controller.py"
    extra_args: list[str] = []
    if query:
        extra_args.extend(["--query", query])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_319(json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_319_schema_guard.py"
    extra_args: list[str] = ["--json"] if json_output else []
    return run_script(script_path, extra_args=extra_args)

def run_10_320(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_320_options_playbook.py"
    extra_args: list[str] = []
    if query:
        # Optional shorthand query form: "iv=70 trend=1 conviction=8 risk=1"
        parts = [p.strip() for p in query.split() if p.strip()]
        for part in parts:
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            key = k.strip().lower()
            val = v.strip()
            if key in {"iv", "ivrank", "iv_rank"}:
                extra_args.extend(["--iv-rank", val])
            elif key in {"trend", "trend_strength"}:
                extra_args.extend(["--trend-strength", val])
            elif key in {"conviction", "cv"}:
                extra_args.extend(["--conviction", val])
            elif key in {"risk", "max_risk", "max_risk_pct"}:
                extra_args.extend(["--max-risk-pct", val])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_317(json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_317_cost_report.py"
    extra_args: list[str] = []
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_322(json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_322_sentiment_extraction.py"
    extra_args: list[str] = []
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def run_10_321(query: str | None, json_output: bool = False) -> int:
    script_path = ROOT / "scripts" / "10_321_hourly_opportunity_scanner.py"
    extra_args: list[str] = []
    if query:
        # Optional shorthand query form: "notify=1 min=50 max=4 watch=NVDA,TSLA"
        parts = [p.strip() for p in query.replace(",", " ").split() if p.strip()]
        watch_override = ""
        for part in parts:
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            key = k.strip().lower()
            val = v.strip()
            if key in {"notify", "telegram"} and val in {"1", "true", "yes", "on"}:
                extra_args.append("--notify")
            elif key in {"min", "min_conf", "min_confidence"}:
                extra_args.extend(["--min-confidence", val])
            elif key in {"max", "max_alerts"}:
                extra_args.extend(["--max-alerts", val])
            elif key in {"watch", "watchlist"}:
                watch_override = val
            elif key in {"dedup", "dedup_min", "dedup_window_min"}:
                extra_args.extend(["--dedup-window-min", val])
            elif key in {"improve", "min_improvement", "improvement"}:
                extra_args.extend(["--min-improvement", val])
            elif key in {"allow_risk_off_breakouts", "risk_off_breakouts"} and val in {"1", "true", "yes", "on"}:
                extra_args.append("--allow-risk-off-breakouts")
        if watch_override:
            extra_args.extend(["--watchlist", watch_override])
    if json_output:
        extra_args.append("--json")
    return run_script(script_path, extra_args=extra_args)

def discover_code_scripts(code: str) -> list[Path]:
    """Find candidate scripts for a 10-code when no explicit mapping exists."""
    search_roots = [
        ROOT,
        ROOT,
        ROOT / "scripts",
    ]
    patterns = [
        f"{code}*.py",
        f"{code.replace('-', '_')}*.py",
        f"{code}/**/*.py",
    ]

    candidates: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for pattern in patterns:
            for path in root.glob(pattern):
                if path.is_file():
                    candidates.add(path)

    return sorted(candidates)

# ==============================================================================
# CODE METADATA REGISTRY
# ==============================================================================
# Data-driven registry for 10-code configuration. Each code can specify:
# - query_support: bool (code accepts --query argument)
# - json_support: bool (code supports json_output)
# - option_id_support: bool (code uses option_id from runner args)
# - extra_args: dict of arg_name -> cli_flag mappings
# - pre_dispatch: optional callable for pre-processing (e.g., Fintel context)
# This enables data-driven dispatch without individual dispatch_ functions.
# ==============================================================================

CODE_METADATA = {
    "10-004": {"script": "scripts/10_004_crypto_catalyst_brief.py", "query": True, "json": True},
    "10-011": {"script": "scripts/10_011_research_intel.py", "query": True, "json": True},
    "10-018": {"script": "scripts/10-200_ceo.py", "query": True, "json": True},
    "10-073": {"script": "scripts/10_073_short_interest_compare.py", "query": True, "symbol": True, "json": True},
    "10-077": {"script": "scripts/10_077_squeeze_multi_tool.py", "query": True, "json": True, "fintel_context": True},
    "10-097": {"script": "scripts/10_097_multi_dex.py", "query": True},
    "10-099": {"script": "10-101_eth_val_v14.py", "query": True},
    "10-100": {"script": "10-101_eth_val_v14.py", "query": True, "json": True},
    "10-101": {"script": "10-101_eth_val_v14.py", "query": True},
    "10-102": {"script": "crypto_sfr.py", "query": True, "option_id": True, "json": True},
    "10-103": {"script": "scripts/10_103_fintel_snapshot.py", "query": True, "json": True},
    "10-104": {"script": "scripts/10_104_eth_ecosystem_health.py", "query": True, "json": True},
    "10-105": {"script": "scripts/10_105_avax_ecosystem_health.py", "query": True, "json": True},
    "10-150": {"script": "scripts/sfr_v42.py", "query": True, "json": True},
    "10-200": {"script": "10-200_ceo.py", "query": True, "json": True, "mode": True, "tickers": True, "watchlist_source": True, "core_review_mode": True, "max_core_changes": True, "rotational_slots": True, "watchlist_master": True},
    "10-201": {"script": "scripts/10_201_ceo_status.py", "query": True, "json": True},
    "10-203": {"script": "scripts/10_203_zero_human_backtest.py", "query": True, "json": True},
    "10-204": {"script": "scripts/10_204_daily_scheduler.py", "query": True, "json": True, "mode": True},
    "10-300": {"script": "scripts/10_300_wrrc_pipeline.py", "query": True, "json": True},
    "10-301": {"script": "scripts/10_301_wrrc_synthesizer.py", "query": True, "json": True},
    "10-305": {"script": "scripts/10_305_rag_query.py", "query": True, "json": True},
    "10-306": {"script": "scripts/10_306_market_strategy_fusion.py", "query": True, "json": True},
    "10-307": {"script": "scripts/10_307_hftg_checklist.py", "query": True, "json": True},
    "10-308": {"script": "scripts/10_308_wrrc_orchestrator.py", "query": True, "json": True, "mode": True, "tickers": True, "watchlist_source": True, "core_review_mode": True, "max_core_changes": True, "rotational_slots": True, "no_daily": True, "no_weekly": True},
    "10-309": {"script": "scripts/10_309_ankh_scope_manager.py", "query": True, "json": True, "mode": True, "profile": True},
    "10-310": {"script": "scripts/10_310_hermes_skills_catalog.py", "query": True, "json": True, "mode": True},
    "10-311": {"script": "scripts/10_311_emerging_sector_scanner.py", "query": True, "json": True},
    "10-312": {"script": "scripts/10_312_emerging_player_discovery.py", "query": True, "json": True},
    "10-313": {"script": "scripts/10_313_dynamic_watchlist_manager.py", "query": True, "json": True, "mode": True, "core_review_mode": True, "max_core_changes": True, "rotational_slots": True},
    "10-314": {"script": "scripts/10_314_thesis_development_manager.py", "query": True, "json": True, "mode": True, "master_file": True, "lookback_days": True, "file_limit": True, "retain_proposals": True},
    "10-315": {"script": "scripts/10_315_paperclip_bridge_health.py", "query": True, "json": True},
    "10-316": {"script": "scripts/10_316_paperclip_policy_health.py", "query": True, "json": True},
    "10-317": {"script": "scripts/10_317_cost_report.py", "json": True},
    "10-318": {"script": "scripts/10_318_execution_controller.py", "query": True, "json": True},
    "10-319": {"script": "scripts/10_319_schema_guard.py", "json": True},
    "10-320": {"script": "scripts/10_320_options_playbook.py", "query": True, "json": True},
    "10-321": {"script": "scripts/10_321_hourly_opportunity_scanner.py", "query": True, "json": True},
    "10-322": {"script": "scripts/10_322_sentiment_extraction.py", "json": True},
    "10-323": {"script": "scripts/10_323_csp_call_strategy.py", "query": True, "json": True},
    "10-324": {"script": "scripts/b01_system_health.py", "json": True},
    "10-900": {"script": "scripts/10_900_skill_evolution.py", "custom_dispatch": True},
}


def _build_generic_args_list(args: argparse.Namespace, metadata: dict) -> list[str]:
    """Build argument list from args namespace using metadata field mappings."""
    extra_args = []
    
    # Standard argument mappings (arg_name -> cli_flag)
    arg_mappings = {
        "mode": "--mode",
        "tickers": "--tickers",
        "watchlist_source": "--watchlist-source",
        "core_review_mode": "--core-review-mode",
        "max_core_changes": "--max-core-changes",
        "rotational_slots": "--rotational-slots",
        "watchlist_master": "--watchlist-master",
        "profile": "--profile",
        "no_daily": "--no-daily",
        "no_weekly": "--no-weekly",
        "master_file": "--master-file",
        "lookback_days": "--lookback-days",
        "file_limit": "--file-limit",
        "retain_proposals": "--retain-proposals",
    }
    
    for metadata_key, cli_flag in arg_mappings.items():
        if metadata.get(metadata_key):
            value = getattr(args, metadata_key.replace("-", "_"), None)
            if metadata_key in {"no_daily", "no_weekly"}:
                # Boolean flags
                if value:
                    extra_args.append(cli_flag)
            elif value is not None:
                extra_args.extend([cli_flag, str(value)])
    
    return extra_args


def _generic_dispatcher(args: argparse.Namespace, option_id: str | None, code: str, metadata: dict) -> int:
    """Generic dispatcher that uses metadata to route to run_* function."""
    script_name = metadata.get("script", "").strip()
    if not script_name:
        print(f"No script configured for {code}", file=sys.stderr)
        return 2
    
    script_path = ROOT / script_name if "/" not in script_name else ROOT / script_name
    script_path = ROOT / script_name
    
    extra_args = []
    
    if metadata.get("query"):
        if args.query:
            extra_args.extend(["--query", args.query])
    
    if metadata.get("option_id"):
        if option_id:
            extra_args.extend(["--option-id", option_id])
    
    if metadata.get("json"):
        if args.json:
            extra_args.append("--json")
    
    extra_args.extend(_build_generic_args_list(args, metadata))
    
    return run_script(script_path, extra_args=extra_args)


def dispatch_10_077(args: argparse.Namespace, _option_id: str | None) -> int:
    """Special handler: 10-77 always force-builds Fintel context for primary ticker."""
    import re as _re
    primary_sym = None
    if args.query:
        for tok in _re.findall(r'\\b[A-Z]{1,5}\\b', args.query.upper()):
            if tok not in {"SI", "SHORT", "SQUEEZE", "SCAN", "FOR", "VS", "AND", "OR", "THE", "US", "MULTI"}:
                primary_sym = tok
                break
    if primary_sym:
        build_fintel_context(primary_sym)
    metadata = CODE_METADATA.get("10-077", {})
    return _generic_dispatcher(args, _option_id, "10-077", metadata)


def dispatch_10_073(args: argparse.Namespace, _option_id: str | None) -> int:
    """Special handler: 10-73 extracts primary symbol and builds Fintel context."""
    import re as _re
    primary_sym = None
    sym_arg = getattr(args, "symbol", None) or ""
    for raw in sym_arg.split(","):
        s = raw.strip().upper()
        if s:
            primary_sym = s
            break
    if not primary_sym and args.query:
        for tok in _re.findall(r'\\b[A-Z]{1,5}\\b', args.query.upper()):
            if tok not in {"SI", "SHORT", "INTEREST", "FINTEL", "YF", "COMPARE", "FOR", "VS", "AND", "OR", "THE", "US"}:
                primary_sym = tok
                break
    if primary_sym:
        build_fintel_context(primary_sym)
    metadata = CODE_METADATA.get("10-073", {})
    return _generic_dispatcher(args, _option_id, "10-073", metadata)


def dispatch_10_099(args: argparse.Namespace, _option_id: str | None) -> int:
    """Special handler: 10-99 handles multi-ticker parsing."""
    return run_10_099(query=args.query)


def dispatch_10_102(args: argparse.Namespace, option_id: str | None) -> int:
    """Special handler: 10-102 passes option_id."""
    return run_10_102(option_id=option_id, query=args.query, json_output=args.json)


def dispatch_10_200(args: argparse.Namespace, _option_id: str | None) -> int:
    """Special handler: 10-200 (CEO) with complex parameter passing."""
    return run_10_200(
        query=args.query,
        json_output=args.json,
        mode=getattr(args, "mode", None),
        watchlist_master=getattr(args, "watchlist_master", None),
        tickers=getattr(args, "tickers", None),
        watchlist_source=getattr(args, "watchlist_source", None),
        core_review_mode=getattr(args, "core_review_mode", None),
        max_core_changes=getattr(args, "max_core_changes", None),
        rotational_slots=getattr(args, "rotational_slots", None),
    )

def dispatch_10_101(args: argparse.Namespace, _option_id: str | None) -> int:
    """Special handler: 10-101 reuses 10-99 logic for multi-ticker ETH valuation."""
    return run_10_099(query=args.query)


def dispatch_10_105(args: argparse.Namespace, _option_id: str | None) -> int:
    """Special handler: 10-105 preserves allow-partial behavior."""
    return run_10_105(query=args.query, json_output=args.json)


def dispatch_10_313(args: argparse.Namespace, _option_id: str | None) -> int:
    """Special handler: 10-313 maps rotational slots to --slots."""
    return run_10_313(
        query=args.query,
        json_output=args.json,
        mode=getattr(args, "mode", None),
        core_review_mode=getattr(args, "core_review_mode", None),
        max_core_changes=getattr(args, "max_core_changes", None),
        rotational_slots=getattr(args, "rotational_slots", None),
    )



def dispatch_generic(code: str):
    """Factory to create a generic dispatcher for a given code."""
    def dispatcher(args: argparse.Namespace, option_id: str | None) -> int:
        metadata = CODE_METADATA.get(code, {})
        return _generic_dispatcher(args, option_id, code, metadata)
    return dispatcher


# Automatically create generic dispatchers for codes not requiring special handling
_SIMPLE_CODES = {code for code in CODE_METADATA.keys()} - {"10-077", "10-073", "10-099", "10-102", "10-105", "10-200", "10-101", "10-313"}
for _code in _SIMPLE_CODES:
    _func = dispatch_generic(_code)
    _func.__name__ = f"dispatch_{_code.replace('-', '_')}"
    globals()[f"dispatch_{_code.replace('-', '_')}"] = _func


def dispatch_10_201(args: argparse.Namespace, _option_id: str | None) -> int:
    """10-201: CEO status check."""
    return run_10_201(query=args.query, json_output=args.json)


def dispatch_10_317(args: argparse.Namespace, _option_id: str | None) -> int:
    """10-317: Cost report."""
    return run_10_317(json_output=args.json)


def dispatch_10_018(args: argparse.Namespace, _option_id: str | None) -> int:
    """10-18: System health status check — runs 10-200 in health mode."""
    import socket
    # Prefer HERMES_NODE_ID (authoritative) over hostname heuristics
    callsign = os.getenv("HERMES_NODE_ID", "").strip()
    if not callsign:
        host = socket.gethostname()
        if "openclaw-ubuntu" in host or "ric1" in host:
            callsign = "A01"
        elif "Mac" in host or "mac" in host:
            callsign = "G01"
        else:
            callsign = host.split(".")[0]
    query = (args.query or "").strip()
    print(f"10-18 Status Check: {callsign}")
    return run_10_200(query=query, json_output=args.json, mode="health")


def dispatch_10_011(args: argparse.Namespace, _option_id: str | None) -> int:
    """10-11 v2.0: Enhanced Research & Current Intelligence with multi-asset support."""
    script_path = ROOT / "scripts" / "10_011_research_intel_v2.py"
    extra_args = []
    if args.query:
        extra_args.extend(["--query", args.query])
    if args.json:
        extra_args.append("--json")
    return run_script(script_path, extra_args or [])


def dispatch_10_900(args: argparse.Namespace, _option_id: str | None) -> int:
    """10-900: Hermes Skill Self-Evolution (DSPy + GEPA).

    Query syntax (passed via --query):
      skill=<name>              Skill to evolve (required unless list)
      iterations=<n>            GEPA iterations (default: 10)
      eval-source=<src>         synthetic|sessiondb|golden (default: sessiondb)
      dry-run                   Validate setup without running
      list                      List available skills
      optimizer-model=<model>   Override optimizer model
      eval-model=<model>        Override eval model

    Examples:
      run 10-900 --query "skill=research"
      run 10-900 --query "skill=github iterations=5 dry-run"
      run 10-900 --query list
    """
    script_path = ROOT / "scripts" / "10_900_skill_evolution.py"
    query = (args.query or "").strip()
    extra_args: list[str] = []

    # Parse tokens from query: flag-style (dry-run, list) and kv-style (skill=X)
    tokens = query.split()
    for token in tokens:
        t = token.lstrip("-")  # strip leading dashes for normalisation
        if t in {"list"}:
            extra_args.append("--list")
        elif t in {"dry-run", "dry_run", "dryrun"}:
            extra_args.append("--dry-run")
        elif "=" in t:
            k, v = t.split("=", 1)
            k = k.replace("_", "-")
            if k in {"skill"}:
                extra_args.extend(["--skill", v])
            elif k in {"iterations", "iter"}:
                extra_args.extend(["--iterations", v])
            elif k in {"eval-source", "eval_source"}:
                extra_args.extend(["--eval-source", v])
            elif k in {"optimizer-model", "optimizer_model"}:
                extra_args.extend(["--optimizer-model", v])
            elif k in {"eval-model", "eval_model"}:
                extra_args.extend(["--eval-model", v])

    # Bare token (no '=') that isn't a flag → treat as skill name
    if not any(a == "--skill" for a in extra_args) and not any(a == "--list" for a in extra_args):
        bare = [t for t in tokens if "=" not in t and t.lstrip("-") not in {"list", "dry-run", "dry_run", "dryrun"}]
        if bare:
            extra_args.extend(["--skill", bare[0]])

    # 10-900 is a long-running streaming process — bypass run_script (which uses
    # capture_output=True) to avoid the pipe-buffer deadlock when evolve_skill
    # produces > 64 KB of rich output mid-optimisation.
    cmd = [PYTHON, str(script_path)] + (extra_args or [])
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    return result.returncode


# Build CODE_REGISTRY from metadata and dispatch functions
CODE_REGISTRY: dict[str, CodeRunner] = {
    # Special cases with custom logic
    "10-018": dispatch_10_018,
    "10-900": dispatch_10_900,
    "10-73": dispatch_10_073,
    "10-077": dispatch_10_077,
    "10-099": dispatch_10_099,
    "10-101": dispatch_10_101,
    "10-102": dispatch_10_102,
    "10-105": dispatch_10_105,
    "10-200": dispatch_10_200,
    "10-201": dispatch_10_201,
    "10-313": dispatch_10_313,
    "10-317": dispatch_10_317,
    # Aliases
    "10-11": globals().get("dispatch_10_011"),
    "10-4": globals().get("dispatch_10_004"),
    "10-77": globals().get("dispatch_10_077"),
    "10-97": globals().get("dispatch_10_097"),
    "10-99": dispatch_10_099,
}

# Auto-populate registry from CODE_METADATA for codes not explicitly defined above
for code in CODE_METADATA.keys():
    if code not in CODE_REGISTRY:
        dispatcher_name = f"dispatch_{code.replace('-', '_')}"
        dispatcher = globals().get(dispatcher_name)
        if dispatcher:
            CODE_REGISTRY[code] = dispatcher
        else:
            # This shouldn't happen if auto-generation worked
            print(f"[warning] No dispatcher found for {code}", file=sys.stderr)

# Ensure aliases never resolve to None and keep deterministic ordering.
CODE_REGISTRY = {
    code: runner
    for code, runner in sorted(CODE_REGISTRY.items(), key=lambda item: item[0])
    if callable(runner)
}

# Hardening: assert every CODE_METADATA code made it into the registry.
_ALIAS_CODES = {c for c in CODE_REGISTRY if not c.startswith("10-0") and len(c.split("-")[1]) < 3}
_missing_from_registry = set(CODE_METADATA.keys()) - set(CODE_REGISTRY.keys())
if _missing_from_registry:
    print(f"[FATAL] CODE_METADATA codes missing from CODE_REGISTRY: {sorted(_missing_from_registry)}", file=sys.stderr)
    sys.exit(1)

SUPPORTED_CODES = tuple(CODE_REGISTRY.keys())

POSITIONAL_QUERY_FALLBACK_CODES = {


    "10-18",
    "10-73",
    "10-77",
    "10-97",
    "10-4",
    "10-99",
    "10-100",

    "10-102",
    "10-103",
    "10-104",
    "10-105",
    "10-150",
    "10-200",

    "10-203",
    "10-204",
    "10-300",

    "10-305",
    "10-306",
    "10-307",
    "10-308",
    "10-309",
    "10-310",

    "10-312",
    "10-313",
    "10-314",
    "10-315",
    "10-316",
    "10-318",
    "10-319",
    "10-320",
    "10-321",
    "10-323",
}

def main() -> int:
    load_workspace_env(ROOT)
    load_workspace_env(ROOT.parent)
    load_workspace_env(ROOT.parent.parent)

    parser = argparse.ArgumentParser(description="Run a manual OpenClaw 10-code")
    parser.add_argument("code", help="10-code identifier, e.g. 10-102")
    parser.add_argument("--option-id", dest="option_id", help="User-supplied option id")
    parser.add_argument("--query", help="Original freeform user query")
    parser.add_argument("--json", action="store_true", help="Request JSON output when supported")
    parser.add_argument("--mode", "--slot", dest="mode", default=None, help="Mode for codes that support it (10-200/10-204/10-308/10-309/10-310/10-313/10-314). --slot is an alias for --mode (10-204 daily scheduler slots)")
    parser.add_argument("--profile", default=None, help="Profile for 10-309 scope bootstrap")
    parser.add_argument("--tickers", default=None, help="Comma-separated watchlist for 10-200/10-308")
    parser.add_argument(
        "--watchlist-source",
        dest="watchlist_source",
        default=None,
        help="Watchlist provider for 10-200/10-308 (auto/static/dynamic for 10-200; static/dynamic for 10-308)",
    )
    parser.add_argument(
        "--core-review-mode",
        dest="core_review_mode",
        default=None,
        help="Pass-through core review mode for 10-200/10-308/10-313",
    )
    parser.add_argument(
        "--max-core-changes",
        dest="max_core_changes",
        type=int,
        default=None,
        help="Pass-through core swap cap for 10-200/10-308/10-313",
    )
    parser.add_argument(
        "--rotational-slots",
        dest="rotational_slots",
        type=int,
        default=None,
        help="Pass-through rotational slot count for 10-200/10-308/10-313",
    )
    parser.add_argument(
        "--watchlist-master",
        "--watchlist_master",
        dest="watchlist_master",
        default=None,
        help="Path to master watchlist file (JSON array or newline/CSV text); forwarded to 10-200",
    )
    parser.add_argument(
        "--no-daily",
        dest="no_daily",
        action="store_true",
        help="Pass-through for 10-308: skip daily macro heartbeat",
    )
    parser.add_argument(
        "--no-weekly",
        dest="no_weekly",
        action="store_true",
        help="Pass-through for 10-308: skip weekly parallel ticker execution",
    )
    parser.add_argument(
        "--master-file",
        dest="master_file",
        default=None,
        help="Pass-through for 10-314: canonical master thesis file used for checklist/complete-merge",
    )
    parser.add_argument(
        "--lookback-days",
        dest="lookback_days",
        type=int,
        default=None,
        help="Pass-through for 10-314: thesis scan lookback days",
    )
    parser.add_argument(
        "--file-limit",
        dest="file_limit",
        type=int,
        default=None,
        help="Pass-through for 10-314: thesis scan max file count",
    )
    parser.add_argument(
        "--retain-proposals",
        dest="retain_proposals",
        type=int,
        default=None,
        help="Pass-through for 10-314: retain latest N proposal artifacts",
    )
    args, extra = parser.parse_known_args()

    code = args.code.strip().lower()
    if extra:
        if args.query:
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
        if any(token.startswith("-") for token in extra):
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
        if code not in POSITIONAL_QUERY_FALLBACK_CODES:
            parser.error(f"unrecognized arguments: {' '.join(extra)}")
        args.query = " ".join(extra).strip()
        print(
            f"[runner] positional query fallback applied for {args.code}: {args.query}",
            file=sys.stderr,
        )

    trace_id = initialize_run_context(args)
    option_id = args.option_id or extract_option_id(args.query)
    print(f"[runner] trace_id={trace_id}", file=sys.stderr)

    try:
        # For any Fintel-like request (except 10-73 which manages its own context),
        # prefetch normalized Fintel context once and expose to all 10-codes via env vars.
        if code != "10-73" and query_requests_fintel(args.query):
            ok, detail = build_fintel_context(args.query)
            if ok:
                print(f"Fintel context ready: {detail}", file=sys.stderr)
            else:
                print(f"Fintel context unavailable: {detail}", file=sys.stderr)

        # Normalize short-form aliases to zero-padded registry keys (e.g. 10-18 → 10-018)
        if code not in CODE_REGISTRY:
            parts = code.split("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                padded = f"{parts[0]}-{parts[1].zfill(3)}"
                if padded in CODE_REGISTRY:
                    code = padded

        runner = CODE_REGISTRY.get(code)
        if runner is None:
            discovered = discover_code_scripts(code)
            if len(discovered) == 1:
                return run_script(discovered[0])

            if len(discovered) > 1:
                preview = ", ".join(str(path.relative_to(ROOT)) for path in discovered[:5])
                print(
                    f"Ambiguous scripts for {args.code}: {preview}. Add explicit mapping in CODE_REGISTRY.",
                    file=sys.stderr,
                )
                return 2

            print(f"No mapped command for {args.code}. Supported codes: {', '.join(SUPPORTED_CODES)}. To enable {args.code}, add to CODE_REGISTRY or create {args.code}*.py.", file=sys.stderr)
            return 2

        return runner(args, option_id)
    finally:
        cleanup_run_context()

if __name__ == "__main__":
    raise SystemExit(main())
