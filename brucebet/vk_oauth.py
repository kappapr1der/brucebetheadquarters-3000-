from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
import hashlib
import json
import os
from pathlib import Path
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


# VK ID applications use OAuth 2.1 with PKCE.  The older oauth.vk.com flow
# rejects these application IDs with "Security Error".
VK_ID_AUTHORIZE_URL = "https://id.vk.com/authorize"
VK_ID_TOKEN_URL = "https://id.vk.com/oauth2/auth"


class VkOAuthConfigurationError(RuntimeError):
    pass


class VkOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class VkOAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    credentials_path: Path
    state_path: Path
    state_ttl_minutes: int
    api_version: str
    worker_url: str = ""
    worker_relay_secret: str = ""
    worker_timeout_seconds: int = 20

    @classmethod
    def from_env(cls) -> "VkOAuthSettings":
        data_dir = Path(os.getenv("BRUCEBET_DATA_DIR", "data"))
        client_id = os.getenv("VK_OAUTH_CLIENT_ID", "").strip()
        client_secret = os.getenv("VK_OAUTH_CLIENT_SECRET", "").strip()
        redirect_uri = os.getenv("VK_OAUTH_REDIRECT_URI", "").strip()
        if not client_id or not redirect_uri:
            raise VkOAuthConfigurationError(
                "VK OAuth needs VK_OAUTH_CLIENT_ID and VK_OAUTH_REDIRECT_URI"
            )
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "https" or not parsed.netloc:
            raise VkOAuthConfigurationError("VK_OAUTH_REDIRECT_URI must be a public HTTPS URL")
        worker_url = os.getenv("VK_OAUTH_WORKER_URL", "").strip().rstrip("/")
        worker_relay_secret = os.getenv("VK_OAUTH_WORKER_RELAY_SECRET", "").strip()
        if worker_url:
            worker_parsed = urlparse(worker_url)
            if worker_parsed.scheme != "https" or not worker_parsed.netloc or worker_parsed.query or worker_parsed.fragment:
                raise VkOAuthConfigurationError("VK_OAUTH_WORKER_URL must be a public HTTPS Worker URL")
            if not worker_relay_secret:
                raise VkOAuthConfigurationError("VK_OAUTH_WORKER_RELAY_SECRET is required with VK_OAUTH_WORKER_URL")
            expected_redirect = f"{worker_url}/vk/oauth/callback"
            if redirect_uri.rstrip("/") != expected_redirect:
                raise VkOAuthConfigurationError(
                    "VK_OAUTH_REDIRECT_URI must be the Worker callback URL when VK_OAUTH_WORKER_URL is set"
                )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            credentials_path=Path(os.getenv("VK_OAUTH_CREDENTIALS_PATH", str(data_dir / "vk_oauth_credentials.json"))),
            state_path=Path(os.getenv("VK_OAUTH_STATE_PATH", str(data_dir / "vk_oauth_state.json"))),
            state_ttl_minutes=max(5, int(os.getenv("VK_OAUTH_STATE_TTL_MINUTES", "15"))),
            api_version=os.getenv("VK_API_VERSION", "5.199").strip() or "5.199",
            worker_url=worker_url,
            worker_relay_secret=worker_relay_secret,
            worker_timeout_seconds=max(5, int(os.getenv("VK_OAUTH_WORKER_TIMEOUT_SECONDS", "20"))),
        )


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def create_authorization_url(settings: VkOAuthSettings, *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    _write_private_json(
        settings.state_path,
        {
            "state_sha256": hashlib.sha256(state.encode("utf-8")).hexdigest(),
            "expires_at": (current + timedelta(minutes=settings.state_ttl_minutes)).isoformat(),
            "worker_state": state,
            "code_verifier": code_verifier,
        },
    )
    query = urlencode(
        {
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "s256",
        }
    )
    return f"{VK_ID_AUTHORIZE_URL}?{query}"


def _validate_state(settings: VkOAuthSettings, candidate: str, *, now: datetime | None = None) -> bool:
    stored = _read_json(settings.state_path)
    expected_hash = str(stored.get("state_sha256", ""))
    expires_at = str(stored.get("expires_at", ""))
    if not expected_hash or not expires_at or not candidate:
        return False
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    if expires.tzinfo is None or expires < current:
        return False
    return secrets.compare_digest(expected_hash, hashlib.sha256(candidate.encode("utf-8")).hexdigest())


def exchange_authorization_code(
    settings: VkOAuthSettings,
    code: str,
    *,
    state: str,
    device_id: str,
    code_verifier: str,
    opener: Callable[..., object] = urlopen,
) -> dict[str, object]:
    query = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "code_verifier": code_verifier,
            "state": state,
            "device_id": device_id,
        }
    )
    request = Request(
        f"{VK_ID_TOKEN_URL}?{query}",
        data=urlencode({"code": code}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise VkOAuthError(f"VK token exchange failed: {exc}") from exc
    if not isinstance(payload, dict) or not str(payload.get("access_token", "")).strip():
        detail = str(payload.get("error_description") or payload.get("error") or "no access token") if isinstance(payload, dict) else "invalid response"
        raise VkOAuthError(f"VK token exchange failed: {detail}")
    return payload


def complete_authorization(settings: VkOAuthSettings, *, code: str, state: str, device_id: str) -> None:
    if not _validate_state(settings, state):
        raise VkOAuthError("OAuth state is invalid or has expired; run /vk_connect again")
    stored = _read_json(settings.state_path)
    code_verifier = str(stored.get("code_verifier", "")).strip()
    if not code_verifier:
        raise VkOAuthError("OAuth PKCE verifier is missing; run /vk_connect again")
    if not device_id:
        raise VkOAuthError("VK did not return a device ID; run /vk_connect again")
    payload = exchange_authorization_code(
        settings,
        code,
        state=state,
        device_id=device_id,
        code_verifier=code_verifier,
    )
    _write_private_json(
        settings.credentials_path,
        {
            "access_token": str(payload["access_token"]),
            "user_id": payload.get("user_id"),
            "refresh_token": payload.get("refresh_token"),
            "expires_in": payload.get("expires_in"),
            "device_id": device_id,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    try:
        settings.state_path.unlink()
    except FileNotFoundError:
        pass


def _pending_worker_state(settings: VkOAuthSettings) -> str:
    stored = _read_json(settings.state_path)
    candidate = str(stored.get("worker_state", "")).strip()
    return candidate if _validate_state(settings, candidate) else ""


def complete_worker_relay_authorization(
    settings: VkOAuthSettings,
    *,
    opener: Callable[..., object] = urlopen,
) -> bool:
    """Exchange a short-lived code fetched from the optional Worker relay."""

    if not settings.worker_url:
        return False
    state = _pending_worker_state(settings)
    if not state:
        return False

    request = Request(
        f"{settings.worker_url}/vk/oauth/pending?{urlencode({'state': state})}",
        headers={
            "Authorization": f"Bearer {settings.worker_relay_secret}",
            "User-Agent": "BruceBet/3000",
        },
    )
    try:
        with opener(request, timeout=settings.worker_timeout_seconds) as response:
            raw_payload = response.read()
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise VkOAuthError(f"VK OAuth Worker relay request failed with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise VkOAuthError(f"VK OAuth Worker relay request failed: {exc}") from exc

    if not raw_payload:
        return False
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VkOAuthError("VK OAuth Worker relay returned invalid data") from exc
    if not isinstance(payload, dict):
        raise VkOAuthError("VK OAuth Worker relay returned invalid data")
    code = str(payload.get("code", "")).strip()
    device_id = str(payload.get("device_id", "")).strip()
    if not code or not device_id:
        raise VkOAuthError("VK OAuth Worker relay returned an incomplete authorization response")

    complete_authorization(settings, code=code, state=state, device_id=device_id)
    return True


def _page(title: str, body: str, status: int = 200) -> tuple[int, bytes]:
    html = f"<!doctype html><meta charset=\"utf-8\"><title>{escape(title)}</title><h1>{escape(title)}</h1><p>{escape(body)}</p>"
    return status, html.encode("utf-8")


def handler_factory(settings: VkOAuthSettings):
    class VkOAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path in {"/healthz", "/vk/oauth/healthz"}:
                status, body = _page("ok", "VK OAuth callback is ready")
            elif parsed.path != "/vk/oauth/callback":
                status, body = _page("Not found", "Unknown route", status=404)
            else:
                query = parse_qs(parsed.query)
                error = query.get("error", [""])[0]
                code = query.get("code", [""])[0]
                state = query.get("state", [""])[0]
                device_id = query.get("device_id", [""])[0]
                try:
                    if error:
                        description = query.get("error_description", [error])[0]
                        raise VkOAuthError(description)
                    if not code:
                        raise VkOAuthError("VK did not return an authorization code")
                    complete_authorization(settings, code=code, state=state, device_id=device_id)
                    status, body = _page("BruceBet connected to VK", "Готово. Вернись в Telegram и используй /vk_status.")
                except VkOAuthError as exc:
                    status, body = _page("VK connection was not completed", str(exc), status=400)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return VkOAuthHandler


def main() -> None:
    settings = VkOAuthSettings.from_env()
    host = os.getenv("VK_OAUTH_BIND_HOST", "0.0.0.0")
    port = int(os.getenv("VK_OAUTH_PORT", "8090"))
    server = ThreadingHTTPServer((host, port), handler_factory(settings))
    server.serve_forever()


if __name__ == "__main__":
    main()
