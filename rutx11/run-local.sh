#!/usr/bin/env bash
# Run the network page on this machine (Linux / WSL / macOS): creates .venv on first use,
# installs the package in editable mode, then serves http://localhost:5080 (no login, loopback
# only) and asks for the router's root password. Extra arguments go to rover-netui, e.g.
#   ./run-local.sh --open            # also open the browser
#   ./run-local.sh --demo --open     # no router needed: simulated RUTX11
#   ./run-local.sh --router 192.168.77.1
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/rover-netui ]; then
  echo "Creating .venv and installing rover-netui ..."
  rm -rf .venv
  # Some sourced ROS setups export PYTHONPATH; keep it out of the venv.
  if command -v uv >/dev/null 2>&1; then
    env -u PYTHONPATH uv venv --quiet --python "$PY" .venv
    env -u PYTHONPATH VIRTUAL_ENV="$PWD/.venv" uv pip install --quiet -e .
  elif env -u PYTHONPATH "$PY" -m venv .venv; then
    env -u PYTHONPATH .venv/bin/pip install --quiet --upgrade pip
    env -u PYTHONPATH .venv/bin/pip install --quiet -e .
  else
    rm -rf .venv
    echo "Cannot create a virtualenv: install python3-venv (sudo apt install python3-venv)" >&2
    echo "or uv (https://docs.astral.sh/uv/), then run this again." >&2
    exit 1
  fi
fi
exec env -u PYTHONPATH .venv/bin/rover-netui "$@"
