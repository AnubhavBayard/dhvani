#!/usr/bin/env bash
# Push the app to the Hugging Face Space. The index does not travel with it —
# that lives in the dataset repo and is fetched at boot (space_boot.py).
#
#   ./deploy/deploy_space.sh                 # code only
#   ./deploy/deploy_space.sh --secrets       # also push the API keys from .env
#
# Requires `hf auth login` once. The token stays in ~/.cache/huggingface and is
# never read, printed or committed by this script.
set -euo pipefail

SPACE="${DHVANI_SPACE:-Anubhav100/dhvani}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HF="$ROOT/.venv/bin/hf"
cd "$ROOT"

echo "==> $SPACE"

# Code: the package, the UI, the pinned deps, and the two files that only exist
# for the Space. Tests, docs, eval sets and the index stay out — the Space is a
# deployment artifact, not a mirror of the repo.
"$HF" upload "$SPACE" dhvani dhvani --type space --exclude "**/__pycache__/*" \
  --commit-message "deploy: dhvani package"
"$HF" upload "$SPACE" web web --type space \
  --commit-message "deploy: web ui"
"$HF" upload "$SPACE" requirements.txt requirements.txt --type space \
  --commit-message "deploy: pinned requirements"
"$HF" upload "$SPACE" deploy/space/Dockerfile Dockerfile --type space \
  --commit-message "deploy: dockerfile"
"$HF" upload "$SPACE" deploy/space/space_boot.py space_boot.py --type space \
  --commit-message "deploy: boot"
"$HF" upload "$SPACE" deploy/space/README.md README.md --type space \
  --commit-message "deploy: space card"

if [[ "${1:-}" == "--secrets" ]]; then
  # Sourced, not committed: .env is gitignored and stays on this machine. The
  # Space stores these as secrets, which is where they belong on a server.
  set -a; . ./.env; set +a
  "$HF" spaces secrets add "$SPACE" \
    --secrets "SARVAM_API_KEY=$SARVAM_API_KEY" \
    --secrets "GROQ_API_KEY=$GROQ_API_KEY" \
    --secrets "HF_TOKEN=$("$HF" auth token)"
  echo "==> secrets pushed (3)"
fi

echo "==> https://huggingface.co/spaces/$SPACE"
