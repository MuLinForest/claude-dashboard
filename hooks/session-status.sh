#!/bin/sh
# Update session .status file when Claude changes state.
# Format: "<status> <epoch>"
# For "idle", preserves the working epoch so callers can compute elapsed time.
# Called from: Stop (idle), UserPromptSubmit (working), PermissionRequest (waiting)

SESSIONS_DIR="$HOME/.claude/sessions"
STATUS="${1:-idle}"

# Find the Claude PID by traversing up the process tree
_claude_pid=""
_check="$PPID"
for _i in 1 2 3 4 5; do
    _comm=$(ps -o comm= -p "$_check" 2>/dev/null | tr -d ' ')
    if [ "$_comm" = "claude" ]; then
        _claude_pid="$_check"
        break
    fi
    _check=$(ps -o ppid= -p "$_check" 2>/dev/null | tr -d ' ')
    [ -z "$_check" ] || [ "$_check" = "0" ] || [ "$_check" = "1" ] && break
done

[ -z "$_claude_pid" ] && exit 0

_statusfile="$SESSIONS_DIR/${_claude_pid}.status"
_epoch=$(date +%s)

if [ "$STATUS" = "idle" ]; then
    # Preserve the epoch from when "working" was written,
    # so notify-on-stop.sh can compute elapsed working time.
    _working_epoch=""
    if [ -f "$_statusfile" ]; then
        _prev_status="" _prev_epoch=""
        read -r _prev_status _prev_epoch < "$_statusfile" 2>/dev/null || true
        if [ "$_prev_status" = "working" ] && [ -n "$_prev_epoch" ]; then
            _working_epoch="$_prev_epoch"
        fi
    fi
    _epoch="${_working_epoch:-$_epoch}"
fi

printf '%s %s\n' "$STATUS" "$_epoch" > "$_statusfile" 2>/dev/null || true
