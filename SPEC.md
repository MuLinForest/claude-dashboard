# Claude Dashboard — 專案規格

## 目標

Claude Code 即時監控 dashboard，結合：
- **即時** session 狀態（hook 寫的 JSON + .status 檔案）
- **歷史統計**（parse `~/.claude/projects/**/*.jsonl`）
- **Web UI**（內建 HTTP server + Chart.js）

---

## 架構

```
Claude Code
  ├── PostToolUse hook (session-writer.sh)
  │     └── 寫 ~/.claude/sessions/<pid>.json（即時資料來源）
  │     └── 增量解析 JSONL transcript 取得 model / tokens
  ├── Stop hook (session-status.sh idle)
  │     └── 寫 ~/.claude/sessions/<pid>.status
  ├── UserPromptSubmit hook (session-status.sh working)
  │     └── 同上
  └── ~/.claude/projects/**/*.jsonl → 歷史記錄（Claude Code 自動寫）

claude-dashboard/
  ├── web.py               → Web server（port 7878）
  ├── session_reader.py    → 讀即時 session JSON + .status
  ├── history_reader.py    → parse JSONL 歷史記錄
  ├── hooks/
  │   ├── session-writer.sh
  │   └── session-status.sh
  ├── claude-dashboard-web.service → systemd unit
  └── requirements.txt
```

---

## 資料來源

### 即時資料：`~/.claude/sessions/<pid>.json`

由 `session-writer.sh` hook 寫入。PostToolUse hook 的 input 只有 tool metadata，
**不包含 context window 資料**，所以 model / tokens 從 JSONL transcript 增量解析。

格式：
```json
{
  "pid": 12345,
  "epoch": 1711612800,
  "model": "claude-sonnet-4-6",
  "project_dir": "/home/mulin/homelab",
  "project_name": "homelab",
  "git_branch": "main",
  "status": "working",
  "last_activity": "",
  "used_pct": 42,
  "tokens_in": 50000,
  "tokens_out": 3000,
  "mem_kb": 204800,
  "cost_usd": 0,
  "session_title": "dashboard 開發"
}
```

- `tokens_in`：最後一次 request 的 context 大小（input + cache_read + cache_creation）
- `tokens_out`：整個 session 累積輸出 tokens
- `used_pct`：估算的 context window 使用率（opus=1M, 其他=200k）
- `status`：優先讀 `.status` 檔案，fallback 到 JSON 內的值

### 狀態檔：`~/.claude/sessions/<pid>.status`

由 `session-status.sh` 寫入，格式：`<status> <epoch>`

| 事件 | 寫入 |
|------|------|
| Stop | `idle <epoch>` |
| UserPromptSubmit | `working <epoch>` |

### 歷史資料：`~/.claude/projects/**/*.jsonl`

每行一個 JSON 物件。關鍵：`type == "assistant"` 的行包含 `message.usage`。

```json
{
  "type": "assistant",
  "message": {
    "model": "claude-sonnet-4-6",
    "usage": {
      "input_tokens": 3,
      "output_tokens": 800,
      "cache_read_input_tokens": 50000,
      "cache_creation_input_tokens": 2000
    }
  },
  "timestamp": "2026-03-28T10:30:00Z",
  "sessionId": "72c5f7d4-..."
}
```

注意：`subagents/` 目錄下的 JSONL 排除（tokens 已計入主 session）。

### 增量解析快取

`session-writer.sh` 在 `/tmp/claude-sw-tok-<pid>` 記錄上次讀到的行數，
每次只解析新增的行。快取在重開機後自動清除，下次 hook 觸發時重建。

---

## 功能規格

### Sessions 頁（主畫面）

每個 session 一張卡片，三行佈局：
```
NAS管理  homelab                         WORKING
Sonnet 4.6 · ⑂ main · ⚙ 1753739 · 🧩 393M
████████░░░░░░  42%  📤 35.8k
```

- Status 顏色：WORKING=黃（脈動動畫）、IDLE=綠、WAITING=灰、QUEUED=紫
- 所有 metadata hover 有 i18n 提示文字
- 點擊展開顯示詳細資訊（lazy load，收合清 cache）：
  - Session 時長
  - Token 明細（in / out）
  - 工具使用統計（從 JSONL 解析）
  - 最後活動摘要（支援 Markdown 粗體 + code）
  - 估算費用
- **DOM patching**：資料更新只改變動的 DOM 元素，不重建整頁
  - PID 集合不變 → 局部更新（展開狀態、hover 不受影響）
  - PID 集合變了 → 全部重建，但保留展開狀態

### Usage 頁

- Hero 大數字：Today / This Week / This Month 的 total tokens（含 cache）
- Daily Tokens 長條圖（14 天，Chart.js）
- By Project 進度條（佔 total 比例）
- By Session 表格：title / project / date / output / total / est. cost
  - Total 欄 hover 浮窗顯示 token 類型明細 + 計費說明

### Models 頁

- Hero：Total Tokens / Output Tokens / Est. Cost（30 天）
- Token Distribution 甜甜圈圖（Chart.js）
- 每個 model 一張卡片：
  - Token 明細（output / input / cache_read / cache_write）+ 佔比 %
  - Stacked bar 視覺化
  - 平均每次 request token 數
  - 總 requests 數
  - 估算費用

---

## Web Server（web.py）

### API

| Endpoint | 說明 |
|----------|------|
| `GET /` | HTML 單頁應用 |
| `GET /api/sessions` | 即時 session 列表 |
| `GET /api/history` | 歷史用量統計（60 秒 cache） |
| `GET /api/session/<pid>` | Session 詳細資訊（30 秒 cache，lazy parse JSONL） |

### URL Hash 路由

- `/#sessions`（預設）
- `/#usage`
- `/#models`
- 重新載入會停在當前 tab

### 功能

- **Dark / Light theme**：右上角切換，localStorage 記住
- **i18n**：English / 繁體中文，localStorage 記住
- **Auto-reload**：監控 `*.py` 檔案，有改動自動重啟 server
- **零額外依賴**：Chart.js 透過 CDN 載入

### 部署

```bash
# 前景執行
.venv/bin/python web.py

# systemd 常駐
sudo ln -sf $(pwd)/claude-dashboard-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-dashboard-web
```

---

## 設計決策

| 決策 | 原因 |
|------|------|
| Token 為主、cost 為輔 | Max plan 按月費計費，token pricing 估算僅供參考 |
| PostToolUse 讀 JSONL | hook input 不含 context window 資料 |
| 增量解析 + /tmp cache | 避免每次 hook 都讀完整個大 JSONL |
| 排除 subagents/ | tokens 已計入主 session，避免重複 |
| DOM patching | 避免 2 秒 poll 重建整頁導致閃爍 |
| 原生 http.server | 零額外依賴，夠用 |
| Atomic write（mv） | 避免 race condition 寫出不完整的 JSON |
