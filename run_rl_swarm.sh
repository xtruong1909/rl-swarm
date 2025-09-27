#!/bin/bash
set -euo pipefail

ROOT=$PWD
GENRL_TAG="v0.1.8"

export IDENTITY_PATH
export GENSYN_RESET_CONFIG
export CONNECT_TO_TESTNET=true
export ORG_ID
export HF_HUB_DOWNLOAD_TIMEOUT=120  # 2 minutes
export SWARM_CONTRACT="0xFaD7C5e93f28257429569B854151A1B8DCD404c2"
export PRG_CONTRACT="0x51D4db531ae706a6eC732458825465058fA23a35"
export HUGGINGFACE_ACCESS_TOKEN="None"
export PRG_GAME=true

# Path to an RSA private key. If this path does not exist, a new key pair will be created.
# Remove this file if you want a new PeerID.
DEFAULT_IDENTITY_PATH="$ROOT"/swarm.pem
IDENTITY_PATH=${IDENTITY_PATH:-$DEFAULT_IDENTITY_PATH}

DOCKER=${DOCKER:-""}
GENSYN_RESET_CONFIG=${GENSYN_RESET_CONFIG:-""}
CPU_ONLY=true

# Set if successfully parsed from modal-login/temp-data/userData.json.
ORG_ID=${ORG_ID:-""}

GREEN_TEXT="\033[32m"
BLUE_TEXT="\033[34m"
RED_TEXT="\033[31m"
RESET_TEXT="\033[0m"

echo_green() { echo -e "$GREEN_TEXT$1$RESET_TEXT"; }
echo_blue() { echo -e "$BLUE_TEXT$1$RESET_TEXT"; }
echo_red() { echo -e "$RED_TEXT$1$RESET_TEXT"; }

ROOT_DIR="$(cd $(dirname "${BASH_SOURCE[0]}") && pwd)"
mkdir -p "$ROOT/logs"

if [ "$CONNECT_TO_TESTNET" = true ]; then
    echo_green ">> Restarting with existing setup..."
    cd modal-login

    # Kill any existing server process
    pkill -f "yarn start" || true
    sleep 2

    ENV_FILE="$ROOT/modal-login/.env"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS version
        sed -i '' "3s/.*/SWARM_CONTRACT_ADDRESS=$SWARM_CONTRACT/" "$ENV_FILE"
        sed -i '' "4s/.*/PRG_CONTRACT_ADDRESS=$PRG_CONTRACT/" "$ENV_FILE"

    else
        # Linux version
        sed -i "3s/.*/SWARM_CONTRACT_ADDRESS=$SWARM_CONTRACT/" "$ENV_FILE"
        sed -i "4s/.*/PRG_CONTRACT_ADDRESS=$PRG_CONTRACT/" "$ENV_FILE"
    fi

    # Check if userData.json exists
    if [ ! -f "temp-data/userData.json" ]; then
        echo_red ">> userData.json not found! Please run full setup first."
        exit 1
    fi

    echo_green ">> Starting backend server (modal-login)"
    yarn start >> "$ROOT/logs/yarn.log" 2>&1 &
    SERVER_PID=$!
    echo "Started server process: $SERVER_PID"
    sleep 5

    cd "$ROOT"

    echo_green ">> Using existing userData.json..."
    ORG_ID=$(awk 'BEGIN { FS = "\"" } !/^[ \t]*[{}]/ { print $(NF - 1); exit }' modal-login/temp-data/userData.json)
    echo "ORG_ID: $ORG_ID"

    # Check API key status
    echo "Checking API key status..."
    STATUS=$(curl -s "http://localhost:3000/api/get-api-key-status?orgId=$ORG_ID" || echo "error")
    if [[ "$STATUS" == "activated" ]]; then
        echo_green "API key is already activated!"
    else
        echo_blue "Waiting for API key activation..."
        while true; do
            STATUS=$(curl -s "http://localhost:3000/api/get-api-key-status?orgId=$ORG_ID" || echo "error")
            if [[ "$STATUS" == "activated" ]]; then
                echo_green "API key activated!"
                break
            else
                echo "Waiting for API key activation..."
                sleep 5
            fi
        done
    fi
fi

# Ensure configs directory and copy config
if [ ! -d "$ROOT/configs" ]; then
    mkdir "$ROOT/configs"
fi

cp "$ROOT/rgym_exp/config/rg-swarm.yaml" "$ROOT/configs/rg-swarm.yaml"

if [ -n "$DOCKER" ]; then
    sudo chmod -R 0777 /home/gensyn/rl_swarm/configs
fi

MODEL_NAME="Gensyn/Qwen2.5-0.5B-Instruct"
echo_green ">> Using model: $MODEL_NAME"
export MODEL_NAME
export PRG_GAME=true
echo_green ">> Starting rl-swarm..."

python3 -m rgym_exp.runner.swarm_launcher \
    --config-path "$ROOT/rgym_exp/config" \
    --config-name "rg-swarm.yaml"

wait
