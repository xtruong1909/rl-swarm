#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GREEN="\033[32m"; BLUE="\033[34m"; RED="\033[31m"; NC="\033[0m"
ok(){ echo -e "${GREEN}$*${NC}"; }
info(){ echo -e "${BLUE}$*${NC}"; }
warn(){ echo -e "${RED}$*${NC}"; }

info "===> Bắt đầu khởi chạy rl-swarm từ: $ROOT"

GENRL_TAG="0.1.9"
export CONNECT_TO_TESTNET=true
export HF_HUB_DOWNLOAD_TIMEOUT=120
export SWARM_CONTRACT="0xFaD7C5e93f28257429569B854151A1B8DCD404c2"
export PRG_CONTRACT="0x51D4db531ae706a6eC732458825465058fA23a35"
export HUGGINGFACE_ACCESS_TOKEN="None"
export PRG_GAME=true

export IDENTITY_PATH="$ROOT/swarm.pem"
mkdir -p "$ROOT/logs"

# ===================== Node + Yarn =====================
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
corepack enable >/dev/null 2>&1 || true
corepack prepare yarn@stable --activate >/dev/null 2>&1 || true
yarn config set --home enableTelemetry 0 >/dev/null 2>&1 || true

ok "Node $(node -v), Yarn $(yarn -v)"

# ===================== Modal Login =====================
cd "$ROOT/modal-login"

touch .env
grep -q '^SWARM_CONTRACT_ADDRESS=' .env && \
  sed -i "s/^SWARM_CONTRACT_ADDRESS=.*/SWARM_CONTRACT_ADDRESS=${SWARM_CONTRACT}/" .env \
  || echo "SWARM_CONTRACT_ADDRESS=${SWARM_CONTRACT}" >> .env
grep -q '^PRG_CONTRACT_ADDRESS=' .env && \
  sed -i "s/^PRG_CONTRACT_ADDRESS=.*/PRG_CONTRACT_ADDRESS=${PRG_CONTRACT}/" .env \
  || echo "PRG_CONTRACT_ADDRESS=${PRG_CONTRACT}" >> .env

yarn config set enableImmutableInstalls false >/dev/null 2>&1 || true
yarn install --mode=update-lockfile || yarn install --update-lockfile
yarn build >> "$ROOT/logs/yarn.log" 2>&1 || true

pkill -f "yarn start" 2>/dev/null || true
yarn start >> "$ROOT/logs/yarn.log" 2>&1 &
SERVER_PID=$!
ok "Khởi chạy backend PID=$SERVER_PID"

sleep 3

# ===================== Kiểm tra file key =====================
info "Kiểm tra key trong modal-login/temp-data..."
mkdir -p "$ROOT/modal-login/temp-data"
cd "$ROOT"

ORG_ID=""
if [ -f "$ROOT/modal-login/temp-data/userData.json" ]; then
  if command -v jq >/dev/null 2>&1; then
    ORG_ID=$(jq -r '.orgId // .organizationId // empty' "$ROOT/modal-login/temp-data/userData.json" || true)
  fi
  [ -z "$ORG_ID" ] && ORG_ID=$(awk 'BEGIN{FS="\""} /orgId|organizationId/ {for(i=1;i<=NF;i++){if($i=="orgId"||$i=="organizationId"){print $(i+2); exit}}}' "$ROOT/modal-login/temp-data/userData.json" || true)
fi

if [ -z "$ORG_ID" ] && [ -f "$ROOT/modal-login/temp-data/apikey.json" ]; then
  if command -v jq >/dev/null 2>&1; then
    ORG_ID=$(jq -r '.orgId // .organizationId // empty' "$ROOT/modal-login/temp-data/apikey.json" || true)
  fi
  [ -z "$ORG_ID" ] && ORG_ID=$(awk 'BEGIN{FS="\""} /orgId|organizationId/ {for(i=1;i<=NF;i++){if($i=="orgId"||$i=="organizationId"){print $(i+2); exit}}}' "$ROOT/modal-login/temp-data/apikey.json" || true)
fi

if [ -n "$ORG_ID" ]; then
  ok "ORG_ID: $ORG_ID"
  info "Chờ xác nhận API key kích hoạt..."
  t_end=$(( $(date +%s) + 300 ))
  while true; do
    STATUS=$(curl -s "http://localhost:3000/api/get-api-key-status?orgId=$ORG_ID" || echo "error")
    if [[ "$STATUS" == "activated" || "$STATUS" == "error" ]]; then
      ok "API key đã sẵn sàng hoặc không yêu cầu kích hoạt thêm."
      break
    fi
    [ "$(date +%s)" -gt "$t_end" ] && { warn "API key chưa kích hoạt sau 5 phút"; break; }
    sleep 5
  done
else
  warn "Không tìm thấy ORG_ID trong userData.json hoặc apikey.json — bỏ qua kích hoạt."
fi

# ===================== Python venv =====================
cd "$ROOT"
if [ ! -d "$ROOT/.venv" ]; then
  python3 -m venv "$ROOT/.venv"
fi
source "$ROOT/.venv/bin/activate"
pip install --upgrade pip setuptools wheel
pip install "gensyn-genrl==${GENRL_TAG}"
pip install "reasoning-gym>=0.1.20"
pip install "hivemind@git+https://github.com/gensyn-ai/hivemind@639c964a8019de63135a2594663b5bec8e5356dd"

mkdir -p "$ROOT/configs"
cp -f "$ROOT/rgym_exp/config/rg-swarm.yaml" "$ROOT/configs/rg-swarm.yaml" || true

MODEL_NAME="Gensyn/Qwen2.5-0.5B-Instruct"
export MODEL_NAME
ok "Chạy swarm launcher với model: $MODEL_NAME"

python3 -m rgym_exp.runner.swarm_launcher \
  --config-path "$ROOT/rgym_exp/config" \
  --config-name "rg-swarm.yaml"
