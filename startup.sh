#!/bin/bash
# Start VisualTest Viewer if not already running
if ! pgrep -f "python3 app.py 8089" > /dev/null 2>&1; then
    cd /home/server/Workspace/VisualTestViewer
    nohup python3 app.py 8089 > /tmp/visualtest-viewer.log 2>&1 &
fi
