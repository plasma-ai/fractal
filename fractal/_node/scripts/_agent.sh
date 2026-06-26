#!/usr/bin/env bash
set -euo pipefail

# Dispatch a single agent invocation
# ----------------------------------
#
# Usage: _agent.sh <agent-command> <prompt>
#
# Environment:
#   NODE_DIR             working directory for the agent
#   WORKTREE_DIR         worktree path (for fractal _stream)
#   STEP_ID              step row ID (for cost recording)
#   STEP_LIMIT_SECONDS   timeout seconds (0 = no limit)
#   STEP_MODEL           model override (empty = use config default)
#   STEP_BUDGET          per-step USD cost cap (empty = none; claude only)
#   STEP_DETACHED        "true" when this turn is detached (no session weaving)

AGENT_COMMAND="$1"
PROMPT="$2"

read -ra PARTS <<<"$AGENT_COMMAND"
AGENT_BASE_COMMAND="${PARTS[0]}"

# clear any stale budget-exceeded marker; _stream re-creates it if this invocation
# reaches its --max-budget-usd cap (a clean stop, handled in the outcome below)
rm -f "$NODE_DIR/.budget_exceeded"

LAUNCH=()

# timeout wrapper
if [[ "${STEP_LIMIT_SECONDS:-0}" -gt 0 ]]; then
    LAUNCH+=(timeout "$STEP_LIMIT_SECONDS")
fi

# resolve this agent's session id (empty unless a continuous session exists)
SESSION_ID=""
if [[ "${STEP_DETACHED:-false}" != true ]]; then
    SESSION_ID=$(fractal session _get "$AGENT_BASE_COMMAND" \
        --path="$WORKTREE_DIR" 2>/dev/null || true)
fi

# resolve the agent's own configured default model, for the session record --
# claude reads the worktree's .claude/settings.json then ~/.claude/settings.json,
# codex reads ~/.codex/config.toml; empty when neither configures one
agent_config_model() {
    local AGENT="$1" FILE
    if [[ "$AGENT" == "claude" ]]; then
        for FILE in "$WORKTREE_DIR/.claude/settings.json" "$HOME/.claude/settings.json"; do
            [[ -f "$FILE" ]] || continue
            python3 -c 'import json, sys
model = json.load(open(sys.argv[1])).get("model", "")
print(model if isinstance(model, str) else "")' "$FILE" 2>/dev/null && return
        done
    elif [[ "$AGENT" == "codex" && -f "$HOME/.codex/config.toml" ]]; then
        sed -n 's/^model[[:space:]]*=[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' \
            "$HOME/.codex/config.toml" | head -1
    fi
}

# the model recorded with the woven session: the explicit step model, else the
# agent's own configured default -- the launch below still passes only the
# explicit one (the agent already applies its own config itself)
RECORD_MODEL="${STEP_MODEL:-}"
if [[ -z "$RECORD_MODEL" ]]; then
    RECORD_MODEL=$(agent_config_model "$AGENT_BASE_COMMAND" || true)
fi

# fractal _stream args (shared); --detached tells _stream not to persist the session
STREAM_ARGS=(--agent="$AGENT_BASE_COMMAND")
if [[ -n "$RECORD_MODEL" ]]; then
    STREAM_ARGS+=(--model="$RECORD_MODEL")
fi
if [[ "${STEP_DETACHED:-false}" == true ]]; then
    STREAM_ARGS+=(--detached)
fi

# pass the step id only when present; an empty positional makes _stream's typer
# int-parse fail (exit 2) and discards the agent output, whereas an omitted one
# uses the null default; left unset (not =()) so the +-expansion below is safe
# under set -u even on bash 3.2 (empty-array expand would error)
if [[ -n "${STEP_ID:-}" ]]; then
    STREAM_STEP=("$STEP_ID")
fi

# the agent's and the downstream _stream consumer's exit statuses, captured
# separately (via PIPESTATUS) so a step failure can be attributed accurately:
# an agent error vs a fractal-side _stream (output parse / cost record) error
AGENT_STATUS=0
STREAM_STATUS=0
ERR_FILE=""

# ------ claude

if [[ "$AGENT_BASE_COMMAND" == "claude" ]]; then
    LAUNCH+=(
        "${PARTS[@]}"
        -p "$PROMPT"
        --output-format stream-json
        --include-partial-messages
        --verbose
    )
    if [[ -n "${STEP_MODEL:-}" ]]; then
        LAUNCH+=(--model "$STEP_MODEL")
    fi
    # enforce the per-step USD budget when set (computed in _run.sh); claude stops
    # mid-turn and emits a result subtype error_max_budget_usd on reaching it
    if [[ -n "${STEP_BUDGET:-}" ]]; then
        LAUNCH+=(--max-budget-usd "$STEP_BUDGET")
    fi
    # resume the session, or start a new one (caller-generated id)
    if [[ "${STEP_DETACHED:-false}" != true ]]; then
        if [[ -n "$SESSION_ID" ]]; then
            LAUNCH+=(--resume "$SESSION_ID")
        else
            LAUNCH+=(--session-id "$(uuidgen | tr '[:upper:]' '[:lower:]')")
        fi
    fi
    ERR_FILE="$NODE_DIR/claude.err"
    # ensure the per-node credential link exists before launch -- init.sh seeds it
    # for new nodes, but this retrofits nodes initialized before CLAUDE_CONFIG_DIR
    # relocation (whose .claude has no credential), so they don't lose auth on the
    # next run. Best-effort: only when a global credential exists and no link is
    # present; auth via ANTHROPIC_API_KEY needs no file and is left untouched
    if [[ ! -L "$NODE_DIR/.claude/.credentials.json" ]]; then
        GLOBAL_CLAUDE_AUTH="$HOME/.claude/.credentials.json"
        if [[ -e "$GLOBAL_CLAUDE_AUTH" ]]; then
            mkdir -p "$NODE_DIR/.claude"
            ln -s "$(readlink -f "$GLOBAL_CLAUDE_AUTH")" \
                "$NODE_DIR/.claude/.credentials.json"
        fi
    fi
    # capture stderr straight to a log so auth/startup failures are not hidden --
    # a direct redirect (not an un-awaited `tee` process substitution) can't be
    # truncated when a fast-failing launch exits before the tee flushes; the script
    # exits right after, so cd-ing here is harmless; `set +e` lets us read each
    # pipe stage's status from PIPESTATUS instead of aborting on failure
    cd "$NODE_DIR"
    set +e
    # CLAUDE_CONFIG_DIR relocates claude's config (settings/auth) and session
    # transcripts under the node dir -- on the persisted worktree, not the
    # ephemeral home, so they survive container recreation (mirrors CODEX_HOME);
    # the cwd stays the node dir, so claude's projects slug is unchanged
    CLAUDE_CONFIG_DIR="$NODE_DIR/.claude" "${LAUNCH[@]}" 2>"$ERR_FILE" \
        | fractal _stream ${STREAM_STEP[@]+"${STREAM_STEP[@]}"} \
            --path="$WORKTREE_DIR" "${STREAM_ARGS[@]}"
    # copy PIPESTATUS in one shot -- a simple assignment resets it, so reading
    # [0] then [1] separately would leave [1] unbound under set -u
    PIPE_STATUS=("${PIPESTATUS[@]}")
    AGENT_STATUS=${PIPE_STATUS[0]}
    STREAM_STATUS=${PIPE_STATUS[1]}
    set -e

# ------ codex

elif [[ "$AGENT_BASE_COMMAND" == "codex" ]]; then
    # resume the thread, or start a new one (codex mints the id; _stream captures it)
    if [[ -n "$SESSION_ID" ]]; then
        # exec resume uses shell cwd (no --cd flag)
        LAUNCH+=("${PARTS[@]}" exec resume "$SESSION_ID" --json)
    else
        LAUNCH+=("${PARTS[@]}" exec -C "$WORKTREE_DIR" --json)
    fi
    if [[ -n "${STEP_MODEL:-}" ]]; then
        LAUNCH+=(-m "$STEP_MODEL")
    fi
    LAUNCH+=("$PROMPT")
    ERR_FILE="$NODE_DIR/codex.err"
    # run in the worktree (the project): CODEX_HOME supplies config/auth/skills,
    # so the cwd is the project not the node dir (exec resume has no -C, uses cwd);
    # capture stderr with a direct redirect (see claude) so a fast-failing launch
    # can't truncate the log
    cd "$WORKTREE_DIR"
    set +e
    CODEX_HOME="$NODE_DIR/.codex" "${LAUNCH[@]}" 2>"$ERR_FILE" \
        | fractal _stream ${STREAM_STEP[@]+"${STREAM_STEP[@]}"} \
            --path="$WORKTREE_DIR" "${STREAM_ARGS[@]}"
    # copy PIPESTATUS in one shot -- a simple assignment resets it, so reading
    # [0] then [1] separately would leave [1] unbound under set -u
    PIPE_STATUS=("${PIPESTATUS[@]}")
    AGENT_STATUS=${PIPE_STATUS[0]}
    STREAM_STATUS=${PIPE_STATUS[1]}
    set -e
fi

# ------ attribute the outcome

# clear any stale marker; _run.sh reads it to label a failed step/iter honestly
rm -f "$NODE_DIR/.fail_reason"
STATUS=0
if [[ "$AGENT_STATUS" -ne 0 ]] && [[ -f "$NODE_DIR/.budget_exceeded" ]]; then
    # the agent stopped because it reached its per-step --max-budget-usd cap
    # (claude exits non-zero + emits result subtype error_max_budget_usd, which
    # _stream marked) -- a clean budget stop, not a failure; the loop's boundary
    # cost checks decide whether to wind the run down
    rm -f "$NODE_DIR/.budget_exceeded"
    echo "reached per-step budget (--max-budget-usd); stopping this step cleanly" >&2
    STATUS=0
elif [[ "$AGENT_STATUS" -ne 0 ]]; then
    # the agent itself failed (124 = timed out, else agent error) -- propagate its
    # code so _run.sh derives the reason from it, and surface the captured stderr
    STATUS=$AGENT_STATUS
    if [[ -n "$ERR_FILE" && -s "$ERR_FILE" ]]; then
        cat "$ERR_FILE" >&2
    fi
elif [[ "$STREAM_STATUS" -ne 0 ]]; then
    # the agent succeeded but fractal _stream failed -- a fractal-side error, not
    # the agent's; mark it so node activity records "stream error", not "agent error"
    echo "stream error" >"$NODE_DIR/.fail_reason"
    STATUS="$STREAM_STATUS"
fi

exit "$STATUS"
