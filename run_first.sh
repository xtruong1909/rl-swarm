#!/usr/bin/env bash
set -euo pipefail

ROOT="$PWD"

# ==== 0) Chuẩn hóa log màu ====
GREEN="\033[32m"; BLUE="\033[34m"; RED="\033[31m"; NC="\033[0m"
ok(){ echo -e "${GREEN}$*${NC}"; }
info(){ echo -e "${BLUE}$*${NC}"; }
warn(){ echo -e "${RED}$*${NC}"; }

# ==== 1) Giải phóng port 3000 nếu đang bận ====
info "Kiểm tra port 3000..."
PORT_PID=$(ss -ltnp 2>/dev/null | grep ':3000' | awk -F 'pid=' '{print $2}' | cut -d',' -f1 || true)
if [ -n "${PORT_PID:-}" ]; then
  kill -9 "$PORT_PID" || true
  ok "Đã kill port 3000 (PID=$PORT_PID)."
else
  ok "Port 3000 đang rảnh."
fi

# ==== 2) Biến môi trường cốt lõi ====
GENRL_TAG="0.1.9"
export CONNECT_TO_TESTNET=true
export HF_HUB_DOWNLOAD_TIMEOUT=120
export SWARM_CONTRACT="0xFaD7C5e93f28257429569B854151A1B8DCD404c2"
export PRG_CONTRACT="0x51D4db531ae706a6eC732458825465058fA23a35"
export HUGGINGFACE_ACCESS_TOKEN="None"
export PRG_GAME=true

DEFAULT_IDENTITY_PATH="$ROOT/swarm.pem"
export IDENTITY_PATH="${IDENTITY_PATH:-$DEFAULT_IDENTITY_PATH}"

mkdir -p "$ROOT/logs"

# ==== 3) Chuẩn Node 22 + Yarn (Corepack) ====
if ! command -v node >/dev/null 2>&1; then
  info "Cài Node.js 22..."
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
ok "Node $(node -v)"

# Dùng Corepack để có Yarn ổn định cho server
corepack enable >/dev/null 2>&1 || true
corepack prepare yarn@stable --activate >/dev/null 2>&1 || true
ok "Yarn $(yarn -v)"
# Tắt telemetry cho đỡ ồn
yarn config set --home enableTelemetry 0 >/dev/null 2>&1 || true

# ==== 4) Modal-login: đảm bảo .env và cài deps ====
if [ "${CONNECT_TO_TESTNET}" = true ]; then
  echo "Please login to create an Ethereum Server Wallet"
  cd "$ROOT/modal-login"

  # Tạo .env nếu chưa có; cập nhật 2 biến địa chỉ contract một cách idempotent
  ENV_FILE="$ROOT/modal-login/.env"
  touch "$ENV_FILE"
  if grep -q '^SWARM_CONTRACT_ADDRESS=' "$ENV_FILE"; then
    sed -i "s/^SWARM_CONTRACT_ADDRESS=.*/SWARM_CONTRACT_ADDRESS=${SWARM_CONTRACT}/" "$ENV_FILE"
  else
    echo "SWARM_CONTRACT_ADDRESS=${SWARM_CONTRACT}" >> "$ENV_FILE"
  fi
  if grep -q '^PRG_CONTRACT_ADDRESS=' "$ENV_FILE"; then
    sed -i "s/^PRG_CONTRACT_ADDRESS=.*/PRG_CONTRACT_ADDRESS=${PRG_CONTRACT}/" "$ENV_FILE"
  else
    echo "PRG_CONTRACT_ADDRESS=${PRG_CONTRACT}" >> "$ENV_FILE"
  fi

  # Cho phép Yarn cập nhật lockfile nếu cần (tránh YN0028)
  yarn config set enableImmutableInstalls false >/dev/null 2>&1 || true

  # Fix peer deps viem nếu project yêu cầu 2.29.2
  if grep -q '"viem' package.json 2>/dev/null; then
    # Khóa về 2.29.2 bằng resolutions (ổn định nhất)
    if command -v jq >/dev/null 2>&1; then
      tmp=$(mktemp)
      jq '.resolutions = (.resolutions // {}) | .resolutions["viem@*"]="2.29.2"' package.json > "$tmp" && mv "$tmp" package.json
    else
      # fallback thô nếu thiếu jq
      if ! grep -q '"resolutions"' package.json; then
        sed -i '0,/{/s//{\n  "resolutions": { "viem@*": "2.29.2" },/' package.json
      else
        # chèn khi đã có resolutions
        sed -i 's/"resolutions":[[:space:]]*{[^}]*}/&,\n  "viem@*": "2.29.2"/' package.json
      fi
    fi
  fi

  info "Cài deps modal-login (cho phép update lockfile nếu cần)..."
  yarn install --mode=update-lockfile || yarn install --update-lockfile

  info "Build modal-login..."
  yarn build >> "$ROOT/logs/yarn.log" 2>&1 || true

  ok "Start backend server (modal-login)"
  # Dừng server cũ nếu còn
  pkill -f "yarn start" 2>/dev/null || true
  sleep 1
  yarn start >> "$ROOT/logs/yarn.log" 2>&1 &
  SERVER_PID=$!
  ok "Started server PID: $SERVER_PID"
  sleep 3

  # Nếu chưa có login, mở ngrok để login từ xa
  if ! ls "$ROOT/modal-login/temp-data"/user*.json >/dev/null 2>&1; then
    info "Chưa có modal login. Bật ngrok để login từ xa..."
    if ! command -v ngrok >/dev/null 2>&1; then
      curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
      echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | tee /etc/apt/sources.list.d/ngrok.list >/dev/null
      apt update -y >/dev/null
      apt install -y ngrok >/dev/null
    fi
    if [ ! -f "$HOME/.config/ngrok/ngrok.yml" ]; then
      read -p "Nhập NGROK_TOKEN: " NGROK_TOKEN
      ngrok config add-authtoken "$NGROK_TOKEN"
    fi
    nohup ngrok http 3000 >/dev/null 2>&1 &
    sleep 2
    NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | grep -o '"public_url":"https:[^"]*' | cut -d'"' -f4 || true)
    ok "Mở http://localhost:3000 để login."
    [ -n "$NGROK_URL" ] && ok "URL ngrok: $NGROK_URL"
  else
    ok "Đã có modal login, bỏ qua ngrok."
  fi

  cd "$ROOT"

  info "Đợi tạo modal-login/temp-data/userData.json..."
  # Timeout 10 phút cho chắc
  t_end=$(( $(date +%s) + 600 ))
  while [ ! -f "modal-login/temp-data/userData.json" ]; do
    [ "$(date +%s)" -gt "$t_end" ] && { warn "Hết thời gian chờ tạo userData.json"; break; }
    sleep 5
  done

  if [ -f "modal-login/temp-data/userData.json" ]; then
    ORG_ID=$(awk 'BEGIN { FS="\"" } !/^[ \t]*[{}]/ { print $(NF-1); exit }' modal-login/temp-data/userData.json)
    ok "ORG_ID: $ORG_ID"
    info "Chờ kích hoạt API key..."
    # Chờ tối đa 5 phút
    t_end=$(( $(date +%s) + 300 ))
    while true; do
      STATUS=$(curl -s "http://localhost:3000/api/get-api-key-status?orgId=$ORG_ID" || echo "error")
      if [[ "$STATUS" == "activated" ]]; then
        ok "API key activated!"
        break
      fi
      [ "$(date +%s)" -gt "$t_end" ] && { warn "API key chưa activated sau 5 phút (tiếp tục chạy)"; break; }
      sleep 5
    done
  fi
fi

# ==== 5) Python venv + libs (tránh PEP 668) ====
info "Thiết lập Python venv..."
if [ ! -d "$ROOT/.venv" ]; then
  python3 -m venv "$ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install --upgrade pip setuptools wheel
ok ">> Installing GenRL + deps..."
pip install "gensyn-genrl==${GENRL_TAG}"
pip install "reasoning-gym>=0.1.20"
pip install "hivemind@git+https://github.com/gensyn-ai/hivemind@639c964a8019de63135a2594663b5bec8e5356dd"

# ==== 6) Copy config để tiện debug ====
mkdir -p "$ROOT/configs"
cp -f "$ROOT/rgym_exp/config/rg-swarm.yaml" "$ROOT/configs/rg-swarm.yaml" || true

# ==== 7) Chạy rl-swarm ====
MODEL_NAME="Gensyn/Qwen2.5-0.5B-Instruct"
export MODEL_NAME
export PRG_GAME=true
ok "Model: $MODEL_NAME"
ok "Khởi chạy rl-swarm..."

python3 -m rgym_exp.runner.swarm_launcher \
  --config-path "$ROOT/rgym_exp/config" \
  --config-name "rg-swarm.yaml"

wait
