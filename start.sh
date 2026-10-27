#!/usr/bin/env bash
# =============================================================================
#  AAE4203 GNSS Benchmark Challenge - Linux / macOS launcher
#  Requires Python 3.11 or newer.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Python 3 was not found. Install Python 3.11 or newer." >&2
    exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11 or newer is required. Found: $("$PY" -V)" >&2
    exit 1
fi

if [ ! -f config.toml ]; then
    echo "config.toml is missing." >&2
    exit 1
fi

exec "$PY" run.py "$@"
