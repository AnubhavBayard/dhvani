#!/usr/bin/env bash
# Copy the API keys to the VM without them touching git, a chat window, or a
# world-readable file. Run from the repo root on the dev box:
#
#   ./deploy/vm/push_env.sh azureuser@<ip>
set -euo pipefail
TARGET="${1:?usage: push_env.sh user@host}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

set -a; . ./.env; set +a
HF_TOKEN="$(.venv/bin/hf auth token)"

# Written by root, 600, in one shot — the keys never land on the box's disk in a
# readable state, and the heredoc keeps them off the process list.
ssh "$TARGET" 'sudo install -d -m 750 /etc/dhvani && sudo tee /etc/dhvani/env >/dev/null && sudo chmod 600 /etc/dhvani/env' <<ENV
SARVAM_API_KEY=$SARVAM_API_KEY
GROQ_API_KEY=$GROQ_API_KEY
HF_TOKEN=$HF_TOKEN
ENV
echo "==> keys installed at /etc/dhvani/env on $TARGET"
