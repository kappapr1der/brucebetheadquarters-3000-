#!/usr/bin/env python3
import json
import sys
import time
import urllib.request

import websocket


CDP_BASE = "http://127.0.0.1:9222"


def get_json(path):
    with urllib.request.urlopen(CDP_BASE + path, timeout=5) as response:
        return json.load(response)


def choose_page():
    pages = [item for item in get_json("/json/list") if item.get("type") == "page"]
    if not pages:
        raise RuntimeError("no CDP page target")
    return pages[0]


def send(ws, message_id, method, params=None):
    payload = {"id": message_id, "method": method}
    if params is not None:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == message_id:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("result", {})


def main():
    target = choose_page()
    ws = websocket.create_connection(
        target["webSocketDebuggerUrl"], timeout=8, suppress_origin=True
    )
    try:
        send(ws, 1, "Runtime.enable")
        expression = r"""
(() => {
  const bodyText = document.body ? document.body.innerText.slice(0, 200000) : '';
  const path = location.pathname || '/';
  const loginForm = !!document.querySelector(
    'input[name="email"], input[name="login"], input[type="password"], form[action*="login"]'
  );
  const challenge = /(^|\/)challenge(?:\.html)?(?:\/|$)/i.test(path) ||
    /captcha|подтвердите, что вы не робот|security check/i.test(bodyText);
  const logoutMarker = !!document.querySelector(
    'a[href*="act=logout"], a[href*="/logout"]'
  );
  const accountMarker = !!document.querySelector(
    '#top_profile_link, .TopNavBtn__profile, .TopNavBtn--profile, [data-testid*="profile"], [aria-label*="профил" i]'
  );
  const authenticatedPath = /^\/(feed|im|friends|groups|albums|video|audios|id\d+)(?:\/|$)/i.test(path);
  const authenticatedUiText = /(^|\n)(моя страница|новости|мессенджер|друзья|сообщества)(\n|$)/i.test(bodyText);
  return {
    host: location.host,
    path,
    ready_state: document.readyState,
    challenge,
    login_form: loginForm,
    logout_marker: logoutMarker,
    account_marker: accountMarker,
    authenticated_path: authenticatedPath,
    authenticated_ui_text: authenticatedUiText,
    likely_authenticated: !challenge && !loginForm &&
      (logoutMarker || accountMarker || authenticatedPath || authenticatedUiText)
  };
})()
"""
        result = send(
            ws,
            2,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        value = result.get("result", {}).get("value")
        if not isinstance(value, dict):
            raise RuntimeError("session probe returned no object")
        value["observed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(json.dumps(value, ensure_ascii=True, sort_keys=True))
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"probe_error": str(exc)}, ensure_ascii=True, sort_keys=True))
        sys.exit(1)
