"""
web.py — Claude Dashboard web server
用法：cd ~/claude-dashboard && .venv/bin/python web.py
瀏覽：http://localhost:7878
"""

from __future__ import annotations

import json
import sys
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta, date as date_cls
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from session_reader import load_sessions, fmt_tokens, fmt_mem, SESSIONS_DIR
from history_reader import load_history, HistoryStats, estimate_cost, PROJECTS_DIR

HOST = "0.0.0.0"
PORT = 7878


def _total_tokens(s: dict) -> int:
    return s["input_tokens"] + s["output_tokens"] + s["cache_read"] + s["cache_write"]

# ── Background history cache (loaded once, refreshed every 60s) ───────────────
_history: HistoryStats | None = None
_history_lock = threading.Lock()


def _history_loader():
    global _history
    while True:
        h = load_history()
        with _history_lock:
            _history = h
        time.sleep(60)


threading.Thread(target=_history_loader, daemon=True).start()


# ── Session detail cache ──────────────────────────────────────────────────────
_detail_cache: dict[int, tuple[float, dict]] = {}
_detail_lock = threading.Lock()
DETAIL_TTL = 30


def _find_jsonl(project_dir: str) -> Path | None:
    key = "-" + project_dir.strip("/").replace("/", "-")
    proj_dir = Path.home() / ".claude" / "projects" / key
    try:
        jsonls = [p for p in proj_dir.glob("*.jsonl") if "subagents" not in p.parts]
        return max(jsonls, key=lambda p: p.stat().st_mtime) if jsonls else None
    except OSError:
        return None


def session_detail(pid: int) -> dict:
    now = time.time()
    with _detail_lock:
        if pid in _detail_cache:
            ts, data = _detail_cache[pid]
            if now - ts < DETAIL_TTL:
                return data

    # Read session JSON directly instead of loading all sessions
    session_file = SESSIONS_DIR / f"{pid}.json"
    try:
        data = json.loads(session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"error": "not found"}

    result = {
        "pid": pid,
        "project_dir": data.get("project_dir", ""),
        "duration_fmt": "",
        "tokens_in_fmt": fmt_tokens(int(data.get("tokens_in", 0))),
        "tokens_out_fmt": fmt_tokens(int(data.get("tokens_out", 0))),
        "cost_est": 0,
        "tools": {},
        "last_activity": "",
    }

    jsonl = _find_jsonl(result["project_dir"])
    if not jsonl:
        with _detail_lock:
            _detail_cache[pid] = (now, result)
        return result

    tools: dict[str, int] = {}
    last_activity = ""
    first_ts = ""
    total_cost = 0.0
    try:
        for line in jsonl.read_text(errors="ignore").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not first_ts and d.get("timestamp"):
                first_ts = d["timestamp"]

            if d.get("type") != "assistant":
                continue

            msg = d.get("message", {})

            # Single pass over content blocks: tools + last activity
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    name = block.get("name", "unknown")
                    tools[name] = tools.get(name, 0) + 1
                elif btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        last_activity = text[:200]

            usage = msg.get("usage")
            if usage:
                total_cost += estimate_cost(
                    msg.get("model", ""),
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("cache_read_input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                )
    except OSError:
        pass

    if first_ts:
        try:
            start = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            dur_s = int(now - start.timestamp())
            h, m = divmod(dur_s // 60, 60)
            result["duration_fmt"] = f"{h}h {m}m" if h > 0 else f"{m}m"
        except (ValueError, AttributeError):
            pass

    result["tools"] = dict(sorted(tools.items(), key=lambda x: -x[1]))
    result["last_activity"] = last_activity
    result["cost_est"] = round(total_cost, 2)

    with _detail_lock:
        # Cap cache size
        if len(_detail_cache) > 50:
            oldest = min(_detail_cache, key=lambda k: _detail_cache[k][0])
            del _detail_cache[oldest]
        _detail_cache[pid] = (now, result)
    return result


# ── API helpers ───────────────────────────────────────────────────────────────
def sessions_json() -> list[dict]:
    sessions = load_sessions(cleanup_dead=False)
    return [
        {
            "pid": s.pid,
            "name": s.display_name,
            "name_source": "session" if s.session_title else ("slug" if s.slug else "project"),
            "project": s.project_name,
            "model": s.short_model,
            "status": s.status,
            "used_pct": s.used_pct,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "tokens_out_fmt": fmt_tokens(s.tokens_out),
            "mem_kb": s.mem_kb,
            "mem_fmt": fmt_mem(s.mem_kb),
            "branch": s.git_branch,
            "alive": s.is_alive,
        }
        for s in sessions
        if s.is_alive
    ]


def history_json() -> dict:
    with _history_lock:
        h = _history
    if h is None:
        return {"loading": True}

    def _period(days: int) -> dict:
        s = h.by_period(days)
        total = _total_tokens(s)
        return {
            "cost": round(s["cost_usd"], 2),
            "input": s["input_tokens"],
            "input_fmt": fmt_tokens(s["input_tokens"]),
            "output": s["output_tokens"],
            "output_fmt": fmt_tokens(s["output_tokens"]),
            "total_tokens": total,
            "total_tokens_fmt": fmt_tokens(total),
            "requests": s["requests"],
        }

    models = []
    by_model = h.by_model(30)
    total_tokens_all = sum(_total_tokens(s) for s in by_model.values()) or 1
    for model, s in by_model.items():
        tt = _total_tokens(s)
        avg = tt // s["requests"] if s["requests"] else 0
        models.append({
            "model": model,
            "cost": round(s["cost_usd"], 2),
            "input": s["input_tokens"],
            "input_fmt": fmt_tokens(s["input_tokens"]),
            "output": s["output_tokens"],
            "output_fmt": fmt_tokens(s["output_tokens"]),
            "cache_read": s["cache_read"],
            "cache_read_fmt": fmt_tokens(s["cache_read"]),
            "cache_write": s["cache_write"],
            "cache_write_fmt": fmt_tokens(s["cache_write"]),
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "requests": s["requests"],
            "pct": round(tt / total_tokens_all * 100, 1),
            "avg_tokens": avg,
            "avg_tokens_fmt": fmt_tokens(avg),
            "input_pct": round(s["input_tokens"] / tt * 100, 1) if tt else 0,
            "output_pct": round(s["output_tokens"] / tt * 100, 1) if tt else 0,
            "cache_read_pct": round(s["cache_read"] / tt * 100, 1) if tt else 0,
            "cache_write_pct": round(s["cache_write"] / tt * 100, 1) if tt else 0,
        })

    projects = []
    for proj, s in list(h.by_project(30).items())[:10]:
        tt = _total_tokens(s)
        projects.append({
            "project": proj,
            "cost": round(s["cost_usd"], 2),
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "requests": s["requests"],
        })

    trend = []
    daily = h.daily_trend(14)
    max_tokens = max((_total_tokens(s) for _, s in daily), default=1) or 1
    for date, s in daily:
        tt = _total_tokens(s)
        trend.append({
            "date": date[5:],
            "cost": round(s["cost_usd"], 2),
            "tokens": tt,
            "tokens_fmt": fmt_tokens(tt),
            "pct": round(tt / max_tokens * 100),
        })

    sessions = []
    for s in h.by_session(30)[:15]:
        tt = _total_tokens(s)
        sessions.append({
            "title": s["title"],
            "project": s["project"],
            "cost": round(s["cost_usd"], 2),
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "input_fmt": fmt_tokens(s["input_tokens"]),
            "output_fmt": fmt_tokens(s["output_tokens"]),
            "cache_read_fmt": fmt_tokens(s["cache_read"]),
            "cache_write_fmt": fmt_tokens(s["cache_write"]),
            "requests": s["requests"],
            "dates": f"{s['date_start'][5:]} ~ {s['date_end'][5:]}" if s["date_start"] else "",
        })

    return {
        "today": _period(1),
        "week": _period(7),
        "month": _period(30),
        "models": models,
        "projects": projects,
        "trend": trend,
        "sessions": sessions,
        "blocks": _build_blocks(h),
    }


def _build_blocks(h: HistoryStats) -> list[dict]:
    blocks = []
    for b in h.billing_blocks(5, 10):
        tt = _total_tokens(b)
        start = b["start"][:16].replace("T", " ")
        end = b["end"][:16].replace("T", " ")
        blocks.append({
            "start": start,
            "end": end,
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "requests": b["requests"],
            "cost": round(b["cost_usd"], 2),
            "is_active": b["is_active"],
        })
    return blocks


def _build_blocks_range(h: HistoryStats, from_date: str, to_date: str) -> list[dict]:
    blocks = []
    for b in h.billing_blocks_range(from_date, to_date):
        tt = _total_tokens(b)
        start = b["start"][:16].replace("T", " ")
        end = b["end"][:16].replace("T", " ")
        blocks.append({
            "start": start,
            "end": end,
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "requests": b["requests"],
            "cost": round(b["cost_usd"], 2),
            "is_active": b["is_active"],
        })
    return blocks


def history_json_range(from_date: str, to_date: str) -> dict:
    """Return history data filtered to [from_date, to_date] (YYYY-MM-DD)."""
    with _history_lock:
        h = _history
    if h is None:
        return {"loading": True}

    def _fmt_sum(s: dict) -> dict:
        total = _total_tokens(s)
        return {
            "cost": round(s["cost_usd"], 2),
            "input": s["input_tokens"],
            "input_fmt": fmt_tokens(s["input_tokens"]),
            "output": s["output_tokens"],
            "output_fmt": fmt_tokens(s["output_tokens"]),
            "total_tokens": total,
            "total_tokens_fmt": fmt_tokens(total),
            "requests": s["requests"],
        }

    range_sum = _fmt_sum(h.by_range(from_date, to_date))

    models = []
    by_model = h.by_model_range(from_date, to_date)
    total_tokens_all = sum(_total_tokens(s) for s in by_model.values()) or 1
    for model, s in by_model.items():
        tt = _total_tokens(s)
        avg = tt // s["requests"] if s["requests"] else 0
        models.append({
            "model": model,
            "cost": round(s["cost_usd"], 2),
            "input": s["input_tokens"],
            "input_fmt": fmt_tokens(s["input_tokens"]),
            "output": s["output_tokens"],
            "output_fmt": fmt_tokens(s["output_tokens"]),
            "cache_read": s["cache_read"],
            "cache_read_fmt": fmt_tokens(s["cache_read"]),
            "cache_write": s["cache_write"],
            "cache_write_fmt": fmt_tokens(s["cache_write"]),
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "requests": s["requests"],
            "pct": round(tt / total_tokens_all * 100, 1),
            "avg_tokens": avg,
            "avg_tokens_fmt": fmt_tokens(avg),
            "input_pct": round(s["input_tokens"] / tt * 100, 1) if tt else 0,
            "output_pct": round(s["output_tokens"] / tt * 100, 1) if tt else 0,
            "cache_read_pct": round(s["cache_read"] / tt * 100, 1) if tt else 0,
            "cache_write_pct": round(s["cache_write"] / tt * 100, 1) if tt else 0,
        })

    projects = []
    for proj, s in list(h.by_project_range(from_date, to_date).items())[:10]:
        tt = _total_tokens(s)
        projects.append({
            "project": proj,
            "cost": round(s["cost_usd"], 2),
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "requests": s["requests"],
        })

    trend = []
    daily = h.daily_trend_range(from_date, to_date)
    max_tokens = max((_total_tokens(s) for _, s in daily), default=1) or 1
    for date_str, s in daily:
        tt = _total_tokens(s)
        trend.append({
            "date": date_str[5:],
            "cost": round(s["cost_usd"], 2),
            "tokens": tt,
            "tokens_fmt": fmt_tokens(tt),
            "pct": round(tt / max_tokens * 100),
        })

    sessions = []
    for s in h.by_session_range(from_date, to_date)[:15]:
        tt = _total_tokens(s)
        sessions.append({
            "title": s["title"],
            "project": s["project"],
            "cost": round(s["cost_usd"], 2),
            "total_tokens": tt,
            "total_tokens_fmt": fmt_tokens(tt),
            "input_fmt": fmt_tokens(s["input_tokens"]),
            "output_fmt": fmt_tokens(s["output_tokens"]),
            "cache_read_fmt": fmt_tokens(s["cache_read"]),
            "cache_write_fmt": fmt_tokens(s["cache_write"]),
            "requests": s["requests"],
            "dates": f"{s['date_start'][5:]} ~ {s['date_end'][5:]}" if s["date_start"] else "",
        })

    return {
        "range": range_sum,
        "from": from_date,
        "to": to_date,
        "models": models,
        "projects": projects,
        "trend": trend,
        "sessions": sessions,
        "blocks": _build_blocks_range(h, from_date, to_date),
    }


# ── Timeline API ─────────────────────────────────────────────────────────────
_timeline_cache: tuple[float, list] = (0.0, [])
_timeline_lock = threading.Lock()
TIMELINE_TTL = 30
ACTIVITY_GAP_SEC = 300  # 5 minutes — gap longer than this = idle


def _scan_jsonl_timestamps(jsonl_path: Path, since_ts: float) -> list[float]:
    """Extract assistant-message timestamps from a JSONL file, after since_ts."""
    timestamps: list[float] = []
    try:
        for line in jsonl_path.read_text(errors="ignore").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "assistant":
                continue
            ts = d.get("timestamp", "")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                epoch = dt.timestamp()
                if epoch >= since_ts:
                    timestamps.append(epoch)
            except (ValueError, AttributeError):
                continue
    except OSError:
        pass
    return timestamps


def _timestamps_to_segments(timestamps: list[float], window_start: float, window_end: float) -> list[dict]:
    """Convert sorted timestamps into active/idle segments."""
    if not timestamps:
        return [{"start": window_start, "end": window_end, "type": "idle"}]

    timestamps.sort()
    segments: list[dict] = []

    # Add idle gap before first activity
    if timestamps[0] - window_start > ACTIVITY_GAP_SEC:
        segments.append({"start": window_start, "end": timestamps[0], "type": "idle"})

    # Group timestamps into active windows
    seg_start = timestamps[0]
    seg_end = timestamps[0]

    for ts in timestamps[1:]:
        if ts - seg_end > ACTIVITY_GAP_SEC:
            # Close current active segment
            segments.append({"start": seg_start, "end": seg_end + 60, "type": "active"})
            # Add idle gap
            segments.append({"start": seg_end + 60, "end": ts, "type": "idle"})
            seg_start = ts
            seg_end = ts
        else:
            seg_end = ts

    # Close last active segment
    segments.append({"start": seg_start, "end": min(seg_end + 60, window_end), "type": "active"})

    # Add idle gap after last activity
    if seg_end + 60 < window_end:
        segments.append({"start": seg_end + 60, "end": window_end, "type": "idle"})

    return segments


def timeline_json() -> list[dict]:
    """Return activity timeline for each active session over the last 24h."""
    global _timeline_cache
    now = time.time()
    with _timeline_lock:
        cached_ts, cached_data = _timeline_cache
        if now - cached_ts < TIMELINE_TTL:
            return cached_data

    sessions = load_sessions(cleanup_dead=False)
    alive = [s for s in sessions if s.is_alive]

    window_end = now
    window_start = now - 86400  # 24 hours ago

    result: list[dict] = []
    for s in alive:
        # Find the JSONL for this session's project
        jsonl = _find_jsonl(s.project_dir)
        if not jsonl:
            result.append({
                "pid": s.pid,
                "name": s.display_name,
                "status": s.status,
                "segments": [{"start": window_start, "end": window_end, "type": "idle"}],
            })
            continue

        timestamps = _scan_jsonl_timestamps(jsonl, window_start)
        segments = _timestamps_to_segments(timestamps, window_start, window_end)

        result.append({
            "pid": s.pid,
            "name": s.display_name,
            "status": s.status,
            "segments": [
                {"start": round(seg["start"], 1), "end": round(seg["end"], 1), "type": seg["type"]}
                for seg in segments
            ],
        })

    with _timeline_lock:
        _timeline_cache = (now, result)

    return result


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0f1117; --surface: #1a1d2e; --surface2: #232738; --border: #2d3148;
  --text: #e2e8f0; --dim: #64748b; --accent: #a78bfa;
  --green: #34d399; --yellow: #fbbf24; --red: #f87171;
  --cyan: #22d3ee; --blue: #60a5fa;
  --shadow: 0 2px 8px rgba(0,0,0,.3);
  --glow: 0 0 20px rgba(167,139,250,.08);
}
[data-theme="light"] {
  --bg: #f1f5f9; --surface: #ffffff; --surface2: #f8fafc; --border: #e2e8f0;
  --text: #1e293b; --dim: #94a3b8; --accent: #7c3aed;
  --green: #059669; --yellow: #d97706; --red: #dc2626;
  --cyan: #0891b2; --blue: #2563eb;
  --shadow: 0 2px 8px rgba(0,0,0,.06);
  --glow: none;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif; font-size: 14px; min-height: 100vh; }

/* Header */
.header { padding: 18px 28px; background: linear-gradient(135deg, var(--surface) 0%, var(--surface2) 100%); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 16px; box-shadow: var(--shadow); }
.header h1 { background: linear-gradient(135deg, var(--accent), var(--cyan)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-size: 20px; font-weight: 700; letter-spacing: -.5px; }
.header .time { color: var(--dim); font-size: 13px; font-family: 'JetBrains Mono', monospace; }
.header-right { margin-left: auto; display: flex; gap: 8px; }
.theme-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--dim); padding: 6px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; font-family: inherit; transition: all .2s; }
.theme-btn:hover { color: var(--accent); border-color: var(--accent); transform: translateY(-1px); }

/* Tabs */
.tabs { display: flex; gap: 0; padding: 0 28px; background: var(--surface); border-bottom: 1px solid var(--border); }
.tab { padding: 12px 24px; cursor: pointer; color: var(--dim); border-bottom: 2px solid transparent; transition: all .2s; font-weight: 500; font-size: 13px; letter-spacing: .3px; }
.tab:hover { color: var(--text); background: var(--surface2); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.content { padding: 24px 28px; max-width: 1280px; margin: 0 auto; }

/* Sessions */
.session { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 20px; margin-bottom: 12px; transition: all .2s; box-shadow: var(--shadow); }
.session:hover { border-color: var(--accent); box-shadow: var(--glow), var(--shadow); transform: translateY(-1px); }
.session .row1 { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; }
.session .pid { color: var(--cyan); font-size: 11px; font-family: 'JetBrains Mono', monospace; min-width: 70px; opacity: .8; }
.session .name { font-weight: 600; flex: 1; font-size: 15px; }
.session { cursor: pointer; }
.session .model { color: var(--dim); font-size: 12px; }
.session .expand-icon { color: var(--dim); font-size: 16px; transition: transform .2s; margin-left: 4px; }
.session.open .expand-icon { transform: rotate(90deg); }
.session-detail { display: none; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); font-size: 12px; animation: fadeIn .2s; }
.session.open .session-detail { display: block; }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.detail-section h4 { color: var(--accent); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
.detail-section .drow { display: flex; justify-content: space-between; padding: 3px 0; font-size: 12px; }
.detail-section .drow .dlabel { color: var(--dim); }
.detail-section .drow .dval { color: var(--cyan); font-family: 'JetBrains Mono', monospace; }
.tool-list { display: flex; flex-wrap: wrap; gap: 6px; }
.tool-tag { background: var(--surface2); border: 1px solid var(--border); padding: 2px 8px; border-radius: 4px; font-size: 11px; color: var(--dim); }
.tool-tag .tool-count { color: var(--cyan); margin-left: 4px; }
.last-activity { color: var(--dim); font-size: 12px; line-height: 1.5; margin-top: 8px; padding: 8px 10px; background: var(--surface2); border-radius: 6px; white-space: pre-wrap; word-break: break-word; max-height: 120px; overflow-y: auto; }
.session .info { display: grid; grid-template-columns: 100px 80px 90px 80px; gap: 8px; font-size: 12px; color: var(--dim); }
.session .info span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session .status { font-weight: 700; font-size: 11px; padding: 3px 10px; border-radius: 6px; text-transform: uppercase; letter-spacing: .5px; }
.status-working { color: #000; background: var(--yellow); animation: pulse 2s ease-in-out infinite; }
.status-idle { color: #000; background: var(--green); }
.status-waiting { color: var(--text); background: var(--border); }
.status-queued { color: #fff; background: var(--accent); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .7; } }
.session .row2 { display: flex; align-items: center; gap: 14px; font-size: 13px; }
.bar-bg { width: 180px; height: 6px; background: var(--surface2); border-radius: 3px; overflow: hidden; border: 1px solid var(--border); }
.bar-fill { height: 100%; border-radius: 3px; transition: width .5s ease; }
.bar-ok { background: linear-gradient(90deg, var(--green), var(--cyan)); }
.bar-warn { background: linear-gradient(90deg, var(--yellow), #f59e0b); }
.bar-danger { background: linear-gradient(90deg, var(--red), #ef4444); }
.session .pct { min-width: 38px; text-align: right; font-weight: 600; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
.session .meta { color: var(--dim); font-size: 12px; }
.session .branch { color: var(--blue); font-size: 12px; font-family: 'JetBrains Mono', monospace; }

.summary { display: flex; gap: 28px; padding: 16px 4px; color: var(--dim); font-size: 13px; border-top: 1px solid var(--border); margin-top: 8px; }
.summary b { color: var(--text); font-weight: 500; }
.summary span { color: var(--cyan); font-weight: 600; }

/* Big numbers */
.hero-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
.hero { background: linear-gradient(135deg, var(--surface) 0%, var(--surface2) 100%); border: 1px solid var(--border); border-radius: 12px; padding: 20px; text-align: center; box-shadow: var(--shadow); }
.hero .label { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 6px; }
.hero .num { font-size: 28px; font-weight: 700; letter-spacing: -1px; }
.hero .sub { color: var(--dim); font-size: 12px; margin-top: 4px; }

/* Stats */
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin-bottom: 20px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; box-shadow: var(--shadow); transition: all .2s; }
.card:hover { border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
.card h3 { color: var(--accent); font-size: 11px; margin-bottom: 14px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 600; }
.card .row { display: grid; grid-template-columns: 1fr auto auto; gap: 12px; align-items: center; padding: 8px 0; font-size: 13px; border-bottom: 1px solid var(--border); }
.card .row:last-child { border-bottom: none; }
.card .label { color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card .cost { color: var(--yellow); font-weight: 700; font-family: 'JetBrains Mono', monospace; text-align: right; }
.card .val { color: var(--cyan); font-family: 'JetBrains Mono', monospace; text-align: right; }
.card .meta { color: var(--dim); font-size: 11px; text-align: right; }
.card .row-bar { grid-template-columns: 100px 1fr 70px; }
.card .row-bar .bar-bg { width: 100%; }
.card .row-session { grid-template-columns: 1fr 90px 100px 70px 70px 60px; }
.card .row-block { grid-template-columns: 120px 15px 120px 70px auto auto; }

/* Tooltip */
.tip { position: relative; cursor: help; border-bottom: 1px dotted var(--dim); }
.tip .tiptext {
  visibility: hidden; opacity: 0; position: absolute; bottom: 130%; left: 50%; transform: translateX(-50%);
  background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px;
  font-size: 12px; white-space: nowrap; z-index: 10; box-shadow: var(--shadow);
  transition: opacity .15s;
}
.tip:hover .tiptext { visibility: visible; opacity: 1; }
.tiptext .trow { display: grid; grid-template-columns: 90px 60px 1fr; gap: 4px; padding: 2px 0; align-items: baseline; }
.tiptext .trow .tlabel { color: var(--dim); }
.tiptext .trow .tval { color: var(--cyan); font-family: 'JetBrains Mono', monospace; text-align: right; }
.tiptext .trow .trate { color: var(--dim); font-size: 11px; }
.tiptext .tsep { border-top: 1px solid var(--border); margin: 4px 0; }

/* Stacked bar */
.sbar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; margin: 8px 0; }
.sbar > div { height: 100%; transition: width .3s; }
.sbar-legend { display: flex; flex-wrap: wrap; gap: 8px 16px; font-size: 11px; margin-top: 6px; }
.sbar-legend span { display: flex; align-items: center; gap: 4px; }
.sbar-legend .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

.chart-wrap { position: relative; height: 220px; }
.chart-wrap-sm { position: relative; height: 200px; }

.empty { color: var(--dim); text-align: center; padding: 80px 20px; font-size: 15px; }
.pct-ok { color: var(--green); } .pct-warn { color: var(--yellow); } .pct-danger { color: var(--red); font-weight: 700; }

/* Context alert glow */
.session.ctx-alert { border-color: var(--red); box-shadow: 0 0 12px rgba(248,113,113,.45), 0 0 24px rgba(248,113,113,.2); animation: ctxPulse 2s ease-in-out infinite; }
@keyframes ctxPulse { 0%,100% { box-shadow: 0 0 12px rgba(248,113,113,.45), 0 0 24px rgba(248,113,113,.2); } 50% { box-shadow: 0 0 18px rgba(248,113,113,.6), 0 0 36px rgba(248,113,113,.3); } }
[data-theme="light"] .session.ctx-alert { box-shadow: 0 0 12px rgba(220,38,38,.3), 0 0 24px rgba(220,38,38,.15); }

/* Notification bell */
.notify-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--dim); width: 36px; height: 36px; border-radius: 8px; cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; transition: all .2s; position: relative; }
.notify-btn:hover { color: var(--accent); border-color: var(--accent); transform: translateY(-1px); }
.notify-btn.granted { color: var(--green); }
.notify-btn.denied { color: var(--red); opacity: .6; }

/* Timeline */
.timeline-container { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; box-shadow: var(--shadow); }
.timeline-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.timeline-header h3 { color: var(--accent); font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 600; margin: 0; }
.timeline-hours { position: relative; height: 24px; margin-left: 160px; margin-bottom: 4px; border-bottom: 1px solid var(--border); }
.timeline-hour-mark { position: absolute; top: 0; font-size: 10px; color: var(--dim); font-family: 'JetBrains Mono', monospace; transform: translateX(-50%); }
.timeline-row { display: flex; align-items: center; margin-bottom: 6px; height: 32px; }
.timeline-label { width: 150px; min-width: 150px; padding-right: 10px; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.timeline-label .tl-name { font-weight: 500; }
.timeline-label .tl-status { font-size: 10px; margin-left: 6px; }
.timeline-bar-wrap { flex: 1; height: 20px; position: relative; background: var(--surface2); border-radius: 4px; overflow: hidden; border: 1px solid var(--border); }
.timeline-seg { position: absolute; top: 0; height: 100%; }
.timeline-seg-active { background: var(--yellow); opacity: 0.85; border-radius: 2px; }
.timeline-seg-idle { background: transparent; }
.timeline-now { position: absolute; top: 0; height: 100%; width: 2px; background: var(--red); z-index: 2; }
.timeline-now::after { content: ''; position: absolute; top: -4px; left: -3px; width: 8px; height: 8px; background: var(--red); border-radius: 50%; }
.timeline-legend { display: flex; gap: 16px; margin-top: 14px; font-size: 11px; color: var(--dim); }
.timeline-legend span { display: flex; align-items: center; gap: 5px; }
.timeline-legend .leg-box { width: 14px; height: 10px; border-radius: 2px; display: inline-block; }

/* Date range picker */
.date-range { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; padding: 14px 18px; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow); }
.date-range label { color: var(--dim); font-size: 12px; font-weight: 500; letter-spacing: .3px; }
.date-range input[type="date"] { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 5px 10px; border-radius: 6px; font-size: 12px; font-family: 'JetBrains Mono', monospace; outline: none; transition: border-color .2s; }
.date-range input[type="date"]:focus { border-color: var(--accent); }
.date-range input[type="date"]::-webkit-calendar-picker-indicator { filter: invert(.6); }
[data-theme="light"] .date-range input[type="date"]::-webkit-calendar-picker-indicator { filter: none; }
.date-presets { display: flex; gap: 4px; margin-left: 8px; }
.date-preset { background: var(--surface2); border: 1px solid var(--border); color: var(--dim); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 11px; font-family: inherit; font-weight: 500; transition: all .2s; }
.date-preset:hover { color: var(--accent); border-color: var(--accent); }
.date-preset.active { color: var(--accent); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, var(--surface2)); }

@media (max-width: 768px) {
  .hero-grid { grid-template-columns: 1fr; }
  .grid2 { grid-template-columns: 1fr; }
  .header { padding: 14px 16px; }
  .content { padding: 16px; }
  .tabs { padding: 0 16px; }
  .tab { padding: 10px 14px; font-size: 12px; }
  .bar-bg { width: 120px; }
  .timeline-label { width: 100px; min-width: 100px; font-size: 11px; }
  .timeline-hours { margin-left: 110px; }
  .date-range { padding: 10px 12px; gap: 6px; }
  .date-presets { margin-left: 0; }
}
</style>
</head>
<body>

<div class="header">
  <h1>Claude Dashboard</h1>
  <span class="time" id="clock"></span>
  <div class="header-right">
    <button class="notify-btn" id="notifyToggle" title="">&#128276;</button>
    <button class="theme-btn" id="langToggle"></button>
    <button class="theme-btn" id="themeToggle"></button>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-tab="sessions">Sessions</div>
  <div class="tab" data-tab="timeline">Timeline</div>
  <div class="tab" data-tab="usage">Usage</div>
  <div class="tab" data-tab="models">Models</div>
</div>

<div class="content" id="content"></div>

<script>
// ── i18n ──────────────────────────────────────────
const I18N = {
  en: {
    sessions: 'Sessions', usage: 'Usage', models: 'Models',
    today: 'Today', thisWeek: 'This Week', thisMonth: 'This Month',
    incCache: '(incl. cache)', output: 'output', req: 'req',
    instances: 'Instances', mem: 'Mem',
    dailyTokens: 'Daily Tokens (14d)', byProject: 'By Project (30d)', bySession: 'By Session (30d)',
    totalTokens: 'Total Tokens', estCost: 'Est. Cost', requests: 'Requests', share: 'Share',
    totalTokens30: 'Total Tokens (30d)', outputTokens30: 'Output Tokens (30d)', estCost30: 'Est. Cost (30d)',
    tokenDist: 'Token Distribution', modelDetails: 'Model Details', modelsUsed: 'models used',
    noSessions: 'No active Claude Code sessions.', loading: 'Loading...',
    d7: '7 days', d30: '30 days',
    input: 'Input', cacheRead: 'Cache Read', cacheWrite: 'Cache Write',
    rate: 'rate', total: 'Total',
    tipOutput: "Claude's response", tipInput: 'New input (1x)', tipCacheRead: 'Cached context (0.1x)', tipCacheWrite: 'Write to cache (1.25x)',
    hdrSession: 'Session', hdrProject: 'Project', hdrDate: 'Date',
    avgPerReq: 'Avg / request', tokenBreakdown: 'Token Breakdown', perReq: '/ req',
    tipModel: 'AI model used', tipBranch: 'Git branch', tipPid: 'Process ID', tipMem: 'Memory usage (RAM)', tipOutputTokens: 'Output tokens — response from Claude', tipCtx: 'Context window usage',
    nameSession: 'Session Name', nameSlug: 'Slug', nameProject: 'Project Name',
    dataSource: 'Data from local JSONL transcripts (~/.claude/projects/)',
    billingBlocks: '5h Billing Blocks', active: 'ACTIVE',
    notifyPermission: 'Enable desktop notifications',
    notifyGranted: 'Notifications enabled',
    notifyDenied: 'Notifications blocked by browser',
    ctxAlert: 'Context window alert',
    ctxAlertBody: '{name} has reached {pct}% context usage',
    timeline: 'Timeline', timelineTitle: 'Session Activity (24h)',
    tlActive: 'Active', tlIdle: 'Idle', tlNow: 'Now',
    tlNoSessions: 'No active sessions to display.',
    dateFrom: 'From', dateTo: 'To',
    presetToday: 'Today', preset7d: '7d', preset30d: '30d', preset90d: '90d', presetAll: 'All',
    dateRange: 'Date Range', dailyTokensRange: 'Daily Tokens', byProjectRange: 'By Project', bySessionRange: 'By Session',
    selectedRange: 'Selected Range',
  },
  'zh-TW': {
    sessions: '工作階段', usage: '用量', models: '模型',
    today: '今天', thisWeek: '本週', thisMonth: '本月',
    incCache: '(含 cache)', output: '輸出', req: '次請求',
    instances: '實例', mem: '記憶體',
    dailyTokens: '每日 Token (14天)', byProject: '依專案 (30天)', bySession: '依工作階段 (30天)',
    totalTokens: '總 Token', estCost: '估算費用', requests: '請求數', share: '佔比',
    totalTokens30: '總 Token (30天)', outputTokens30: '輸出 Token (30天)', estCost30: '估算費用 (30天)',
    tokenDist: 'Token 分佈', modelDetails: '模型詳情', modelsUsed: '個模型',
    noSessions: '沒有進行中的 Claude Code 工作階段。', loading: '載入中...',
    d7: '7 天', d30: '30 天',
    input: '輸入', cacheRead: '快取讀取', cacheWrite: '快取寫入',
    rate: '費率', total: '合計',
    tipOutput: 'Claude 的回應', tipInput: '新增的輸入 (1x)', tipCacheRead: '重複讀取快取 (0.1x)', tipCacheWrite: '寫入快取 (1.25x)',
    hdrSession: '工作階段', hdrProject: '專案', hdrDate: '日期',
    avgPerReq: '平均每次請求', tokenBreakdown: 'Token 組成', perReq: '/ 次',
    tipModel: '使用的 AI 模型', tipBranch: 'Git 分支', tipPid: '程序 ID', tipMem: '記憶體用量 (RAM)', tipOutputTokens: 'Output tokens — Claude 的回應量', tipCtx: 'Context window 使用率',
    nameSession: '工作階段名稱', nameSlug: '自動名稱 (Slug)', nameProject: '專案名稱',
    dataSource: '資料來自本機 JSONL 對話紀錄 (~/.claude/projects/)',
    billingBlocks: '5 小時計費區間', active: '進行中',
    notifyPermission: '啟用桌面通知',
    notifyGranted: '通知已啟用',
    notifyDenied: '瀏覽器已封鎖通知',
    ctxAlert: 'Context window 警報',
    ctxAlertBody: '{name} 已達 {pct}% context 使用率',
    timeline: '時間軸', timelineTitle: '工作階段活動 (24小時)',
    tlActive: '活躍', tlIdle: '閒置', tlNow: '現在',
    tlNoSessions: '沒有可顯示的活躍工作階段。',
    dateFrom: '從', dateTo: '到',
    presetToday: '今天', preset7d: '7天', preset30d: '30天', preset90d: '90天', presetAll: '全部',
    dateRange: '日期範圍', dailyTokensRange: '每日 Token', byProjectRange: '依專案', bySessionRange: '依工作階段',
    selectedRange: '選取範圍',
  }
};
let lang = localStorage.getItem('lang') || 'en';
function t(key) { return (I18N[lang] || I18N.en)[key] || I18N.en[key] || key; }

const langBtn = document.getElementById('langToggle');
function updateLangBtn() { langBtn.textContent = lang === 'en' ? '中文' : 'EN'; }
updateLangBtn();
langBtn.onclick = () => {
  lang = lang === 'en' ? 'zh-TW' : 'en';
  localStorage.setItem('lang', lang);
  updateLangBtn();
  updateNotifyBtn();
  updateTabs();
  render();
};

function updateTabs() {
  const keys = ['sessions', 'timeline', 'usage', 'models'];
  document.querySelectorAll('.tab').forEach((el, i) => { el.textContent = t(keys[i]); });
}
updateTabs();

// Theme
const saved = localStorage.getItem('theme') || 'dark';
document.documentElement.setAttribute('data-theme', saved);
const themeBtn = document.getElementById('themeToggle');
function updateThemeBtn() { themeBtn.textContent = document.documentElement.getAttribute('data-theme') === 'dark' ? '\u2600 Light' : '\u263E Dark'; }
updateThemeBtn();
themeBtn.onclick = () => {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  updateThemeBtn();
  if (currentTab !== 'sessions') render(); // re-render charts with new colors
};

function cs() { return getComputedStyle(document.documentElement); }

const VALID_TABS = ['sessions', 'timeline', 'usage', 'models'];
let currentTab = VALID_TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'sessions';
let sessionsData = [], historyData = null, rangeHistoryData = null;
let costChart = null, modelChart = null;

// ── Date range state ──────────────────────────────
function fmtDate(d) { return d.toISOString().slice(0, 10); }
const _today = new Date();
const _30ago = new Date(_today); _30ago.setDate(_30ago.getDate() - 30);
let dateFrom = fmtDate(_30ago);
let dateTo = fmtDate(_today);
let activePreset = '30d';

async function fetchRangeHistory() {
  try {
    rangeHistoryData = await (await fetch('/api/history?from=' + dateFrom + '&to=' + dateTo)).json();
  } catch {}
}

function setDateRange(from, to, preset) {
  dateFrom = from;
  dateTo = to;
  activePreset = preset || '';
  fetchRangeHistory().then(() => {
    if (currentTab === 'usage' || currentTab === 'models') render();
  });
}

function applyPreset(key) {
  const today = new Date();
  let from;
  if (key === 'today') { from = new Date(today); }
  else if (key === '7d') { from = new Date(today); from.setDate(from.getDate() - 7); }
  else if (key === '30d') { from = new Date(today); from.setDate(from.getDate() - 30); }
  else if (key === '90d') { from = new Date(today); from.setDate(from.getDate() - 90); }
  else if (key === 'all') { from = new Date('2024-01-01'); }
  else return;
  setDateRange(fmtDate(from), fmtDate(today), key);
}

function switchTab(tab) {
  currentTab = tab;
  location.hash = tab;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
  render();
}

document.querySelectorAll('.tab').forEach(t => { t.onclick = () => switchTab(t.dataset.tab); });
window.addEventListener('hashchange', () => {
  const h = location.hash.slice(1);
  if (VALID_TABS.includes(h) && h !== currentTab) switchTab(h);
});

// Set initial active tab from hash
document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === currentTab));

function updateClock() { document.getElementById('clock').textContent = new Date().toLocaleString('sv-SE'); }
setInterval(updateClock, 1000); updateClock();

// ── Notifications ─────────────────────────────────
const CTX_THRESHOLD = 80;
const notifiedPids = new Set();
const notifyBtn = document.getElementById('notifyToggle');

function updateNotifyBtn() {
  if (!('Notification' in window)) { notifyBtn.style.display = 'none'; return; }
  const perm = Notification.permission;
  notifyBtn.classList.toggle('granted', perm === 'granted');
  notifyBtn.classList.toggle('denied', perm === 'denied');
  notifyBtn.title = perm === 'granted' ? t('notifyGranted') : perm === 'denied' ? t('notifyDenied') : t('notifyPermission');
}
updateNotifyBtn();

notifyBtn.onclick = async () => {
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default') {
    await Notification.requestPermission();
  }
  updateNotifyBtn();
};

function checkCtxAlerts(sessions) {
  // Apply/remove glow class on session cards
  for (const s of sessions) {
    const card = document.getElementById('session-' + s.pid);
    if (card) card.classList.toggle('ctx-alert', s.used_pct >= CTX_THRESHOLD);
  }

  // Send browser notifications for newly crossed thresholds
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  for (const s of sessions) {
    if (s.used_pct >= CTX_THRESHOLD && !notifiedPids.has(s.pid)) {
      notifiedPids.add(s.pid);
      const body = t('ctxAlertBody').replace('{name}', s.name).replace('{pct}', s.used_pct);
      new Notification(t('ctxAlert'), { body, tag: 'ctx-' + s.pid });
    }
    // Reset if drops below threshold so it can re-notify on next crossing
    if (s.used_pct < CTX_THRESHOLD) notifiedPids.delete(s.pid);
  }

  // Clean up PIDs for sessions that no longer exist
  const activePids = new Set(sessions.map(s => s.pid));
  for (const pid of notifiedPids) {
    if (!activePids.has(pid)) notifiedPids.delete(pid);
  }
}

async function fetchSessions() { try { sessionsData = await (await fetch('/api/sessions')).json(); } catch {} }
async function fetchHistory() {
  try { historyData = await (await fetch('/api/history')).json(); } catch {}
  try { rangeHistoryData = await (await fetch('/api/history?from=' + dateFrom + '&to=' + dateTo)).json(); } catch {}
}

function barClass(p) { return p < 60 ? 'bar-ok' : p < 85 ? 'bar-warn' : 'bar-danger'; }
function pctClass(p) { return p < 60 ? 'pct-ok' : p < 85 ? 'pct-warn' : 'pct-danger'; }
function statusClass(s) {
  s = s.toLowerCase();
  if (['working','thinking','responding','streaming'].includes(s)) return 'status-working';
  if (['idle','done','waiting_for_input'].includes(s)) return 'status-idle';
  if (s === 'waiting') return 'status-waiting';
  if (s === 'queued') return 'status-queued';
  return 'status-idle';
}
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function mdLight(s) {
  // Minimal markdown: **bold**, `code`, escape HTML first
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code style="background:var(--surface2);padding:1px 5px;border-radius:3px;font-size:12px">$1</code>');
}
function fmtK(n) { return n >= 1e6 ? (n/1e6).toFixed(1)+'M' : n >= 1e3 ? (n/1e3).toFixed(1)+'k' : ''+n; }
function fmtMem(kb) { return kb >= 1048576 ? (kb/1048576).toFixed(1)+'G' : kb >= 1024 ? (kb/1024).toFixed(0)+'M' : kb+'K'; }

// ── Sessions ─────────────────────────────────────
// SVG icons as constants to keep templates readable
const IC_BRANCH = '<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor" style="vertical-align:-1px;margin-right:2px"><path d="M9.5 3.25a2.25 2.25 0 1 1 3 2.122V6A2.5 2.5 0 0 1 10 8.5H6a1 1 0 0 0-1 1v1.128a2.251 2.251 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.5 0v1.836A2.5 2.5 0 0 1 6 7h4a1 1 0 0 0 1-1v-.628A2.25 2.25 0 0 1 9.5 3.25Z"/></svg>';
const IC_PID = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:2px"><circle cx="12" cy="12" r="3"/><path d="M12 1v4m0 14v4m-8.66-3.34 2.83-2.83m11.66-5.66 2.83-2.83M1 12h4m14 0h4M4.34 4.34l2.83 2.83m11.66 5.66 2.83 2.83"/></svg>';
const IC_MEM = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:2px"><rect x="2" y="6" width="20" height="12" rx="2"/><line x1="6" y1="6" x2="6" y2="2"/><line x1="10" y1="6" x2="10" y2="2"/><line x1="14" y1="6" x2="14" y2="2"/><line x1="18" y1="6" x2="18" y2="2"/></svg>';
const IC_OUT = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:2px"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>';

let renderedPids = [];  // track which PIDs are rendered

function sessionCardHtml(s) {
  return `<div class="session" id="session-${s.pid}" onclick="toggleSession(${s.pid})">
    <div class="row1">
      <span class="expand-icon">\u25B8</span>
      <span class="name" data-f="name" title="${{session:t('nameSession'),slug:t('nameSlug'),project:t('nameProject')}[s.name_source]}" style="cursor:help">${esc(s.name)}</span>
      ${s.name !== s.project ? `<span class="meta">${esc(s.project)}</span>` : ''}
      <span class="status ${statusClass(s.status)}" data-f="status">${s.status.toUpperCase()}</span>
    </div>
    <div class="info">
      <span title="${t('tipModel')}" style="cursor:help" data-f="model">${esc(s.model)}</span>
      <span title="${t('tipBranch')}" style="cursor:help">${s.branch ? IC_BRANCH + esc(s.branch) : ''}</span>
      <span title="${t('tipPid')}" style="cursor:help;font-family:'JetBrains Mono',monospace">${IC_PID}${s.pid}</span>
      <span title="${t('tipMem')}" style="cursor:help" data-f="mem">${IC_MEM}${s.mem_fmt}</span>
    </div>
    <div class="row2" style="margin-top:6px">
      <div class="bar-bg" title="${t('tipCtx')}" style="cursor:help"><div class="bar-fill ${barClass(s.used_pct)}" data-f="bar" style="width:${s.used_pct}%"></div></div>
      <span class="pct ${pctClass(s.used_pct)}" title="${t('tipCtx')}" style="cursor:help" data-f="pct">${s.used_pct}%</span>
      <span class="meta" style="margin-left:8px;cursor:help" title="${t('tipOutputTokens')}" data-f="out">${IC_OUT}${s.tokens_out_fmt}</span>
    </div>
    <div class="session-detail" id="detail-${s.pid}"></div>
  </div>`;
}

function renderSessions() {
  if (!sessionsData.length) return `<div class="empty">${t('noSessions')}</div>`;
  let html = '';
  let totIn = 0, totOut = 0, totMem = 0;
  for (const s of sessionsData) {
    totIn += s.tokens_in; totOut += s.tokens_out; totMem += s.mem_kb;
    html += sessionCardHtml(s);
  }
  html += `<div id="session-summary" class="summary"><div><b>${t('instances')}:</b> <span>${sessionsData.length}</span></div><div><b>${t('output')}:</b> <span>${fmtK(totOut)}</span></div><div><b>${t('mem')}:</b> <span>${fmtMem(totMem)}</span></div></div>`;
  renderedPids = sessionsData.map(s => s.pid);
  return html;
}

function patchSessions() {
  // Check if PID set changed → full re-render needed
  const newPids = sessionsData.map(s => s.pid);
  if (JSON.stringify(newPids) !== JSON.stringify(renderedPids)) return false;

  // Patch each card in-place
  let totIn = 0, totOut = 0, totMem = 0;
  for (const s of sessionsData) {
    totIn += s.tokens_in; totOut += s.tokens_out; totMem += s.mem_kb;
    const card = document.getElementById('session-' + s.pid);
    if (!card) return false;

    // Status
    const statusEl = card.querySelector('[data-f="status"]');
    if (statusEl) { statusEl.className = 'status ' + statusClass(s.status); statusEl.textContent = s.status.toUpperCase(); }

    // Mem
    const memEl = card.querySelector('[data-f="mem"]');
    if (memEl) memEl.innerHTML = IC_MEM + s.mem_fmt;

    // Bar
    const barEl = card.querySelector('[data-f="bar"]');
    if (barEl) { barEl.style.width = s.used_pct + '%'; barEl.className = 'bar-fill ' + barClass(s.used_pct); }

    // Pct
    const pctEl = card.querySelector('[data-f="pct"]');
    if (pctEl) { pctEl.className = 'pct ' + pctClass(s.used_pct); pctEl.textContent = s.used_pct + '%'; }

    // Output tokens
    const outEl = card.querySelector('[data-f="out"]');
    if (outEl) outEl.innerHTML = IC_OUT + s.tokens_out_fmt;
  }

  // Summary
  const sum = document.getElementById('session-summary');
  if (sum) sum.innerHTML = `<div><b>${t('instances')}:</b> <span>${sessionsData.length}</span></div><div><b>${t('output')}:</b> <span>${fmtK(totOut)}</span></div><div><b>${t('mem')}:</b> <span>${fmtMem(totMem)}</span></div>`;
  return true;
}

// ── Usage ─────────────────────────────────────────
function datePickerHtml() {
  const presets = [
    ['today', t('presetToday')], ['7d', t('preset7d')], ['30d', t('preset30d')],
    ['90d', t('preset90d')], ['all', t('presetAll')]
  ];
  let html = '<div class="date-range">';
  html += `<label>${t('dateFrom')}</label>`;
  html += `<input type="date" id="dateFrom" value="${dateFrom}" onchange="onDateInputChange()">`;
  html += `<label>${t('dateTo')}</label>`;
  html += `<input type="date" id="dateTo" value="${dateTo}" onchange="onDateInputChange()">`;
  html += '<div class="date-presets">';
  for (const [key, label] of presets) {
    html += `<button class="date-preset${activePreset === key ? ' active' : ''}" onclick="applyPreset('${key}')">${label}</button>`;
  }
  html += '</div></div>';
  return html;
}

function onDateInputChange() {
  const fromEl = document.getElementById('dateFrom');
  const toEl = document.getElementById('dateTo');
  if (fromEl && toEl && fromEl.value && toEl.value) {
    setDateRange(fromEl.value, toEl.value, '');
  }
}

function renderUsage() {
  if (!historyData || historyData.loading) return `<div class="empty">${t('loading')}</div>`;
  const h = historyData;
  const r = rangeHistoryData && !rangeHistoryData.loading ? rangeHistoryData : null;
  let html = '';

  html += `<div style="color:var(--dim);font-size:12px;margin-bottom:12px;cursor:help" title="${t('dataSource')}">📂 ${t('dataSource')}</div>`;

  // Date range picker
  html += datePickerHtml();

  // Hero: today + week are always fixed; third card shows filtered range
  const rangeStats = r ? r.range : h.month;
  const rangeLabel = r ? t('selectedRange') : t('thisMonth');
  html += '<div class="hero-grid">';
  html += `<div class="hero"><div class="label">${t('today')} ${t('incCache')}</div><div class="num" style="color:var(--cyan)">${h.today.total_tokens_fmt}</div><div class="sub">${h.today.requests} ${t('req')} &middot; ${h.today.output_fmt} ${t('output')}</div></div>`;
  html += `<div class="hero"><div class="label">${t('thisWeek')} ${t('incCache')}</div><div class="num" style="color:var(--cyan)">${h.week.total_tokens_fmt}</div><div class="sub">${h.week.requests} ${t('req')} &middot; ${h.week.output_fmt} ${t('output')}</div></div>`;
  html += `<div class="hero"><div class="label">${rangeLabel} ${t('incCache')}</div><div class="num" style="color:var(--accent)">${rangeStats.total_tokens_fmt}</div><div class="sub">${rangeStats.requests} ${t('req')} &middot; ${rangeStats.output_fmt} ${t('output')}</div></div>`;
  html += '</div>';

  // Below here: use range-filtered data
  const projects = r ? r.projects : h.projects;
  const projTotal = rangeStats.total_tokens || 1;

  html += '<div class="grid2">';
  html += `<div class="card"><h3>${t('dailyTokensRange')}</h3><div class="chart-wrap"><canvas id="costChart"></canvas></div></div>`;
  html += `<div class="card"><h3>${t('byProjectRange')}</h3>`;
  for (const p of projects) {
    const pct = projTotal > 0 ? (p.total_tokens / projTotal * 100) : 0;
    html += `<div class="row row-bar"><span class="label">${esc(p.project)}</span><div class="bar-bg"><div class="bar-fill bar-ok" style="width:${pct}%"></div></div><span class="val">${p.total_tokens_fmt}</span></div>`;
  }
  html += '</div></div>';

  const sessionsList = r ? r.sessions : h.sessions;
  const blocks = r ? r.blocks : h.blocks;

  html += `<div class="card" style="margin-bottom:20px"><h3>${t('bySessionRange')}</h3>`;
  html += `<div class="row row-session" style="border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:4px"><span class="meta" style="font-weight:600;text-align:left">${t('hdrSession')}</span><span class="meta" style="font-weight:600">${t('hdrProject')}</span><span class="meta" style="font-weight:600">${t('hdrDate')}</span><span class="meta" style="font-weight:600">${t('output')}</span><span class="meta tip" style="font-weight:600">${t('total')}<span class="tiptext"><div class="trow"><span class="tlabel">${t('output')}</span><span class="trate">${t('tipOutput')}</span></div><div class="trow"><span class="tlabel">${t('input')}</span><span class="trate">${t('tipInput')}</span></div><div class="trow"><span class="tlabel">${t('cacheRead')}</span><span class="trate">${t('tipCacheRead')}</span></div><div class="trow"><span class="tlabel">${t('cacheWrite')}</span><span class="trate">${t('tipCacheWrite')}</span></div></span></span><span class="meta" style="font-weight:600">${t('estCost')}</span></div>`;
  if (sessionsList) {
    for (const s of sessionsList) {
      html += `<div class="row row-session"><span class="label">${esc(s.title)}</span><span class="val">${esc(s.project)}</span><span class="meta">${s.dates}</span><span class="val">${s.output_fmt}</span><span class="tip val">${s.total_tokens_fmt}<span class="tiptext"><div class="trow"><span class="tlabel">${t('output')}</span><span class="tval">${s.output_fmt}</span></div><div class="trow"><span class="tlabel">${t('input')}</span><span class="tval">${s.input_fmt}</span><span class="trate">1x</span></div><div class="trow"><span class="tlabel">${t('cacheRead')}</span><span class="tval">${s.cache_read_fmt}</span><span class="trate">0.1x</span></div><div class="trow"><span class="tlabel">${t('cacheWrite')}</span><span class="tval">${s.cache_write_fmt}</span><span class="trate">1.25x</span></div><div class="tsep"></div><div class="trow"><span class="tlabel">${t('total')}</span><span class="tval">${s.total_tokens_fmt}</span></div></span></span><span class="meta">~$${s.cost.toFixed(0)} USD</span></div>`;
    }
  }
  html += '</div>';

  // 5h Billing Blocks
  if (blocks && blocks.length) {
    html += `<div class="card" style="margin-bottom:20px"><h3>${t('billingBlocks')}</h3>`;
    for (const b of [...blocks].reverse()) {
      const activeTag = b.is_active ? `<span class="status status-working" style="font-size:10px;margin-left:8px">${t('active')}</span>` : '';
      html += `<div class="row row-block"><span class="meta">${b.start}</span><span class="meta">~</span><span class="meta">${b.end}</span><span class="val">${b.total_tokens_fmt}</span><span class="meta">${b.requests} ${t('req')}</span>${activeTag}</div>`;
    }
    html += '</div>';
  }

  return html;
}

function initCostChart() {
  const trendSrc = (rangeHistoryData && rangeHistoryData.trend) ? rangeHistoryData : historyData;
  if (!trendSrc || !trendSrc.trend || !document.getElementById('costChart')) return;
  if (costChart) costChart.destroy();
  const s = cs();
  const trend = trendSrc.trend;
  costChart = new Chart(document.getElementById('costChart'), {
    type: 'bar',
    data: {
      labels: trend.map(t => t.date),
      datasets: [{
        data: trend.map(t => t.tokens),
        backgroundColor: trend.map(t => t.pct < 50 ? s.getPropertyValue('--green').trim() : t.pct < 80 ? s.getPropertyValue('--yellow').trim() : s.getPropertyValue('--red').trim()),
        borderRadius: 4, borderSkipped: false,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => fmtK(c.raw) + ' tokens' } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: s.getPropertyValue('--dim').trim(), font: { size: 10 }, maxRotation: 45 } },
        y: { grid: { color: s.getPropertyValue('--border').trim() }, ticks: { color: s.getPropertyValue('--dim').trim(), callback: v => fmtK(v) } }
      }
    }
  });
}

// ── Models ─────────────────────────────────────────
function renderModels() {
  if (!historyData || historyData.loading) return `<div class="empty">${t('loading')}</div>`;
  const h = historyData;
  const r = rangeHistoryData && !rangeHistoryData.loading ? rangeHistoryData : null;
  const rangeStats = r ? r.range : h.month;
  const models = r ? r.models : h.models;
  let html = '';
  html += `<div style="color:var(--dim);font-size:12px;margin-bottom:12px;cursor:help" title="${t('dataSource')}">📂 ${t('dataSource')}</div>`;

  // Date range picker (shared with Usage)
  html += datePickerHtml();

  // Hero
  html += '<div class="hero-grid">';
  html += `<div class="hero"><div class="label">${t('totalTokens')}</div><div class="num" style="color:var(--cyan)">${rangeStats.total_tokens_fmt}</div><div class="sub">${rangeStats.requests} ${t('req')}</div></div>`;
  html += `<div class="hero"><div class="label">${t('output')}</div><div class="num" style="color:var(--green)">${rangeStats.output_fmt}</div><div class="sub">${rangeStats.input_fmt} ${t('input')}</div></div>`;
  html += `<div class="hero"><div class="label">${t('estCost')}</div><div class="num" style="color:var(--dim)">~$${rangeStats.cost.toFixed(0)} <span style="font-size:14px;font-weight:400">USD</span></div><div class="sub">${models.length} ${t('modelsUsed')}</div></div>`;
  html += '</div>';

  // Doughnut + period breakdown
  html += '<div class="grid2">';
  html += `<div class="card"><h3>${t('tokenDist')}</h3><div class="chart-wrap-sm"><canvas id="modelChart"></canvas></div></div>`;
  html += `<div class="card"><h3>${t('modelDetails')}</h3>`;
  for (const m of models) {
    html += `<div class="row"><span class="label">${esc(m.model.replace('claude-',''))}</span><span class="val">${m.total_tokens_fmt}</span><span class="val">${m.pct}%</span></div>`;
  }
  html += `<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">`;
  for (const [label, d] of [[t('today'), h.today], [t('d7'), h.week], [t('selectedRange'), rangeStats]]) {
    html += `<div class="row"><span class="label">${label}</span><span class="val">${d.total_tokens_fmt}</span><span class="val">${d.requests} ${t('req')}</span></div>`;
  }
  html += '</div></div>';
  html += '</div>';

  // Per-model detail cards
  html += '<div class="grid3">';
  for (const m of models) {
    const name = m.model.replace('claude-','');
    html += `<div class="card"><h3>${esc(name)}</h3>`;

    // Token breakdown rows
    html += `<div class="row"><span class="label">${t('output')}</span><span class="val">${m.output_fmt}</span><span class="meta">${m.output_pct}%</span></div>`;
    html += `<div class="row"><span class="label">${t('input')}</span><span class="val">${m.input_fmt}</span><span class="meta">${m.input_pct}%</span></div>`;
    html += `<div class="row"><span class="label">${t('cacheRead')}</span><span class="val">${m.cache_read_fmt}</span><span class="meta">${m.cache_read_pct}%</span></div>`;
    html += `<div class="row"><span class="label">${t('cacheWrite')}</span><span class="val">${m.cache_write_fmt}</span><span class="meta">${m.cache_write_pct}%</span></div>`;

    // Stacked bar
    html += `<div class="sbar">`;
    html += `<div style="width:${m.output_pct}%;background:var(--green)"></div>`;
    html += `<div style="width:${m.input_pct}%;background:var(--cyan)"></div>`;
    html += `<div style="width:${m.cache_read_pct}%;background:var(--blue)"></div>`;
    html += `<div style="width:${m.cache_write_pct}%;background:var(--accent)"></div>`;
    html += `</div>`;
    html += `<div class="sbar-legend">`;
    html += `<span><i class="dot" style="background:var(--green)"></i>${t('output')}</span>`;
    html += `<span><i class="dot" style="background:var(--cyan)"></i>${t('input')}</span>`;
    html += `<span><i class="dot" style="background:var(--blue)"></i>${t('cacheRead')}</span>`;
    html += `<span><i class="dot" style="background:var(--accent)"></i>${t('cacheWrite')}</span>`;
    html += `</div>`;

    // Summary
    html += `<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">`;
    html += `<div class="row"><span class="label">${t('totalTokens')}</span><span class="val">${m.total_tokens_fmt}</span></div>`;
    html += `<div class="row"><span class="label">${t('requests')}</span><span class="val">${m.requests}</span></div>`;
    html += `<div class="row"><span class="label">${t('avgPerReq')}</span><span class="val">${m.avg_tokens_fmt} ${t('perReq')}</span></div>`;
    html += `<div class="row"><span class="label">${t('estCost')}</span><span class="meta">~$${m.cost.toFixed(0)} USD</span></div>`;
    html += `</div>`;

    html += '</div>';
  }
  html += '</div>';

  return html;
}

function initModelChart() {
  const src = (rangeHistoryData && rangeHistoryData.models) ? rangeHistoryData : historyData;
  if (!src || !src.models || !document.getElementById('modelChart')) return;
  if (modelChart) modelChart.destroy();
  const s = cs();
  const models = src.models;
  const colors = [s.getPropertyValue('--accent').trim(), s.getPropertyValue('--cyan').trim(), s.getPropertyValue('--yellow').trim(), s.getPropertyValue('--green').trim(), s.getPropertyValue('--red').trim()];
  modelChart = new Chart(document.getElementById('modelChart'), {
    type: 'doughnut',
    data: {
      labels: models.map(m => m.model.replace('claude-','')),
      datasets: [{ data: models.map(m => m.total_tokens), backgroundColor: colors, borderWidth: 0, hoverOffset: 8 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '65%',
      plugins: {
        legend: { position: 'bottom', labels: { color: s.getPropertyValue('--text').trim(), padding: 16, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.label + ': ' + fmtK(c.raw) + ' tokens (' + models[c.dataIndex].pct + '%)' } }
      }
    }
  });
}

// ── Timeline ─────────────────────────────────────
let timelineData = null;

async function fetchTimeline() { try { timelineData = await (await fetch('/api/timeline')).json(); } catch {} }

function renderTimeline() {
  if (!timelineData || !timelineData.length) return `<div class="timeline-container"><div class="timeline-empty">${t('tlNoSessions')}</div></div>`;

  const now = Date.now() / 1000;
  const windowStart = now - 86400;
  const windowEnd = now;
  const windowLen = windowEnd - windowStart;

  // Build hour marks
  let hoursHtml = '';
  const startDate = new Date(windowStart * 1000);
  const startHour = new Date(startDate);
  startHour.setMinutes(0, 0, 0);
  if (startHour < startDate) startHour.setHours(startHour.getHours() + 1);

  for (let h = new Date(startHour); h.getTime() / 1000 < windowEnd; h.setHours(h.getHours() + 1)) {
    const ts = h.getTime() / 1000;
    const pct = ((ts - windowStart) / windowLen * 100);
    if (pct < 0 || pct > 100) continue;
    const label = h.getHours().toString().padStart(2, '0') + ':00';
    hoursHtml += `<span class="timeline-hour-mark" style="left:${pct.toFixed(2)}%">${label}</span>`;
  }

  const nowPct = ((now - windowStart) / windowLen * 100).toFixed(2);

  let rowsHtml = '';
  for (const session of timelineData) {
    let segsHtml = '';
    for (const seg of session.segments) {
      const left = Math.max(0, (seg.start - windowStart) / windowLen * 100);
      const width = Math.min(100 - left, (seg.end - seg.start) / windowLen * 100);
      if (width <= 0) continue;
      const cls = seg.type === 'active' ? 'timeline-seg-active' : 'timeline-seg-idle';
      const tooltip = seg.type === 'active'
        ? new Date(seg.start * 1000).toLocaleTimeString('sv-SE', {hour:'2-digit',minute:'2-digit'}) + ' - ' + new Date(seg.end * 1000).toLocaleTimeString('sv-SE', {hour:'2-digit',minute:'2-digit'})
        : '';
      segsHtml += `<div class="timeline-seg ${cls}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%" ${tooltip ? 'title="'+esc(tooltip)+'"' : ''}></div>`;
    }
    segsHtml += `<div class="timeline-now" style="left:${nowPct}%" title="${t('tlNow')}"></div>`;

    const statusCls = statusClass(session.status);
    rowsHtml += `<div class="timeline-row">
      <div class="timeline-label"><span class="tl-name">${esc(session.name)}</span><span class="tl-status status ${statusCls}" style="padding:1px 6px;border-radius:4px;font-size:10px">${session.status.toUpperCase()}</span></div>
      <div class="timeline-bar-wrap">${segsHtml}</div>
    </div>`;
  }

  return `<div class="timeline-container">
    <div class="timeline-header"><h3>${t('timelineTitle')}</h3></div>
    <div class="timeline-hours">${hoursHtml}</div>
    ${rowsHtml}
    <div class="timeline-legend">
      <span><i class="leg-box" style="background:var(--yellow);opacity:.85"></i>${t('tlActive')}</span>
      <span><i class="leg-box" style="background:var(--surface2);border:1px solid var(--border)"></i>${t('tlIdle')}</span>
      <span><i class="leg-box" style="background:var(--red);width:4px"></i>${t('tlNow')}</span>
    </div>
  </div>`;
}

// ── Session expand ──────────────────────────────
const detailCache = {};

async function toggleSession(pid) {
  const el = document.getElementById('session-' + pid);
  const detail = document.getElementById('detail-' + pid);
  if (!el || !detail) return;

  if (el.classList.contains('open')) {
    el.classList.remove('open');
    delete detailCache[pid];
    return;
  }

  el.classList.add('open');

  // Load detail if not cached
  if (!detailCache[pid]) {
    detail.innerHTML = `<div style="color:var(--dim);padding:8px">${t('loading')}</div>`;
    try {
      detailCache[pid] = await (await fetch('/api/session/' + pid)).json();
    } catch { detailCache[pid] = { error: 'failed' }; }
  }

  const d = detailCache[pid];
  if (d.error) { detail.innerHTML = `<div style="color:var(--red)">Error</div>`; return; }

  let html = '<div class="detail-grid">';

  // Left: info
  html += '<div class="detail-section">';
  html += `<h4>${t('sessions')}</h4>`;
  if (d.project_dir) html += `<div class="drow"><span class="dlabel">Path</span><span class="dval" style="font-size:11px">${esc(d.project_dir)}</span></div>`;
  if (d.duration_fmt) html += `<div class="drow"><span class="dlabel">Duration</span><span class="dval">${d.duration_fmt}</span></div>`;
  html += `<div class="drow"><span class="dlabel">${t('input')}</span><span class="dval">${d.tokens_in_fmt}</span></div>`;
  html += `<div class="drow"><span class="dlabel">${t('output')}</span><span class="dval">${d.tokens_out_fmt}</span></div>`;
  if (d.cost_est > 0) html += `<div class="drow"><span class="dlabel">${t('estCost')}</span><span class="dval" style="color:var(--dim)">~$${d.cost_est} USD</span></div>`;
  html += '</div>';

  // Right: tools
  html += '<div class="detail-section">';
  html += `<h4>Tools</h4>`;
  if (Object.keys(d.tools).length) {
    html += '<div class="tool-list">';
    for (const [name, count] of Object.entries(d.tools)) {
      html += `<span class="tool-tag">${esc(name)}<span class="tool-count">${count}</span></span>`;
    }
    html += '</div>';
  } else {
    html += `<span style="color:var(--dim)">—</span>`;
  }
  html += '</div>';
  html += '</div>';

  // Last activity
  if (d.last_activity) {
    html += `<div class="detail-section" style="margin-top:10px"><h4>Last Activity</h4><div class="last-activity">${mdLight(d.last_activity)}</div></div>`;
  }

  detail.innerHTML = html;
}

function render() {
  const el = document.getElementById('content');
  if (currentTab === 'sessions') {
    // Try patching in-place first (preserves expand state, no flicker)
    if (!patchSessions()) {
      // Full re-render needed (PIDs changed)
      const openPids = new Set([...document.querySelectorAll('.session.open')].map(e => parseInt(e.id.replace('session-',''))));
      el.innerHTML = renderSessions();
      for (const pid of openPids) {
        const s = document.getElementById('session-' + pid);
        if (s) { s.classList.add('open'); toggleSession(pid); }
      }
    }
  }
  else if (currentTab === 'timeline') { el.innerHTML = renderTimeline(); }
  else if (currentTab === 'usage') { el.innerHTML = renderUsage(); requestAnimationFrame(initCostChart); }
  else if (currentTab === 'models') { el.innerHTML = renderModels(); requestAnimationFrame(initModelChart); }
}

// Poll — only re-render sessions on 2s tick; charts only on history load
let lastSessionsJson = '';
async function pollSessions() {
  await fetchSessions();
  const json = JSON.stringify(sessionsData);
  if (json !== lastSessionsJson) {
    lastSessionsJson = json;
    if (currentTab === 'sessions') render();
    // Check context alerts after render so DOM cards exist
    checkCtxAlerts(sessionsData);
  }
}

async function pollHistory() {
  await fetchHistory();
  if (currentTab !== 'sessions' && currentTab !== 'timeline') render();
}

let lastTimelineJson = '';
async function pollTimeline() {
  await fetchTimeline();
  const json = JSON.stringify(timelineData);
  if (json !== lastTimelineJson) {
    lastTimelineJson = json;
    if (currentTab === 'timeline') render();
  }
}

pollHistory();
pollSessions();
pollTimeline();
setInterval(pollSessions, 2000);
setInterval(pollTimeline, 30000);
setInterval(pollHistory, 60000);
</script>
</body>
</html>
"""
_HTML_BYTES = HTML.encode()


# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._respond(200, "text/html", _HTML_BYTES)
        elif path == "/api/sessions":
            self._respond(200, "application/json", json.dumps(sessions_json()).encode())
        elif path == "/api/history":
            from_d = qs.get("from", [None])[0]
            to_d = qs.get("to", [None])[0]
            if from_d and to_d:
                # Validate date format
                try:
                    date_cls.fromisoformat(from_d)
                    date_cls.fromisoformat(to_d)
                except ValueError:
                    self._respond(400, "text/plain", b"Invalid date format, use YYYY-MM-DD")
                    return
                data = history_json_range(from_d, to_d)
            else:
                data = history_json()
            self._respond(200, "application/json", json.dumps(data).encode())
        elif path == "/api/timeline":
            self._respond(200, "application/json", json.dumps(timeline_json()).encode())
        elif path.startswith("/api/session/"):
            try:
                pid = int(path.split("/")[-1])
                self._respond(200, "application/json", json.dumps(session_detail(pid)).encode())
            except (ValueError, IndexError):
                self._respond(400, "text/plain", b"Invalid PID")
        else:
            self._respond(404, "text/plain", b"Not found")

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def main():
    server = ReusableHTTPServer((HOST, PORT), Handler)
    print(f"Claude Dashboard web: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


def main_autoreload():
    """Watch .py files, restart server on change."""
    src_dir = Path(__file__).parent
    watch_files = {p: p.stat().st_mtime for p in src_dir.glob("*.py")}

    while True:
        proc = subprocess.Popen([sys.executable, __file__, "--serve"])
        try:
            while proc.poll() is None:
                time.sleep(1)
                for p in src_dir.glob("*.py"):
                    old = watch_files.get(p, 0)
                    cur = p.stat().st_mtime
                    if cur != old:
                        watch_files[p] = cur
                        print(f"\n↻ {p.name} changed, reloading...")
                        proc.send_signal(signal.SIGTERM)
                        proc.wait(timeout=3)
                        raise StopIteration
        except StopIteration:
            continue
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            break
        break  # server exited normally


if __name__ == "__main__":
    if "--serve" in sys.argv:
        main()
    else:
        main_autoreload()
