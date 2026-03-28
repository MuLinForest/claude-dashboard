#!/bin/sh
# session-writer.sh — PostToolUse hook
# Writes ~/.claude/sessions/<pid>.json for dashboard consumption.
# Reads model/token data from transcript JSONL incrementally (not from hook input,
# which only contains tool metadata — no context window data).

input=$(cat)
SESSIONS_DIR="$HOME/.claude/sessions"
mkdir -p "$SESSIONS_DIR"

_now=$(date +%s)

# ── Parse core fields from hook input ────────────────────────────────────────
_US=$(printf '\037')
_parsed=$(printf '%s' "$input" | jq -r '[
    (.cwd // ""),
    (.session_id // ""),
    (.transcript_path // "")
] | join("\u001f")') || exit 0

IFS="$_US" read -r cwd session_id transcript_path <<EOF
$_parsed
EOF

# ── Find Claude PID (walk up process tree) ───────────────────────────────────
_claude_pid=""
_check="$PPID"
for _i in 1 2 3 4 5 6; do
    _pcomm=$(ps -o comm= -p "$_check" 2>/dev/null | tr -d ' ')
    if [ "$_pcomm" = "claude" ]; then
        _claude_pid="$_check"; break
    fi
    _check=$(ps -o ppid= -p "$_check" 2>/dev/null | tr -d ' ')
    [ -z "$_check" ] || [ "$_check" = "0" ] || [ "$_check" = "1" ] && break
done
[ -z "$_claude_pid" ] && exit 0

# ── Project info ──────────────────────────────────────────────────────────────
project_dir="${cwd:-$(pwd)}"
project_name=$(basename "$project_dir")

# ── Git branch (5s cache) ─────────────────────────────────────────────────────
git_branch=""
_git_cache="/tmp/claude-sw-git-$(id -u)"
if [ -d "$project_dir" ]; then
    _cache_hit=0
    if [ -f "$_git_cache" ]; then
        _c_epoch=$(sed -n '1p' "$_git_cache")
        _c_dir=$(sed -n '2p' "$_git_cache")
        _c_branch=$(sed -n '3p' "$_git_cache")
        if [ "$(( _now - _c_epoch ))" -lt 5 ] && [ "$_c_dir" = "$project_dir" ]; then
            git_branch="$_c_branch"; _cache_hit=1
        fi
    fi
    if [ "$_cache_hit" -eq 0 ]; then
        git_branch=$(git -C "$project_dir" --no-optional-locks symbolic-ref --short HEAD 2>/dev/null)
        printf '%s\n%s\n%s\n' "$_now" "$project_dir" "$git_branch" > "$_git_cache" 2>/dev/null
    fi
fi

# ── Session title ─────────────────────────────────────────────────────────────
_session_title=""
if [ -f "$transcript_path" ]; then
    _session_title=$(grep '"customTitle"' "$transcript_path" 2>/dev/null | tail -1 | \
        jq -r '.customTitle // ""' 2>/dev/null) || _session_title=""
fi

# ── Incremental JSONL parsing for model + tokens ──────────────────────────────
# Cache: <transcript_path>\t<last_line_count>\t<total_input>\t<total_output>\t<model>
model="Unknown"; total_input=0; total_output=0; used_pct=0
_tok_cache="/tmp/claude-sw-tok-$_claude_pid"

if [ -f "$transcript_path" ]; then
    _prev_lines=0; _prev_tin=0; _prev_tout=0; _prev_model="Unknown"

    if [ -f "$_tok_cache" ]; then
        IFS='	' read -r _cached_path _prev_lines _prev_tin _prev_tout _prev_model < "$_tok_cache" 2>/dev/null || true
        # Reset if different transcript (e.g. after /clear or session restart)
        [ "$_cached_path" != "$transcript_path" ] && { _prev_lines=0; _prev_tin=0; _prev_tout=0; _prev_model="Unknown"; }
    fi

    _cur_lines=$(wc -l < "$transcript_path" 2>/dev/null | tr -d ' ') || _cur_lines=0

    if [ "${_cur_lines:-0}" -gt "${_prev_lines:-0}" ]; then
        # Parse only new lines added since last run.
        # tin = last turn's total context (input + cache_read + cache_creation)
        # tout = cumulative output tokens (running total)
        _delta=$(tail -n "+$(( _prev_lines + 1 ))" "$transcript_path" 2>/dev/null | jq -rs '
            [.[] | select(.type == "assistant" and .message.usage != null)] |
            if length == 0 then "0\t0\tUnknown"
            else
                (last) as $last |
                ($last.message.model // "Unknown") as $model |
                ($last.message.usage | (.input_tokens//0) + (.cache_read_input_tokens//0) + (.cache_creation_input_tokens//0)) as $tin |
                (map(.message.usage.output_tokens // 0) | add // 0) as $dtout |
                "\($tin)\t\($dtout)\t\($model)"
            end
        ' 2>/dev/null) || _delta="0	0	Unknown"

        IFS='	' read -r _last_tin _dtout _last_model <<EOF
$_delta
EOF

        # tokens_in = last turn context size (not cumulative sum)
        total_input=${_last_tin:-0}
        total_output=$(( ${_prev_tout:-0} + ${_dtout:-0} ))
        if [ -n "$_last_model" ] && [ "$_last_model" != "Unknown" ]; then
            model="$_last_model"
        else
            model="${_prev_model:-Unknown}"
        fi

        printf '%s\t%s\t%s\t%s\t%s\n' \
            "$transcript_path" "$_cur_lines" "$total_input" "$total_output" "$model" \
            > "$_tok_cache" 2>/dev/null
    else
        total_input=${_prev_tin:-0}
        total_output=${_prev_tout:-0}
        model=${_prev_model:-Unknown}
    fi

    # Estimate context window % (opus=1M, all others=200k)
    _ctx_limit=200000
    case "$model" in *opus*) _ctx_limit=1000000 ;; esac
    used_pct=$(awk -v tin="$total_input" -v lim="$_ctx_limit" \
        'BEGIN{v=tin*100/lim; if(v>100)v=100; printf "%.0f",v}')
fi

# ── Determine status ──────────────────────────────────────────────────────────
_sf="$SESSIONS_DIR/$_claude_pid.json"
_status=""
read -r _status _ < "$SESSIONS_DIR/$_claude_pid.status" 2>/dev/null || _status=""
if [ -z "$_status" ] || [ "$_status" = "null" ]; then
    _prev_tout=0
    [ -f "$_sf" ] && _prev_tout=$(jq -r '.tokens_out // 0' "$_sf" 2>/dev/null) || _prev_tout=0
    if [ "${total_output:-0}" -gt "${_prev_tout:-0}" ] 2>/dev/null; then
        _status="working"
    else
        _status="idle"
    fi
fi

# ── Memory ────────────────────────────────────────────────────────────────────
_mem=$(ps -o rss= -p "$_claude_pid" 2>/dev/null | awk '{printf "%d",$1+0}') || _mem=0

# ── Write session JSON ────────────────────────────────────────────────────────
_tmp=$(mktemp "${_sf}.XXXXXX" 2>/dev/null) || _tmp="${_sf}.tmp.$$"
jq -n \
    --arg pid    "$_claude_pid" \
    --arg epoch  "$_now" \
    --arg model  "${model:-Unknown}" \
    --arg pdir   "${project_dir:-}" \
    --arg pname  "${project_name:-}" \
    --arg branch "${git_branch:-}" \
    --arg status "${_status:-idle}" \
    --arg pct    "${used_pct:-0}" \
    --arg tin    "${total_input:-0}" \
    --arg tout   "${total_output:-0}" \
    --arg mem    "${_mem:-0}" \
    --arg stitle "${_session_title:-}" \
    '{pid:($pid|tonumber),epoch:($epoch|tonumber),model:$model,
      project_dir:$pdir,project_name:$pname,git_branch:$branch,
      status:$status,last_activity:"",
      used_pct:($pct|tonumber),tokens_in:($tin|tonumber),
      tokens_out:($tout|tonumber),mem_kb:($mem|tonumber),
      cost_usd:0,session_title:$stitle}' \
    > "$_tmp" 2>/dev/null && mv "$_tmp" "$_sf" 2>/dev/null || rm -f "$_tmp"
