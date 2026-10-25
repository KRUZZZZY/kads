#!/usr/bin/env bash
# DeepSeek Harness (dsh) for the fix loop (owner 2026-10-20: "run deepseek provided by opencode in the deepseek
# harness instead of the opencode harness"). Usage: run-dsh.sh <workdir> [dsh app args...]; the task comes on stdin
# or as an argument. The OpenCode key is read here from OpenCode's own login (~/.local/share/opencode/auth.json; the copy in ~/.minimax/config.yaml expired) into this process only - never
# printed, never written to the profile. Pushes are blocked: every push URL is rewritten to an invalid one.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export DSH_HOME="$here/home"
OPENCODE_GO_API_KEY="$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.local/share/opencode/auth.json")))["opencode"]["key"])')"
export OPENCODE_GO_API_KEY
# 2026-10-22: OpenDesign, the second DeepSeek provider for the owner's A/B test (key saved by the owner, mode 600)
if [ -r "$HOME/.config/open-design/key" ]; then
  OPENDESIGN_API_KEY="$(tr -d '[:space:]' < "$HOME/.config/open-design/key")"
  export OPENDESIGN_API_KEY
fi
# one stable id per conversation: the caller passes the earlier one to resume, else a fresh one
export ZENE_OPENCODE_SESSION="${ZENE_OPENCODE_SESSION:-zene-$(python3 -c 'import uuid;print(uuid.uuid4())')}"
# GIT_CONFIG_KEY_* would be stripped by the harness (it scrubs credential-shaped names), so pushes are blocked via a
# global config file that includes the user's own and rewrites every push URL
export GIT_CONFIG_GLOBAL="$here/gitconfig"
# 2026-10-20 16:44: IPv6 to opencode.ai stopped working here and IPv4 connects took 0.6-0.9 s, past Node's 250 ms
# per-address happy-eyeballs timeout, so every dsh request failed with "TRANSPORT: Connection error". Prefer IPv4
# and give each address 3 s.
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first --network-family-autoselection-attempt-timeout=3000"
# Owner 2026-10-20 (option 1): dsh's own sandbox can write only under the session cwd, but a linked worktree's
# commits land in the main clone's .git, so its first fix (new-p10-257-port-port) could not commit. The whole harness
# runs in the Haiku lane's Landlock write-jail instead (work tree, its git common dir, /tmp, caches, locks, dsh home),
# with its inner sandbox off.
wt="$1"; shift
common="$(git -C "$wt" rev-parse --path-format=absolute --git-common-dir)"
jail="$HOME/Documents/AI_KOS_PROJECT/projects/zene-studio-program/tools/haiku/jail.py"
export DSH_PERMISSION_MODE=danger-full-access
cd "$wt"
exec python3 "$jail" --rw "$wt" --rw "$common" --rw /tmp --rw "$HOME/.cache" \
  --rw "$HOME/.local/share/zene-bunny-fix/bunny/locks" --rw "$DSH_HOME" \
  -- dsh --profile headless --patch "$here/zene-patch.yml" "$@"
