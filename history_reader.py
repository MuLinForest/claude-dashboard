"""
history_reader.py — parse ~/.claude/projects/**/*.jsonl 算歷史用量統計
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from session_reader import fmt_tokens


PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Pricing per 1M tokens (USD) — approximate, as of 2025
PRICING = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75},
    "sonnet": {"input":  3.0, "output": 15.0, "cache_read": 0.30, "cache_write":  3.75},
    "haiku":  {"input":  0.8, "output":  4.0, "cache_read": 0.08, "cache_write":  1.00},
}


def estimate_cost(model: str, input_t: int, output_t: int, cache_read: int, cache_write: int) -> float:
    key = "sonnet"
    if "opus" in model:
        key = "opus"
    elif "haiku" in model:
        key = "haiku"
    p = PRICING[key]
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
            dates = sorted(r.date for r in rs)
            title = next((r.session_title for r in rs if r.session_title), "")
            project = next((r.project for r in rs if r.project), "unknown")
            result.append({
                **s,
                "session_id": sid,
                "title": title or sid[:8],
                "project": project,
                "date_start": dates[0] if dates else "",
                "date_end": dates[-1] if dates else "",
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
