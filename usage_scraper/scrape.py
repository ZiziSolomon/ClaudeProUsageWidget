#!/usr/bin/env python3
"""
Usage scraper for claude.ai — reads Firefox cookies, calls usage API,
writes usage_data/latest.json, commits and pushes to GitHub.

Run on a schedule via Task Scheduler. No browser automation needed.

Usage: python scrape.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import browser_cookie3
from curl_cffi import requests

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
OUTPUT_FILE = REPO_ROOT / "usage_data" / "latest.json"
CALIBRATION_FILE = REPO_ROOT / "usage_data" / "calibration.jsonl"

def _load_org_id() -> str:
    env = os.environ.get("CLAUDE_ORG_ID")
    if env:
        return env
    cfg = REPO_ROOT / "config.json"
    if cfg.exists():
        return json.loads(cfg.read_text(encoding="utf-8"))["org_id"]
    raise SystemExit(
        "Claude organization ID not configured. Set CLAUDE_ORG_ID or create "
        "config.json at the repo root (see config.example.json)."
    )


USAGE_URL = f"https://claude.ai/api/organizations/{_load_org_id()}/usage"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
SESSION_DURATION_HOURS = 5

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def count_transcript_tokens(session_start: datetime, session_end: datetime) -> dict:
    """Scan local Claude Code transcripts and sum unique input+output tokens in the window."""
    seen_ids = set()
    by_model: dict[str, dict] = {}

    for jsonl_path in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        try:
            for line in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                obj = json.loads(line)
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                usage = msg.get("usage")
                msg_id = msg.get("id")
                if not usage or not msg_id or msg_id in seen_ids:
                    continue
                ts = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
                if not (session_start <= ts <= session_end):
                    continue
                seen_ids.add(msg_id)
                model = msg.get("model", "unknown")
                entry = by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0})
                entry["input"]        += usage.get("input_tokens", 0)
                entry["output"]       += usage.get("output_tokens", 0)
                entry["cache_read"]   += usage.get("cache_read_input_tokens", 0)
                entry["cache_create"] += usage.get("cache_creation_input_tokens", 0)
        except Exception as e:
            print(f"[X] count_transcript_tokens: error processing {jsonl_path.name}: {type(e).__name__}: {e}")
            continue

    total_in  = sum(m["input"]  for m in by_model.values())
    total_out = sum(m["output"] for m in by_model.values())
    return {
        "api_calls": len(seen_ids),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_tokens":   sum(m["cache_read"]   for m in by_model.values()),
        "cache_create_tokens": sum(m["cache_create"] for m in by_model.values()),
        "by_model": by_model,
    }


def scrape():
    cookies = browser_cookie3.firefox(domain_name=".claude.ai")
    cookie_dict = {c.name: c.value for c in cookies}

    response = requests.get(
        USAGE_URL,
        cookies=cookie_dict,
        impersonate="firefox",
        headers={"Referer": "https://claude.ai/"},
    )
    response.raise_for_status()
    raw = response.json()

    scraped_at = datetime.now(timezone.utc)
    seven_day = raw.get("seven_day") or {}
    five_hour = raw.get("five_hour") or {}

    session_pct      = five_hour.get("utilization")
    session_resets_at_str = five_hour.get("resets_at")

    output = {
        "scraped_at": scraped_at.isoformat(),
        "weekly_utilization_pct": seven_day.get("utilization"),
        "weekly_resets_at": seven_day.get("resets_at"),
        "session_utilization_pct": session_pct,
        "session_resets_at": session_resets_at_str,
        "raw": raw,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))

    # Calibration: correlate transcript token counts with API utilization %.
    if session_pct and session_resets_at_str:
        session_resets_at = datetime.fromisoformat(session_resets_at_str)
        session_start = session_resets_at - timedelta(hours=SESSION_DURATION_HOURS)
        tokens = count_transcript_tokens(session_start, scraped_at)

        total_io = tokens["input_tokens"] + tokens["output_tokens"]
        implied_budget = round(total_io / (session_pct / 100)) if session_pct > 0 else None

        record = {
            "scraped_at": scraped_at.isoformat(),
            "session_pct": session_pct,
            "session_start": session_start.isoformat(),
            "transcript_input_tokens": tokens["input_tokens"],
            "transcript_output_tokens": tokens["output_tokens"],
            "transcript_io_total": total_io,
            "implied_session_budget": implied_budget,
            "api_calls_in_window": tokens["api_calls"],
            "by_model": tokens["by_model"],
        }
        with CALIBRATION_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    _git_commit_and_push()


def _git_commit_and_push():
    def run(cmd):
        subprocess.run(cmd, cwd=REPO_ROOT, check=True, capture_output=True, **_NO_WINDOW)

    run(["git", "add", str(OUTPUT_FILE), str(CALIBRATION_FILE)])
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, **_NO_WINDOW
    ).returncode != 0

    if not changed:
        return

    run(["git", "commit", "-m", f"Update usage data {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"])
    run(["git", "pull", "--rebase"])
    run(["git", "push"])


if __name__ == "__main__":
    scrape()
