"""
history_reader.py — parse ~/.claude/projects/**/*.jsonl 算歷史用量統計
"""

from __future__ import annotations

import json
import threading
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from session_reader import fmt_tokens


PROJECTS_DIR = Path.home() / ".claude" / "projects"

# ── Online pricing from LiteLLM ──────────────────────────────────────────────
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
CLAUDE_PREFIXES = ("anthropic/", "claude-")

# Fallback pricing per 1M tokens (USD)
FALLBACK_PRICING = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "sonnet": {"input":  3.0, "output": 15.0, "cache_read": 0.30, "cache_write":  3.75},
    "haiku":  {"input":  0.8, "output":  4.0, "cache_read": 0.08, "cache_write":  1.00},
}

_pricing_cache: dict | None = None
_pricing_lock = threading.Lock()
_match_cache: dict[str, dict | None] = {}


def _fetch_pricing() -> dict:
    global _pricing_cache
    with _pricing_lock:
        if _pricing_cache is not None:
            return _pricing_cache
        try:
            req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "claude-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            result = {}
            for name, info in data.items():
                if isinstance(info, dict) and any(name.startswith(p) for p in CLAUDE_PREFIXES):
                    result[name] = info
            _pricing_cache = result
            return result
        except Exception:
            _pricing_cache = {}
            return {}


def _match_pricing(model: str) -> dict | None:
    if model in _match_cache:
        return _match_cache[model]
    pricing = _fetch_pricing()
    if not pricing:
        return None
    result = None
    for candidate in [model, f"anthropic/{model}"]:
        if candidate in pricing:
            result = pricing[candidate]
            break
    if result is None:
        # One-directional: model name contains pricing key (not reverse)
        lower = model.lower()
        for key, val in pricing.items():
            if key.lower() in lower:
                result = val
                break
    _match_cache[model] = result
    return result


def estimate_cost(model: str, input_t: int, output_t: int, cache_read: int, cache_write: int) -> float:
    """Estimate cost using online LiteLLM pricing, fallback to hardcoded."""
    info = _match_pricing(model)
    if info:
        threshold = 200_000

        def _tiered(tokens: int, base_key: str, tiered_key: str) -> float:
            base = info.get(base_key, 0) or 0
            above = info.get(tiered_key, 0) or 0
            if tokens > threshold and above:
                return min(tokens, threshold) * base + max(0, tokens - threshold) * above
            return tokens * base

        return (
            _tiered(input_t,     "input_cost_per_token",                "input_cost_per_token_above_200k_tokens") +
            _tiered(output_t,    "output_cost_per_token",               "output_cost_per_token_above_200k_tokens") +
            _tiered(cache_read,  "cache_read_input_token_cost",         "cache_read_input_token_cost_above_200k_tokens") +
            _tiered(cache_write, "cache_creation_input_token_cost",     "cache_creation_input_token_cost_above_200k_tokens")
        )
    # Fallback
    key = "sonnet"
    if "opus" in model:
        key = "opus"
    elif "haiku" in model:
        key = "haiku"
    p = FALLBACK_PRICING[key]
    return (
        input_t   * p["input"]       / 1_000_000 +
        output_t  * p["output"]      / 1_000_000 +
        cache_read  * p["cache_read"]  / 1_000_000 +
        cache_write * p["cache_write"] / 1_000_000
    )


@dataclass
class UsageRecord:
    date: str          # YYYY-MM-DD
    project: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    requests: int = 0
    session_id: str = ""
    session_title: str = ""
    timestamp: str = ""  # ISO format for 5h block grouping


@dataclass
class HistoryStats:
    records: list[UsageRecord] = field(default_factory=list)

    def _filter(self, since: datetime) -> list[UsageRecord]:
        cutoff = since.strftime("%Y-%m-%d")
        return [r for r in self.records if r.date >= cutoff]

    def _sum(self, recs: list[UsageRecord]) -> dict:
        total_in = sum(r.input_tokens for r in recs)
        total_out = sum(r.output_tokens for r in recs)
        cache_r = sum(r.cache_read for r in recs)
        cache_w = sum(r.cache_write for r in recs)
        cost = sum(r.cost_usd for r in recs)
        return {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read": cache_r,
            "cache_write": cache_w,
            "cost_usd": cost,
            "requests": sum(r.requests for r in recs),
        }

    def by_period(self, days: int) -> dict:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        return self._sum(self._filter(since))

    def by_model(self, days: int = 30) -> dict[str, dict]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        groups: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in self._filter(since):
            groups[r.model].append(r)
        return {m: self._sum(rs) for m, rs in sorted(groups.items())}

    def by_project(self, days: int = 30) -> dict[str, dict]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        groups: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in self._filter(since):
            groups[r.project].append(r)
        return {
            p: self._sum(rs)
            for p, rs in sorted(groups.items(), key=lambda x: -sum(r.cost_usd for r in x[1]))
        }

    def by_session(self, days: int = 30) -> list[dict]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        groups: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in self._filter(since):
            if r.session_id:
                groups[r.session_id].append(r)
        result = []
        for sid, rs in groups.items():
            s = self._sum(rs)
            date_list = [r.date for r in rs]
            title = next((r.session_title for r in rs if r.session_title), "")
            project = next((r.project for r in rs if r.project), "unknown")
            result.append({
                **s,
                "session_id": sid,
                "title": title or sid[:8],
                "project": project,
                "date_start": min(date_list) if date_list else "",
                "date_end": max(date_list) if date_list else "",
            })
        result.sort(key=lambda x: -x["cost_usd"])
        return result

    def daily_trend(self, days: int = 14) -> list[tuple[str, dict]]:
        """Returns list of (date_str, stats_dict) for last N days, oldest first."""
        today = datetime.now(timezone.utc).date()
        cutoff = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")

        # Single pass: group records by date
        by_date: dict[str, list[UsageRecord]] = defaultdict(list)
        for r in self.records:
            if r.date >= cutoff:
                by_date[r.date].append(r)

        result = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            result.append((d, self._sum(by_date.get(d, []))))
        return result

    def billing_blocks(self, hours: int = 5, recent: int = 10) -> list[dict]:
        """Group records into 5h billing windows. Returns most recent blocks."""
        if not self.records:
            return []

        BLOCK_SEC = hours * 3600
        now = datetime.now(timezone.utc)

        timed: list[tuple[datetime, UsageRecord]] = []
        for r in self.records:
            if not r.timestamp:
                continue
            try:
                dt = datetime.fromisoformat(r.timestamp.replace("Z", "+00:00"))
                timed.append((dt, r))
            except (ValueError, AttributeError):
                continue
        timed.sort(key=lambda x: x[0])

        blocks: list[dict] = []
        block_start: datetime | None = None
        block_recs: list[UsageRecord] = []

        for dt, r in timed:
            if block_start is None:
                # Floor to hour
                block_start = dt.replace(minute=0, second=0, microsecond=0)
                block_recs = [r]
            elif (dt - block_start).total_seconds() > BLOCK_SEC:
                # Close block
                s = self._sum(block_recs)
                blocks.append({
                    **s,
                    "start": block_start.isoformat(),
                    "end": (block_start + timedelta(seconds=BLOCK_SEC)).isoformat(),
                    "is_active": False,
                })
                block_start = dt.replace(minute=0, second=0, microsecond=0)
                block_recs = [r]
            else:
                block_recs.append(r)

        # Close last block
        if block_start and block_recs:
            end = block_start + timedelta(seconds=BLOCK_SEC)
            s = self._sum(block_recs)
            blocks.append({
                **s,
                "start": block_start.isoformat(),
                "end": end.isoformat(),
                "is_active": now < end,
            })

        return blocks[-recent:]


def load_history(max_files: int = 200) -> HistoryStats:
    """Parse all JSONL files and return aggregated HistoryStats."""
    stats = HistoryStats()

    if not PROJECTS_DIR.exists():
        return stats

    all_files = sorted(
        (p for p in PROJECTS_DIR.rglob("*.jsonl") if "subagents" not in p.parts),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_files]

    for path in all_files:
        parts = path.parent.name.lstrip("-").split("-")
        project = parts[-1] if parts else "unknown"
        session_id = path.stem

        # Single pass: extract title and parse records together
        session_title = ""
        try:
            for line in path.read_text(errors="ignore").splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if d.get("type") == "custom-title":
                    session_title = d.get("customTitle", "")
                    continue

                if d.get("type") != "assistant":
                    continue

                msg = d.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue

                model = msg.get("model", "unknown")
                if model.startswith("<"):
                    continue

                ts = d.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    date = dt.strftime("%Y-%m-%d")
                except (ValueError, AttributeError):
                    continue

                input_t  = usage.get("input_tokens", 0)
                output_t = usage.get("output_tokens", 0)
                cache_r  = usage.get("cache_read_input_tokens", 0)
                cache_w  = usage.get("cache_creation_input_tokens", 0)

                stats.records.append(UsageRecord(
                    date=date,
                    project=project,
                    model=model,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    cache_read=cache_r,
                    cache_write=cache_w,
                    cost_usd=estimate_cost(model, input_t, output_t, cache_r, cache_w),
                    requests=1,
                    session_id=session_id,
                    session_title=session_title,
                    timestamp=ts,
                ))
        except OSError:
            continue

    return stats


if __name__ == "__main__":
    import time as _time
    t0 = _time.time()
    h = load_history()
    elapsed = _time.time() - t0

    print(f"Loaded {len(h.records)} requests in {elapsed:.2f}s\n")

    for label, days in [("Today", 1), ("This week", 7), ("This month", 30)]:
        s = h.by_period(days)
        print(f"── {label} ──")
        print(f"  Cost:    ${s['cost_usd']:.4f}")
        print(f"  Input:   {fmt_tokens(s['input_tokens'])}")
        print(f"  Output:  {fmt_tokens(s['output_tokens'])}")
        print(f"  Requests:{s['requests']}")
        print()

    print("── By model (30d) ──")
    for model, s in h.by_model().items():
        print(f"  {model:<30} ${s['cost_usd']:>8.4f}  {fmt_tokens(s['input_tokens']):>8} in  {fmt_tokens(s['output_tokens']):>8} out")

    print()
    print("── By project (30d) ──")
    for proj, s in h.by_project().items():
        print(f"  {proj:<20} ${s['cost_usd']:>8.4f}")
