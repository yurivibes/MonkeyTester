#!/bin/bash
# Start MonkeyTester if not already running
if ! pgrep -f "python3 app.py 8089" > /dev/null 2>&1; then
    cd "$(dirname "$0")"
    nohup python3 app.py 8089 > /tmp/monkeytester-viewer.log 2>&1 &
fi
