#!/usr/bin/env bash
set -euo pipefail

# Rewrite a node's seed surfaces from a rendered template bundle
# --------------------------------------------------------------

usage() {
    cat <<USAGE
Usage: reseed.sh <path> --bundle=<dir>

Rewrite a node's seed surfaces from a rendered template bundle.

Options:
    --bundle=<dir>    Rendered template bundle to reseed from
    --help|-h         Show this help message
USAGE
    exit 0
}

WORKTREE_DIR=""
BUNDLE=""

for arg in "$@"; do
    case "$arg" in
        --help | -h) usage ;;
        --bundle=*) BUNDLE="${arg#*=}" ;;
        *)
            if [[ -z "$WORKTREE_DIR" ]]; then
                WORKTREE_DIR="$arg"
            fi
            ;;
    esac
done

if [[ -z "$WORKTREE_DIR" ]]; then
    echo "Error: path is required" >&2
    exit 1
fi

if [[ -z "$BUNDLE" || ! -d "$BUNDLE" ]]; then
    echo "Error: --bundle must name a directory" >&2
    exit 1
fi

if [[ ! "$WORKTREE_DIR" = /* ]]; then
    WORKTREE_DIR="$(cd "$WORKTREE_DIR" && pwd)"
fi

# ------ node directory

# derive the node's data directory (mirrors Node.node_dir): the project
# prefix comes from the .worktrees/.project/<branch> cache in the main repo
COMMON_DIR=$(git -C "$WORKTREE_DIR" rev-parse --git-common-dir)
if [[ "$COMMON_DIR" = /* ]]; then
    REPO_ROOT=$(cd "$COMMON_DIR/.." && pwd)
else
    REPO_ROOT=$(cd "$WORKTREE_DIR/$COMMON_DIR/.." && pwd)
fi
BRANCH=$(git -C "$WORKTREE_DIR" rev-parse --abbrev-ref HEAD)
PROJECT="."
PROJECT_FILE="$REPO_ROOT/.worktrees/.project/$BRANCH"
if [[ -f "$PROJECT_FILE" ]]; then
    PROJECT=$(cat "$PROJECT_FILE")
fi
if [[ "$PROJECT" == "." ]]; then
    NODE_DIR="$WORKTREE_DIR/.fractal/$BRANCH"
else
    NODE_DIR="$WORKTREE_DIR/$PROJECT/.fractal/$BRANCH"
fi

if [[ ! -d "$NODE_DIR" ]]; then
    echo "Error: no node data directory at $NODE_DIR" >&2
    exit 1
fi

# ------ seed surfaces

# the bundle's surfaces rewrite the node's: add and overwrite, never delete;
# NODE.md, config.json, memory/, and _template.toml are never touched here
# (Python owns the provenance record)

# steps (the copy set mirrors init.sh: *.md files only)
if [[ -d "$BUNDLE/steps" ]]; then
    mkdir -p "$NODE_DIR/steps"
    for FILE in "$BUNDLE/steps/"*.md; do
        [[ -f "$FILE" ]] || continue
        BASENAME=$(basename "$FILE")
        cp "$FILE" "$NODE_DIR/steps/$BASENAME"
    done
fi

# scripts (mirrors init.sh: regular files, skipping underscore machinery)
if [[ -d "$BUNDLE/scripts" ]]; then
    mkdir -p "$NODE_DIR/scripts"
    for SRC in "$BUNDLE/scripts"/*; do
        [[ -f "$SRC" ]] || continue
        BASENAME=$(basename "$SRC")
        [[ "$BASENAME" == _* ]] && continue
        cp "$SRC" "$NODE_DIR/scripts/$BASENAME"
        chmod +x "$NODE_DIR/scripts/$BASENAME" 2>/dev/null || true
    done
fi

# skills (per skill dir, merged file by file: a node-added file survives)
if [[ -d "$BUNDLE/skills" ]]; then
    for SKILL_SRC in "$BUNDLE/skills"/*/; do
        [[ -d "$SKILL_SRC" ]] || continue
        SKILL_NAME=$(basename "$SKILL_SRC")
        mkdir -p "$NODE_DIR/skills/$SKILL_NAME"
        cp -RL "${SKILL_SRC}." "$NODE_DIR/skills/$SKILL_NAME/"
    done
fi

# ------ agent settings

# per-agent files deploy through the seeding machinery in overwrite mode:
# bundle-carried files rewrite, everything else (the auth and skills
# symlinks included) stands
if [[ -d "$BUNDLE/agents" ]]; then
    fractal node _seed "$NODE_DIR" --bundle="$BUNDLE" --overwrite
fi
