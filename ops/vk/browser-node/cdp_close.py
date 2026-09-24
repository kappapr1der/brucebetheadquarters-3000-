#!/usr/bin/env python3
import json
import time
import urllib.request

import websocket


with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5) as response:
    version = json.load(response)

ws = websocket.create_connection(
    version["webSocketDebuggerUrl"], timeout=8, suppress_origin=True
)
try:
    ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            message = json.loads(ws.recv())
        except Exception:
            break
        if message.get("id") == 1:
            break
finally:
    ws.close()

print("BROWSER_CLOSE_SENT")
