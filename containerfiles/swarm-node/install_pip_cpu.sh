#!/bin/bash

set +euo pipefail
# shellcheck disable=SC1090
source ~/.profile
set -euo pipefail

pip install --upgrade pip

pip install --user -r ./requirements-cpu.txt
