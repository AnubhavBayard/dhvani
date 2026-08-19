#!/usr/bin/env bash
# Run ON the VM, once. Turns a bare Ubuntu 24.04 box into the live deployment.
#
#   curl -fsSL https://raw.githubusercontent.com/AnubhavBayard/dhvani/main/deploy/vm/bootstrap.sh | bash
#
# Idempotent: safe to re-run after a failure, and it re-runs cheaply because the
# index download and the venv are both skipped when already present.
set -euo pipefail

REPO="${DHVANI_REPO:-https://github.com/AnubhavBayard/dhvani.git}"
REF="${DHVANI_REF:-main}"
INDEX_REPO="${DHVANI_INDEX_REPO:-Anubhav100/dhvani-index}"
APP_DIR="${DHVANI_APP_DIR:-/opt/dhvani}"
INDEX_DIR="${DHVANI_INDEX_DIR:-/opt/dhvani-index}"
ENV_FILE="/etc/dhvani/env"

say() { printf '\n==> %s\n' "$*"; }

say "packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates debian-keyring \
    debian-archive-keyring apt-transport-https

say "python 3.11 via uv (ADR-013 — same interpreter the measurements were taken on)"
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.11

say "code: $REPO @ $REF"
if [[ -d "$APP_DIR/.git" ]]; then
    sudo git -C "$APP_DIR" fetch --depth 1 origin "$REF"
    sudo git -C "$APP_DIR" reset --hard FETCH_HEAD
else
    sudo mkdir -p "$APP_DIR"
    sudo chown "$USER" "$APP_DIR"
    git clone --depth 1 --branch "$REF" "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

say "venv + pinned wheels"
if [[ ! -x .venv/bin/python ]]; then
    uv venv --python 3.11 .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt

say "index: $INDEX_REPO -> $INDEX_DIR"
sudo mkdir -p "$INDEX_DIR"
sudo chown "$USER" "$INDEX_DIR"
if [[ ! -f "$INDEX_DIR/hnsw_sq8.faiss" ]]; then
    # The dataset repo is private, so this needs HF_TOKEN — set in $ENV_FILE by
    # push_env.sh before this runs, or exported into this shell.
    # shellcheck disable=SC1090
    [[ -f "$ENV_FILE" ]] && set -a && . "$ENV_FILE" && set +a
    .venv/bin/hf download "$INDEX_REPO" --repo-type dataset --local-dir "$INDEX_DIR" \
        --include "hnsw_sq8.faiss" "bm25/*" "chunks.parquet" "phonetic_vocab.json" \
        "manifest.json"
fi

say "arrow store (ADR-033 — 7.42 GB resident becomes 2.96 GB)"
if [[ ! -f "$INDEX_DIR/chunks.arrow" ]]; then
    .venv/bin/python -m dhvani.build.arrow_store --index "$INDEX_DIR"
fi

say "systemd"
sudo install -d -m 750 /etc/dhvani
sudo cp deploy/vm/dhvani.service /etc/systemd/system/dhvani.service
sudo sed -i "s#@APP_DIR@#$APP_DIR#g; s#@INDEX_DIR@#$INDEX_DIR#g; s#@USER@#$USER#g" \
    /etc/systemd/system/dhvani.service
sudo systemctl daemon-reload
sudo systemctl enable --now dhvani

say "caddy (HTTPS on <ip>.sslip.io — getUserMedia needs a secure context)"
if ! command -v caddy >/dev/null; then
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq caddy
fi
IP="$(curl -fsS https://api.ipify.org)"
HOST="${IP//./-}.sslip.io"
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
# sslip.io resolves <dashed-ip>.sslip.io to that IP, so Let's Encrypt can issue
# for it and the browser gets a real certificate — no domain purchase, and the
# microphone works, which it does not over plain http.
$HOST {
    reverse_proxy 127.0.0.1:8000
    encode gzip
}
CADDY
sudo systemctl restart caddy

say "done"
echo "    https://$HOST"
echo "    systemctl status dhvani | caddy"
echo "    journalctl -u dhvani -f"
