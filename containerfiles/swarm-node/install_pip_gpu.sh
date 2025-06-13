#!/bin/bash

set +euo pipefail
# shellcheck disable=SC1090
source ~/.profile
set -euo pipefail

pip install --upgrade pip

pip install --user -r ./requirements-gpu.txt
pip install flash-attn --no-build-isolation
