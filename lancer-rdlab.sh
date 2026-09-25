#!/bin/sh
# Lance rdlab avec le venv local s'il existe, sinon le python du systeme.
cd "$(dirname "$0")" || exit 1
if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python -m rdlab
fi
exec python3 -m rdlab
