#!/usr/bin/env python3
"""Install a Linear OAuth application as an app actor using PKCE S256."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast

AUTH_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
GRAPHQL_URL = "https://api.linear.app/graphql"
DEFAULT_DEST = Path(
    "/Users/mutlupolatcan/.hermes/profiles/general/credentials/linear-oauth.json"
)
REQUESTED_SCOPES = ["read", "write", "app:assignable", "app:mentionable"]


class CallbackServer(HTTPServer):
    result: dict[str, str] | None = None


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth/callback":
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        result = {key: values[0] for key, values in params.items() if values}
        callback_server = cast(CallbackServer, self.server)
        callback_server.result = result
        ok = bool(result.get("code")) and not result.get("error")
        title = "Linear OAuth tamamlandı" if ok else "Linear OAuth tamamlanamadı"
        detail = (
            "Bu pencereyi kapatıp Telegram'a dönebilirsin."
            if ok
            else "Linear consent reddedildi veya OAuth hatası oluştu. Telegram'a dön."
        )
        body = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title></head><body>"
            f"<h1>{title}</h1><p>{detail}</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id-from-clipboard", action="store_true", required=True)
    parser.add_argument(
        "--redirect-uri", default="http://localhost:3000/oauth/callback"
    )
    parser.add_argument("--destination", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_client_id_from_clipboard() -> str:
    value = subprocess.check_output(["/usr/bin/pbpaste"], text=True).strip()
    subprocess.run(["/usr/bin/pbcopy"], input=b"", check=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,200}", value):
        raise ValueError("Clipboard does not contain a plausible Linear client ID")
    return value


def base64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def post_form(url: str, values: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(values).encode("ascii")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Linear token response is not an object")
    return result


def query_viewer(access_token: str) -> dict[str, Any]:
    body = json.dumps(
        {"query": "query HermesLinearOAuthViewer { viewer { id name } }"},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or result.get("errors"):
        raise ValueError("Linear viewer verification failed")
    viewer = result.get("data", {}).get("viewer")
    if not isinstance(viewer, dict) or not viewer.get("id"):
        raise ValueError("Linear viewer response is missing app-user identity")
    return viewer


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    args = parse_args()
    destination: Path = args.destination
    if destination.exists() and not args.overwrite:
        raise FileExistsError(f"Credential file already exists: {destination}")

    redirect = urllib.parse.urlparse(args.redirect_uri)
    if (
        redirect.scheme != "http"
        or redirect.hostname not in {"localhost", "127.0.0.1"}
        or redirect.port != 3000
        or redirect.path != "/oauth/callback"
    ):
        raise ValueError("Redirect URI must be http://localhost:3000/oauth/callback")

    client_id = read_client_id_from_clipboard()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64url_sha256(verifier)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": args.redirect_uri,
            "scope": ",".join(REQUESTED_SCOPES),
            "state": state,
            "actor": "app",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    server = CallbackServer(("127.0.0.1", 3000), CallbackHandler)
    server.timeout = 1.0
    subprocess.run(["/usr/bin/open", f"{AUTH_URL}?{query}"], check=True)
    print("OAUTH_BROWSER_OPENED", flush=True)

    deadline = time.monotonic() + args.timeout
    try:
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if server.result is None:
        raise TimeoutError("Timed out waiting for Linear OAuth callback")
    if server.result.get("state") != state:
        raise ValueError("OAuth state mismatch")
    if server.result.get("error"):
        raise PermissionError("Linear OAuth authorization was not granted")
    code = server.result.get("code", "")
    if not code:
        raise ValueError("OAuth callback is missing authorization code")

    token = post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": args.redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Linear token response is missing access_token")

    obtained_at = int(time.time())
    credential: dict[str, Any] = dict(token)
    credential.update(
        {
            "oauth_client_id": client_id,
            "redirect_uri": args.redirect_uri,
            "requested_scopes": REQUESTED_SCOPES,
            "obtained_at": obtained_at,
            "obtained_at_iso": datetime.now(timezone.utc).isoformat(),
        }
    )
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)):
        credential["expires_at"] = obtained_at + int(expires_in)

    # Persist immediately after exchange so a later verification failure cannot lose tokens.
    atomic_write_json(destination, credential)
    viewer = query_viewer(access_token)
    credential["app_user"] = {"id": viewer["id"], "name": viewer.get("name", "")}
    atomic_write_json(destination, credential)

    granted = token.get("scope", "")
    print("OAUTH_TOKEN_STORED", flush=True)
    print(f"OAUTH_FILE_MODE={oct(stat.S_IMODE(destination.stat().st_mode))}", flush=True)
    print(f"APP_USER_ID={viewer['id']}", flush=True)
    print(f"APP_USER_NAME={viewer.get('name', '')}", flush=True)
    print(f"GRANTED_SCOPES={granted}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        # Never print response bodies: OAuth errors may carry sensitive context.
        code = getattr(exc, "code", "network")
        print(f"OAUTH_ERROR={type(exc).__name__}:{code}", flush=True)
        raise SystemExit(1)
    except Exception as exc:
        print(f"OAUTH_ERROR={type(exc).__name__}:{exc}", flush=True)
        raise SystemExit(1)
