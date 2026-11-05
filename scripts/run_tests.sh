#!/usr/bin/env bash
# Offline-capable test run (stdlib unittest). With dev deps installed, prefer: python -m pytest tests -rs
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -t . -v
