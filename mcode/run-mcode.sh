#!/usr/bin/env bash
# MiniMax Code (mcode) for the fix loop (owner 2026-10-20: "just move all of it [MiniMax M3.1 Flash] to mcode").
# Usage: run-mcode.sh <workdir> <model-id> [mcode exec args...]; the task comes on stdin. Called by
# qwen/opencode_free.run_model_mcode for models listed in qwen/MCODE-HARNESS.json.
# - its own data dir (fff the only MCP server, no skills/plugins): the owner's ~/.minimax setup cost 148k input tokens
#   a turn; this one ~11k
# - the OpenCode Go key is read from OpenCode's own login into this process only - never printed or written
# - pushes are blocked (the same push-URL-rewriting git config as the dsh launcher)
# - Node prefers IPv4 with 3 s per address (the 2026-10-20 outage: slow IPv4 + dead IPv6 vs Node's 250 ms default)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export MINIMAX_DATA_DIR="$here/data"
OPENCODE_GO_API_KEY="$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.local/share/opencode/auth.json")))["opencode"]["key"])')"
export OPENCODE_GO_API_KEY
export ZENE_OPENCODE_SESSION="${ZENE_OPENCODE_SESSION:-zene-$(python3 -c 'import uuid;print(uuid.uuid4())')}"
export GIT_CONFIG_GLOBAL="$HOME/.local/share/zene-bunny-fix/dsh/gitconfig"
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first --network-family-autoselection-attempt-timeout=3000"
wt="$1"; model="$2"; shift 2
cd "$wt"
exec "$HOME/.minimax-code/bin/mcode" exec --config "$here/zene-mcode.yaml" --model "custom_provider:opencode-go/$model" \
  --permission full --prompt-mode coding --cwd "$wt" --output-format stream-json --input - "$@"
