# Claude Dashboard

Monitor Claude Code sessions in real-time — web dashboard with usage history, token stats, and tool analytics.

## Screenshots

- **Sessions** — live session cards with status, context bar, expandable detail (tool stats, last activity)
- **Usage** — daily token trend chart, by-project & by-session breakdown
- **Models** — token distribution doughnut, per-model breakdown with stacked bar

## Architecture

```
Claude Code
  ├── PostToolUse hook (session-writer.sh)
  │     └── writes ~/.claude/sessions/<pid>.json
  ├── Stop/UserPromptSubmit hook (session-status.sh)
  │     └── writes ~/.claude/sessions/<pid>.status
  └── ~/.claude/projects/**/*.jsonl  (conversation transcripts)

claude-dashboard/
  ├── web.py              → web server (port 7878)
  ├── session_reader.py   → reads live session JSON + .status
  ├── history_reader.py   → parses JSONL history
  └── hooks/
      ├── session-writer.sh
      └── session-status.sh
```

## Setup

### 1. Install

```bash
cd ~/claude-dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Install hooks

```bash
mkdir -p ~/.claude/hooks
cp hooks/session-writer.sh ~/.claude/hooks/
cp hooks/session-status.sh ~/.claude/hooks/
```

### 3. Configure Claude Code hooks

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [{
          "type": "command",
          "command": "sh ~/.claude/hooks/session-writer.sh",
          "async": true
        }]
      }
    ],
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "sh ~/.claude/hooks/session-status.sh idle"
        }]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [{
          "type": "command",
          "command": "sh ~/.claude/hooks/session-status.sh working"
        }]
      }
    ]
  }
}
```

### 4. Run

```bash
# Foreground
.venv/bin/python web.py

# Or as systemd service (persistent)
cat <<EOF | sudo tee /etc/systemd/system/claude-dashboard-web.service
[Unit]
Description=Claude Dashboard Web
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python web.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now claude-dashboard-web
```

Browse: `http://localhost:7878`

## Features

- **Live sessions** — status (working/idle), context %, output tokens, memory
- **Expandable cards** — click to see tool usage stats, last activity, duration
- **Usage history** — daily token chart, by-project, by-session (30 days)
- **Model analysis** — token distribution, breakdown per model type
- **Dark / Light theme** — saved in localStorage
- **i18n** — English / 繁體中文
- **DOM patching** — session updates without page flicker
- **Auto-reload** — web server restarts on code change
- **Zero extra dependencies** — only `rich` + `inotify-simple` (Chart.js via CDN)

## How it works

- `session-writer.sh` runs on every tool use, incrementally parses the JSONL transcript for model/token data (not from hook input — PostToolUse doesn't include context window data)
- `session-status.sh` writes idle/working status on Stop/UserPromptSubmit events
- `session_reader.py` reads `.json` + `.status` files, checks process liveness with `kill -0`
- `history_reader.py` parses all `*.jsonl` under `~/.claude/projects/` (excludes `subagents/` to avoid double-counting)
- `web.py` serves a single-page app with JSON API endpoints, polls sessions every 2s

## Notes

- **Cost is estimated** — hardcoded token pricing, not actual billing. Max plan users pay a flat fee.
- **Single machine only** — reads local files, no remote API.
