#!/bin/sh
# Start webterm, installing dependencies on first run.
cd "$(dirname "$0")"
python3 -c "import fastapi, uvicorn" 2>/dev/null || pip install -q -r requirements.txt
exec python3 server.py "$@"
