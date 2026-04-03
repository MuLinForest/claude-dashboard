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
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from session_reader import load_sessions, fmt_tokens, fmt_mem, SESSIONS_DIR
from history_reader import load_history, HistoryStats, estimate_cost

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

@media (max-width: 768px) {
  .hero-grid { grid-template-columns: 1fr; }
  .grid2 { grid-template-columns: 1fr; }
  .header { padding: 14px 16px; }
  .content { padding: 16px; }
  .tabs { padding: 0 16px; }
  .tab { padding: 10px 14px; font-size: 12px; }
  .bar-bg { width: 120px; }
}
</style>
</head>
<body>

<div class="header">
  <h1>Claude Dashboard</h1>
  <span class="time" id="clock"></span>
  <div class="header-right">
    <button class="theme-btn" id="langToggle"></button>
    <button class="theme-btn" id="themeToggle"></button>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-tab="sessions">Sessions</div>
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
  updateTabs();
  render();
};

function updateTabs() {
  const keys = ['sessions', 'usage', 'models'];
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

const VALID_TABS = ['sessions', 'usage', 'models'];
let currentTab = VALID_TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'sessions';
let sessionsData = [], historyData = null;
let costChart = null, modelChart = null;

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

async function fetchSessions() { try { sessionsData = await (await fetch('/api/sessions')).json(); } catch {} }
async function fetchHistory() { try { historyData = await (await fetch('/api/history')).json(); } catch {} }

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
function renderUsage() {
  if (!historyData || historyData.loading) return `<div class="empty">${t('loading')}</div>`;
  const h = historyData, s = cs();
  let html = '';

  html += `<div style="color:var(--dim);font-size:12px;margin-bottom:12px;cursor:help" title="${t('dataSource')}">📂 ${t('dataSource')}</div>`;
  html += '<div class="hero-grid">';
  html += `<div class="hero"><div class="label">${t('today')} ${t('incCache')}</div><div class="num" style="color:var(--cyan)">${h.today.total_tokens_fmt}</div><div class="sub">${h.today.requests} ${t('req')} &middot; ${h.today.output_fmt} ${t('output')}</div></div>`;
  html += `<div class="hero"><div class="label">${t('thisWeek')} ${t('incCache')}</div><div class="num" style="color:var(--cyan)">${h.week.total_tokens_fmt}</div><div class="sub">${h.week.requests} ${t('req')} &middot; ${h.week.output_fmt} ${t('output')}</div></div>`;
  html += `<div class="hero"><div class="label">${t('thisMonth')} ${t('incCache')}</div><div class="num" style="color:var(--accent)">${h.month.total_tokens_fmt}</div><div class="sub">${h.month.requests} ${t('req')} &middot; ${h.month.output_fmt} ${t('output')}</div></div>`;
  html += '</div>';

  html += '<div class="grid2">';
  html += `<div class="card"><h3>${t('dailyTokens')}</h3><div class="chart-wrap"><canvas id="costChart"></canvas></div></div>`;
  html += `<div class="card"><h3>${t('byProject')}</h3>`;
  for (const p of h.projects) {
    const pct = h.month.total_tokens > 0 ? (p.total_tokens / h.month.total_tokens * 100) : 0;
    html += `<div class="row row-bar"><span class="label">${esc(p.project)}</span><div class="bar-bg"><div class="bar-fill bar-ok" style="width:${pct}%"></div></div><span class="val">${p.total_tokens_fmt}</span></div>`;
  }
  html += '</div></div>';

  html += `<div class="card" style="margin-bottom:20px"><h3>${t('bySession')}</h3>`;
  html += `<div class="row row-session" style="border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:4px"><span class="meta" style="font-weight:600;text-align:left">${t('hdrSession')}</span><span class="meta" style="font-weight:600">${t('hdrProject')}</span><span class="meta" style="font-weight:600">${t('hdrDate')}</span><span class="meta" style="font-weight:600">${t('output')}</span><span class="meta tip" style="font-weight:600">${t('total')}<span class="tiptext"><div class="trow"><span class="tlabel">${t('output')}</span><span class="trate">${t('tipOutput')}</span></div><div class="trow"><span class="tlabel">${t('input')}</span><span class="trate">${t('tipInput')}</span></div><div class="trow"><span class="tlabel">${t('cacheRead')}</span><span class="trate">${t('tipCacheRead')}</span></div><div class="trow"><span class="tlabel">${t('cacheWrite')}</span><span class="trate">${t('tipCacheWrite')}</span></div></span></span><span class="meta" style="font-weight:600">${t('estCost')}</span></div>`;
  if (h.sessions) {
    for (const s of h.sessions) {
      html += `<div class="row row-session"><span class="label">${esc(s.title)}</span><span class="val">${esc(s.project)}</span><span class="meta">${s.dates}</span><span class="val">${s.output_fmt}</span><span class="tip val">${s.total_tokens_fmt}<span class="tiptext"><div class="trow"><span class="tlabel">${t('output')}</span><span class="tval">${s.output_fmt}</span></div><div class="trow"><span class="tlabel">${t('input')}</span><span class="tval">${s.input_fmt}</span><span class="trate">1x</span></div><div class="trow"><span class="tlabel">${t('cacheRead')}</span><span class="tval">${s.cache_read_fmt}</span><span class="trate">0.1x</span></div><div class="trow"><span class="tlabel">${t('cacheWrite')}</span><span class="tval">${s.cache_write_fmt}</span><span class="trate">1.25x</span></div><div class="tsep"></div><div class="trow"><span class="tlabel">${t('total')}</span><span class="tval">${s.total_tokens_fmt}</span></div></span></span><span class="meta">~$${s.cost.toFixed(0)} USD</span></div>`;
    }
  }
  html += '</div>';

  // 5h Billing Blocks
  if (h.blocks && h.blocks.length) {
    html += `<div class="card" style="margin-bottom:20px"><h3>${t('billingBlocks')}</h3>`;
    for (const b of [...h.blocks].reverse()) {
      const activeTag = b.is_active ? `<span class="status status-working" style="font-size:10px;margin-left:8px">${t('active')}</span>` : '';
      html += `<div class="row row-block"><span class="meta">${b.start}</span><span class="meta">~</span><span class="meta">${b.end}</span><span class="val">${b.total_tokens_fmt}</span><span class="meta">${b.requests} ${t('req')}</span>${activeTag}</div>`;
    }
    html += '</div>';
  }

  return html;
}

function initCostChart() {
  if (!historyData || !document.getElementById('costChart')) return;
  if (costChart) costChart.destroy();
  const s = cs();
  costChart = new Chart(document.getElementById('costChart'), {
    type: 'bar',
    data: {
      labels: historyData.trend.map(t => t.date),
      datasets: [{
        data: historyData.trend.map(t => t.tokens),
        backgroundColor: historyData.trend.map(t => t.pct < 50 ? s.getPropertyValue('--green').trim() : t.pct < 80 ? s.getPropertyValue('--yellow').trim() : s.getPropertyValue('--red').trim()),
        borderRadius: 4, borderSkipped: false,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => fmtK(c.raw) + ' tokens' } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: s.getPropertyValue('--dim').trim(), font: { size: 10 } } },
        y: { grid: { color: s.getPropertyValue('--border').trim() }, ticks: { color: s.getPropertyValue('--dim').trim(), callback: v => fmtK(v) } }
      }
    }
  });
}

// ── Models ─────────────────────────────────────────
function renderModels() {
  if (!historyData || historyData.loading) return `<div class="empty">${t('loading')}</div>`;
  const h = historyData;
  let html = '';
  html += `<div style="color:var(--dim);font-size:12px;margin-bottom:12px;cursor:help" title="${t('dataSource')}">📂 ${t('dataSource')}</div>`;

  // Hero
  html += '<div class="hero-grid">';
  html += `<div class="hero"><div class="label">${t('totalTokens30')}</div><div class="num" style="color:var(--cyan)">${h.month.total_tokens_fmt}</div><div class="sub">${h.month.requests} ${t('req')}</div></div>`;
  html += `<div class="hero"><div class="label">${t('outputTokens30')}</div><div class="num" style="color:var(--green)">${h.month.output_fmt}</div><div class="sub">${h.month.input_fmt} ${t('input')}</div></div>`;
  html += `<div class="hero"><div class="label">${t('estCost30')}</div><div class="num" style="color:var(--dim)">~$${h.month.cost.toFixed(0)} <span style="font-size:14px;font-weight:400">USD</span></div><div class="sub">${h.models.length} ${t('modelsUsed')}</div></div>`;
  html += '</div>';

  // Doughnut + period breakdown
  html += '<div class="grid2">';
  html += `<div class="card"><h3>${t('tokenDist')}</h3><div class="chart-wrap-sm"><canvas id="modelChart"></canvas></div></div>`;
  html += `<div class="card"><h3>${t('modelDetails')}</h3>`;
  for (const m of h.models) {
    html += `<div class="row"><span class="label">${esc(m.model.replace('claude-',''))}</span><span class="val">${m.total_tokens_fmt}</span><span class="val">${m.pct}%</span></div>`;
  }
  html += `<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border)">`;
  for (const [label, d] of [[t('today'), h.today], [t('d7'), h.week], [t('d30'), h.month]]) {
    html += `<div class="row"><span class="label">${label}</span><span class="val">${d.total_tokens_fmt}</span><span class="val">${d.requests} ${t('req')}</span></div>`;
  }
  html += '</div></div>';
  html += '</div>';

  // Per-model detail cards
  html += '<div class="grid3">';
  for (const m of h.models) {
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
  if (!historyData || !document.getElementById('modelChart')) return;
  if (modelChart) modelChart.destroy();
  const s = cs();
  const colors = [s.getPropertyValue('--accent').trim(), s.getPropertyValue('--cyan').trim(), s.getPropertyValue('--yellow').trim(), s.getPropertyValue('--green').trim(), s.getPropertyValue('--red').trim()];
  modelChart = new Chart(document.getElementById('modelChart'), {
    type: 'doughnut',
    data: {
      labels: historyData.models.map(m => m.model.replace('claude-','')),
      datasets: [{ data: historyData.models.map(m => m.total_tokens), backgroundColor: colors, borderWidth: 0, hoverOffset: 8 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '65%',
      plugins: {
        legend: { position: 'bottom', labels: { color: s.getPropertyValue('--text').trim(), padding: 16, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.label + ': ' + fmtK(c.raw) + ' tokens (' + historyData.models[c.dataIndex].pct + '%)' } }
      }
    }
  });
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
  else if (currentTab === 'usage') { el.innerHTML = renderUsage(); requestAnimationFrame(initCostChart); }
  else { el.innerHTML = renderModels(); requestAnimationFrame(initModelChart); }
}

// Poll — only re-render sessions on 2s tick; charts only on history load
let lastSessionsJson = '';
async function pollSessions() {
  await fetchSessions();
  const json = JSON.stringify(sessionsData);
  if (json !== lastSessionsJson) {
    lastSessionsJson = json;
    if (currentTab === 'sessions') render();
  }
}

async function pollHistory() {
  await fetchHistory();
  if (currentTab !== 'sessions') render();
}

pollHistory();
pollSessions();
setInterval(pollSessions, 2000);
setInterval(pollHistory, 60000);
</script>
</body>
</html>
"""
_HTML_BYTES = HTML.encode()


# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._respond(200, "text/html", _HTML_BYTES)
        elif self.path == "/api/sessions":
            self._respond(200, "application/json", json.dumps(sessions_json()).encode())
        elif self.path == "/api/history":
            self._respond(200, "application/json", json.dumps(history_json()).encode())
        elif self.path.startswith("/api/session/"):
            try:
                pid = int(self.path.split("/")[-1])
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
