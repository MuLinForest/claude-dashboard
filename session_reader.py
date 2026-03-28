"""
session_reader.py — 讀取 ~/.claude/sessions/*.json 的即時 session 狀態
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path


SESSIONS_DIR = Path.home() / ".claude" / "sessions"


@dataclass
class Session:
    pid: int
    epoch: int
    model: str
    project_dir: str
    project_name: str
    git_branch: str
    status: str
    last_activity: str
    used_pct: int
    tokens_in: int
    tokens_out: int
    mem_kb: int
    cost_usd: float
    session_title: str

    @property
    def display_name(self) -> str:
        return self.session_title or self.project_name

    @property
    def short_model(self) -> str:
        """'claude-sonnet-4-6' → 'Sonnet 4.6'  |  'Claude Sonnet 4.6' → 'Sonnet 4.6'"""
        name = self.model
        # Strip leading "claude-" or "Claude "
        for prefix in ("claude-", "Claude "):
            if name.lower().startswith(prefix.lower()):
                name = name[len(prefix):]
                break
        # 'sonnet-4-6' → 'Sonnet 4.6'
        # Capitalise first word, turn dashes to spaces, then restore version dots
        parts = name.split("-")
        parts[0] = parts[0].capitalize()
        name = " ".join(parts)
        # Merge digit-space-digit back into digit.digit (version number)
        name = re.sub(r"(\d) (\d)", r"\1.\2", name)
        return name

    @property
    def is_alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def load_sessions(cleanup_dead: bool = True) -> list[Session]:
    """讀取所有 session JSON，過濾掉死掉的 process。"""
    sessions: list[Session] = []

    if not SESSIONS_DIR.exists():
        return sessions

    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        pid = int(data.get("pid", 0))

        # Read authoritative status from .status file (written by Stop/UserPromptSubmit hooks)
        status = data.get("status", "idle")
        status_file = SESSIONS_DIR / f"{pid}.status"
        try:
            parts = status_file.read_text().split()
            if parts:
                status = parts[0]
        except OSError:
            pass

        # Fallback: if no .status file and JSON epoch is stale, assume idle
        if status in ("", "null"):
            epoch = int(data.get("epoch", 0))
            if time.time() - epoch > 10:
                status = "idle"
            else:
                status = "working"

        s = Session(
            pid=pid,
            epoch=int(data.get("epoch", 0)),
            model=data.get("model", "Unknown"),
            project_dir=data.get("project_dir", ""),
            project_name=data.get("project_name", "unknown"),
            git_branch=data.get("git_branch", ""),
            status=status,
            last_activity=data.get("last_activity", ""),
            used_pct=int(data.get("used_pct", 0)),
            tokens_in=int(data.get("tokens_in", 0)),
            tokens_out=int(data.get("tokens_out", 0)),
            mem_kb=int(data.get("mem_kb", 0)),
            cost_usd=float(data.get("cost_usd", 0)),
            session_title=data.get("session_title", ""),
        )

        if s.pid == 0:
            continue

        if not s.is_alive:
            if cleanup_dead:
                path.unlink(missing_ok=True)
                (SESSIONS_DIR / f"{s.pid}.status").unlink(missing_ok=True)
            continue

        sessions.append(s)

    sessions.sort(key=lambda s: s.epoch, reverse=True)
    return sessions


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def fmt_mem(kb: int) -> str:
    if kb >= 1_048_576:
        return f"{kb/1_048_576:.1f}G"
    if kb >= 1_024:
        return f"{kb/1_024:.1f}M"
    return f"{kb}K"


if __name__ == "__main__":
    sessions = load_sessions()
    if not sessions:
        print("No active sessions.")
    else:
        print(f"{'PID':<8} {'NAME':<22} {'PROJECT':<16} {'MODEL':<20} {'CTX%':>5} {'IN':>7} {'OUT':>7} {'MEM':>7} STATUS")
        print("-" * 100)
        for s in sessions:
            print(
                f"{s.pid:<8} {s.display_name[:22]:<22} {s.project_name[:16]:<16}"
                f" {s.short_model[:20]:<20} {s.used_pct:>4}%"
                f" {fmt_tokens(s.tokens_in):>7} {fmt_tokens(s.tokens_out):>7}"
                f" {fmt_mem(s.mem_kb):>7} {s.status.upper()}"
            )
