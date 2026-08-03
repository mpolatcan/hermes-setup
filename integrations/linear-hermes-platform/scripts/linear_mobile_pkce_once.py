#!/usr/bin/env python3
"""One-shot mobile Linear OAuth installer with a public HTTPS callback."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import importlib
import json
import os
import re
import secrets
import stat
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from install_linear_oauth import (  # noqa: E402
    GRAPHQL_URL,
    TOKEN_URL,
    post_form,
)


AUTH_URL = "https://linear.app/oauth/authorize"
REQUESTED_SCOPES = ["read", "write", "app:assignable", "app:mentionable"]
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,200}")
CLIENT_ID_REFERENCE_RE = re.compile(r"op://[^/]+/[^/]+/LINEAR_CLIENT_ID")
SAFE_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
PUBLIC_BASE_URL_RE = re.compile(r"https://(?P<host>[a-z0-9.-]+)/oauth", re.ASCII)
DEFAULT_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"


@dataclass(frozen=True)
class AuthorizationGrant:
    code: str
    verifier: str
    redirect_uri: str


class RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("Invalid command-line arguments")


def _validated_public_host(public_base_url: str) -> str:
    match = PUBLIC_BASE_URL_RE.fullmatch(public_base_url)
    if match is None:
        raise ValueError("Public base URL must be exactly https://<host>/oauth")
    host = match.group("host")
    labels = host.split(".")
    if len(host) > 253 or any(
        DNS_LABEL_RE.fullmatch(label) is None for label in labels
    ):
        raise ValueError("Public base URL has an invalid DNS hostname")
    return host


def _base64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def resolve_client_id(
    reference: str,
    *,
    service_account_token: str,
    client_type: Any | None = None,
) -> str:
    if not CLIENT_ID_REFERENCE_RE.fullmatch(reference):
        raise ValueError("1Password reference must target LINEAR_CLIENT_ID")
    if not service_account_token:
        raise ValueError("Missing profile-scoped 1Password service-account token")
    if client_type is None:
        client_type = importlib.import_module("onepassword.client").Client
    client = await client_type.authenticate(
        auth=service_account_token,
        integration_name="Hermes Linear Mobile PKCE",
        integration_version="v0.1.0",
    )
    response = await client.secrets.resolve_all([reference])
    entry = getattr(response, "individual_responses", {}).get(reference)
    content = getattr(entry, "content", None)
    value = getattr(content, "secret", None)
    if entry is None or getattr(entry, "error", None) is not None:
        raise ValueError("1Password could not resolve LINEAR_CLIENT_ID")
    if not isinstance(value, str) or not CLIENT_ID_RE.fullmatch(value):
        raise ValueError("1Password LINEAR_CLIENT_ID has an invalid format")
    return value


class MobilePkceFlow:
    def __init__(
        self,
        *,
        client_id: str,
        public_base_url: str,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        host = _validated_public_host(public_base_url)
        self.client_id = client_id
        self.public_base_url = f"https://{host}/oauth"
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(48))
        self.state: str | None = None
        self.verifier: str | None = None
        self.completed = False

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url}/callback"

    def begin(self) -> str:
        if self.completed:
            raise RuntimeError("OAuth flow already completed")
        if self.state is None or self.verifier is None:
            self.state = self._token_factory()
            self.verifier = self._token_factory()
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": ",".join(REQUESTED_SCOPES),
                "state": self.state,
                "actor": "app",
                "prompt": "consent",
                "code_challenge": _base64url_sha256(self.verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTH_URL}?{query}"

    def accept_callback(self, params: dict[str, str]) -> AuthorizationGrant:
        if self.completed:
            raise RuntimeError("OAuth flow already completed")
        if self.state is None or self.verifier is None:
            raise RuntimeError("OAuth flow has not started")
        if params.get("state") != self.state:
            raise ValueError("OAuth state mismatch")
        if params.get("error"):
            raise PermissionError("Linear OAuth authorization was not granted")
        code = params.get("code", "")
        if not code:
            raise ValueError("OAuth callback is missing authorization code")
        grant = AuthorizationGrant(
            code=code,
            verifier=self.verifier,
            redirect_uri=self.redirect_uri,
        )
        self.completed = True
        self.state = None
        self.verifier = None
        return grant


class MobileCallbackServer(HTTPServer):
    flow: MobilePkceFlow
    public_host: str
    start_path: str
    start_url: str
    start_capability: str | None
    callback_path: str
    grant: AuthorizationGrant | None = None
    terminal_error: PermissionError | None = None


def _render_start_confirmation(action_path: str) -> bytes:
    escaped_path = html.escape(action_path, quote=True)
    return f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="dark">
  <title>Linear yetkilendirmesi</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; min-height: 100%; background: #08090a; }}
    body {{
      color: #f7f8f8;
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      font-feature-settings: "cv01", "ss03";
      -webkit-font-smoothing: antialiased;
    }}
    .auth-shell {{
      min-height: 100svh;
      display: grid;
      place-items: center;
      padding: max(24px, env(safe-area-inset-top))
        max(20px, env(safe-area-inset-right))
        max(24px, env(safe-area-inset-bottom))
        max(20px, env(safe-area-inset-left));
      background:
        radial-gradient(circle at 50% -15%, rgba(113, 112, 255, .22), transparent 42%),
        #08090a;
    }}
    .auth-card {{
      width: min(100%, 420px);
      padding: 32px;
      border: 1px solid rgba(255, 255, 255, .08);
      border-radius: 22px;
      background: rgba(255, 255, 255, .035);
      box-shadow: 0 24px 80px rgba(0, 0, 0, .45), inset 0 1px rgba(255, 255, 255, .04);
      text-align: center;
    }}
    .brand-mark {{
      width: 52px;
      height: 52px;
      display: grid;
      place-items: center;
      margin: 0 auto 24px;
      border: 1px solid rgba(255, 255, 255, .10);
      border-radius: 15px;
      background: linear-gradient(145deg, #7170ff, #5e6ad2);
      box-shadow: 0 12px 30px rgba(94, 106, 210, .28);
      font-size: 22px;
      font-weight: 590;
    }}
    .eyebrow {{
      margin: 0 0 12px;
      color: #8a8f98;
      font-size: 12px;
      font-weight: 590;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(28px, 8vw, 34px);
      font-weight: 510;
      letter-spacing: -.7px;
      line-height: 1.12;
    }}
    .description {{
      margin: 16px auto 28px;
      color: #8a8f98;
      font-size: 16px;
      line-height: 1.55;
    }}
    form {{ margin: 0; }}
    .primary-action {{
      width: 100%;
      min-height: 56px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 14px 20px;
      border: 1px solid rgba(255, 255, 255, .12);
      border-radius: 12px;
      background: #5e6ad2;
      color: #fff;
      box-shadow: 0 12px 28px rgba(94, 106, 210, .28);
      font: inherit;
      font-size: 16px;
      font-weight: 590;
      cursor: pointer;
      transition: background .16s ease, transform .16s ease, box-shadow .16s ease;
      -webkit-tap-highlight-color: transparent;
    }}
    .primary-action:hover {{ background: #7170ff; }}
    .primary-action:active {{ transform: translateY(1px) scale(.995); }}
    .primary-action:focus-visible {{
      outline: 3px solid rgba(130, 143, 255, .45);
      outline-offset: 3px;
    }}
    .arrow {{ font-size: 20px; line-height: 1; }}
    .security-note {{
      margin: 18px 0 0;
      color: #8a8f98;
      font-size: 13px;
      line-height: 1.5;
    }}
    @media (max-width: 420px) {{
      .auth-card {{ padding: 28px 22px; border-radius: 18px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      .primary-action {{ transition: none; }}
    }}
  </style>
</head>
<body>
  <main class="auth-shell">
    <section class="auth-card" aria-labelledby="auth-title">
      <div class="brand-mark" aria-hidden="true">L</div>
      <p class="eyebrow">Hermes × Linear</p>
      <h1 id="auth-title">Defne’yi Linear’a bağla</h1>
      <p class="description">Güvenli yetkilendirme akışını başlatmak için devam et.</p>
      <form method="post" action="{escaped_path}">
        <button class="primary-action" type="submit">
          <span>Linear ile Devam Et</span><span class="arrow" aria-hidden="true">→</span>
        </button>
      </form>
      <p class="security-note">Bu bağlantı tek kullanımlıdır ve kısa süre içinde sona erer.</p>
    </section>
  </main>
</body>
</html>""".encode("utf-8")


class MobileCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _has_exact_public_host(self, server: MobileCallbackServer) -> bool:
        host_values = self.headers.get_all("Host", [])
        return len(host_values) == 1 and host_values[0] == server.public_host

    def _raw_request_target(self) -> str | None:
        raw_requestline = getattr(self, "raw_requestline", b"")
        if not isinstance(raw_requestline, bytes):
            return None
        try:
            request_line = raw_requestline.decode("iso-8859-1").rstrip("\r\n")
        except UnicodeDecodeError:
            return None
        parts = request_line.split(" ")
        if len(parts) != 3 or not parts[1]:
            return None
        return parts[1]

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _respond(
        self,
        status: int,
        body: bytes = b"",
        *,
        location: str | None = None,
        content_type: str | None = None,
    ) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _redirect_to_linear(self, server: MobileCallbackServer) -> None:
        server.start_capability = None
        authorization_url = server.flow.begin()
        escaped_url = html.escape(authorization_url, quote=True)
        body = (
            '<!doctype html><meta charset="utf-8">'
            "<title>Linear OAuth</title>"
            "<p>Redirecting to Linear… "
            f'<a href="{escaped_url}">Continue to Linear</a></p>'
        ).encode("utf-8")
        self._respond(
            303,
            body,
            location=authorization_url,
            content_type="text/html; charset=utf-8",
        )

    def do_GET(self) -> None:
        server = self.server
        if not isinstance(server, MobileCallbackServer):
            self._respond(500)
            return
        if not self._has_exact_public_host(server):
            self._respond(404)
            return
        request_target = self._raw_request_target()
        if request_target == server.start_path:
            if server.start_capability is None:
                self._respond(404)
                return
            body = _render_start_confirmation(server.start_path)
            self._respond(
                200,
                body,
                content_type="text/html; charset=utf-8",
            )
            return
        if request_target is None:
            self._respond(404)
            return
        callback_target, separator, callback_query = request_target.partition("?")
        if (
            callback_target != server.callback_path
            or separator != "?"
            or not callback_query
            or "#" in callback_query
        ):
            self._respond(404)
            return
        params = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(callback_query).items()
            if values
        }
        try:
            server.grant = server.flow.accept_callback(params)
        except PermissionError as exc:
            server.terminal_error = exc
            self._respond(403, b"Linear OAuth authorization was not granted.")
            return
        except ValueError:
            self._respond(400, b"Linear OAuth callback was rejected.")
            return
        except RuntimeError:
            self._respond(409, b"Linear OAuth callback was already consumed.")
            return
        self._respond(200, b"Linear OAuth complete. You can close this page.")

    def do_POST(self) -> None:
        server = self.server
        if not isinstance(server, MobileCallbackServer):
            self._respond(500)
            return
        if (
            not self._has_exact_public_host(server)
            or self._raw_request_target() != server.start_path
            or server.start_capability is None
        ):
            self._respond(404)
            return
        self._redirect_to_linear(server)


def create_server(
    address: tuple[str, int],
    flow: MobilePkceFlow,
    *,
    capability_factory: Callable[[], str] | None = None,
    bind_and_activate: bool = True,
) -> MobileCallbackServer:
    base = urllib.parse.urlparse(flow.public_base_url)
    server = MobileCallbackServer(
        address, MobileCallbackHandler, bind_and_activate=bind_and_activate
    )
    server.flow = flow
    server.grant = None
    server.terminal_error = None
    server.public_host = (base.hostname or "").lower()
    base_path = base.path.rstrip("/")
    capability = (capability_factory or (lambda: secrets.token_urlsafe(48)))()
    if not capability or "/" in capability or "?" in capability or "#" in capability:
        server.server_close()
        raise ValueError("Start capability generator returned an invalid value")
    server.start_capability = capability
    server.start_path = f"{base_path}/start/{capability}"
    server.start_url = f"{flow.public_base_url}/start/{capability}"
    server.callback_path = f"{base_path}/callback"
    return server


def validate_credential_destination(
    destination: Path,
    *,
    profiles_root: Path = DEFAULT_PROFILES_ROOT,
) -> str:
    if not destination.is_absolute() or not profiles_root.is_absolute():
        raise ValueError("Credential destination and profiles root must be absolute")
    try:
        relative = destination.relative_to(profiles_root)
    except ValueError as exc:
        raise ValueError("Credential destination must be inside the profiles root") from exc
    if len(relative.parts) != 3:
        raise ValueError("Credential destination has an invalid profile path")
    profile, directory, filename = relative.parts
    if (
        not SAFE_PROFILE_RE.fullmatch(profile)
        or directory != "credentials"
        or filename != "linear-oauth.json"
    ):
        raise ValueError("Credential destination has an invalid profile path")

    for component in (profiles_root, profiles_root / profile, destination.parent):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise OSError("Credential path contains a symbolic link")
        if not stat.S_ISDIR(mode):
            raise NotADirectoryError("Credential path component is not a directory")
    try:
        destination_mode = destination.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(destination_mode):
            raise OSError("Credential destination is a symbolic link")
        raise FileExistsError("Credential destination already exists")
    return profile


def atomic_install_json(
    destination: Path,
    value: dict[str, Any],
    *,
    profiles_root: Path = DEFAULT_PROFILES_ROOT,
) -> None:
    profile = validate_credential_destination(
        destination, profiles_root=profiles_root
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = open_absolute_directory_nofollow(profiles_root)
    profile_fd: int | None = None
    credentials_fd: int | None = None
    temp_name: str | None = None
    try:
        profile_fd = os.open(profile, directory_flags, dir_fd=root_fd)
        try:
            os.mkdir("credentials", mode=0o700, dir_fd=profile_fd)
            os.fsync(profile_fd)
        except FileExistsError:
            pass
        credentials_fd = os.open("credentials", directory_flags, dir_fd=profile_fd)
        temp_name = f".linear-oauth.json.{secrets.token_hex(16)}.tmp"
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=credentials_fd,
        )
        try:
            os.fchmod(temp_fd, 0o600)
            payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
            offset = 0
            while offset < len(payload):
                offset += os.write(temp_fd, payload[offset:])
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)

        os.link(
            temp_name,
            destination.name,
            src_dir_fd=credentials_fd,
            dst_dir_fd=credentials_fd,
            follow_symlinks=False,
        )
        os.fsync(credentials_fd)
    finally:
        if temp_name is not None and credentials_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=credentials_fd)
                os.fsync(credentials_fd)
            except FileNotFoundError:
                pass
        if credentials_fd is not None:
            os.close(credentials_fd)
        if profile_fd is not None:
            os.close(profile_fd)
        os.close(root_fd)


def open_absolute_directory_nofollow(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("Directory path must be absolute")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open("/", directory_flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def query_viewer_context(access_token: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "query": (
                "query HermesLinearMobileOAuthViewer { "
                "viewer { id name organization { id } } }"
            )
        },
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
    organization = viewer.get("organization") if isinstance(viewer, dict) else None
    if (
        not isinstance(viewer, dict)
        or not viewer.get("id")
        or not isinstance(organization, dict)
        or not organization.get("id")
    ):
        raise ValueError("Linear viewer response is missing app-user organization")
    return viewer


def complete_install(
    *,
    grant: AuthorizationGrant,
    client_id: str,
    expected_organization_id: str,
    destination: Path,
    post_form_fn: Callable[[str, dict[str, str]], dict[str, Any]] = post_form,
    query_viewer_fn: Callable[[str], dict[str, Any]],
    profiles_root: Path = DEFAULT_PROFILES_ROOT,
    now_fn: Callable[[], float] = time.time,
) -> dict[str, str]:
    validate_credential_destination(destination, profiles_root=profiles_root)
    token = post_form_fn(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": grant.code,
            "redirect_uri": grant.redirect_uri,
            "client_id": client_id,
            "code_verifier": grant.verifier,
        },
    )
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Linear token response is missing access_token")
    granted = token.get("scope", "")
    if not isinstance(granted, str):
        raise ValueError("Linear token response has invalid scopes")
    granted_set = {part for part in re.split(r"[\s,]+", granted) if part}
    if not set(REQUESTED_SCOPES).issubset(granted_set):
        raise PermissionError("Linear OAuth did not grant all required scopes")

    viewer = query_viewer_fn(access_token)
    organization = viewer.get("organization")
    organization_id = (
        organization.get("id") if isinstance(organization, dict) else None
    )
    if organization_id != expected_organization_id:
        raise PermissionError("Linear OAuth organization mismatch")

    obtained_at = int(now_fn())
    credential: dict[str, Any] = dict(token)
    credential.update(
        {
            "oauth_client_id": client_id,
            "redirect_uri": grant.redirect_uri,
            "requested_scopes": REQUESTED_SCOPES,
            "organization_id": organization_id,
            "app_user": {
                "id": viewer["id"],
                "name": viewer.get("name", ""),
            },
            "obtained_at": obtained_at,
            "obtained_at_iso": datetime.fromtimestamp(
                obtained_at, tz=timezone.utc
            ).isoformat(),
        }
    )
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)):
        credential["expires_at"] = obtained_at + int(expires_in)

    atomic_install_json(destination, credential, profiles_root=profiles_root)
    return {
        "app_user_id": str(viewer["id"]),
        "app_user_name": str(viewer.get("name", "")),
        "granted_scopes": granted,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = RedactingArgumentParser(
        description=(
            "Run a one-shot Linear PKCE flow through an exact public HTTPS callback. "
            "LINEAR_CLIENT_ID is resolved from a profile-scoped 1Password reference."
        )
    )
    parser.add_argument("--client-id-reference", required=True)
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--expected-organization-id", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--bind-port", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    return parser.parse_args(argv)


def validate_runtime_inputs(
    args: argparse.Namespace,
    *,
    profiles_root: Path = DEFAULT_PROFILES_ROOT,
) -> None:
    destination: Path = args.destination
    validate_credential_destination(destination, profiles_root=profiles_root)
    if not 1 <= args.bind_port <= 65535:
        raise ValueError("Bind port must be between 1 and 65535")
    if not 30 <= args.timeout_seconds <= 1800:
        raise ValueError("Timeout must be between 30 and 1800 seconds")


def wait_for_grant(
    server: Any,
    *,
    timeout_seconds: int,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> AuthorizationGrant:
    deadline = monotonic_fn() + timeout_seconds
    server.timeout = 1.0
    while (
        server.grant is None
        and getattr(server, "terminal_error", None) is None
        and monotonic_fn() < deadline
    ):
        server.handle_request()
    terminal_error = getattr(server, "terminal_error", None)
    if terminal_error is not None:
        raise terminal_error
    if server.grant is None:
        raise TimeoutError("Mobile PKCE callback timed out")
    return server.grant


def run(
    args: argparse.Namespace,
    *,
    environ: dict[str, str] | os._Environ[str] = os.environ,
    resolve_client_id_fn: Any = resolve_client_id,
    create_server_fn: Any = create_server,
    complete_install_fn: Any = complete_install,
    output: Any = sys.stdout,
    profiles_root: Path = DEFAULT_PROFILES_ROOT,
) -> int:
    validate_runtime_inputs(args, profiles_root=profiles_root)
    token = environ.pop("OP_SERVICE_ACCOUNT_TOKEN", "")
    try:
        client_id = asyncio.run(
            resolve_client_id_fn(
                args.client_id_reference,
                service_account_token=token,
            )
        )
    finally:
        token = ""

    flow = MobilePkceFlow(
        client_id=client_id,
        public_base_url=args.public_base_url,
    )
    server = create_server_fn(("127.0.0.1", args.bind_port), flow)
    print("MOBILE_PKCE_READY=true", file=output)
    print(f"START_URL={json.dumps(server.start_url, ensure_ascii=True)}", file=output)
    print(f"CALLBACK_URL={json.dumps(flow.redirect_uri, ensure_ascii=True)}", file=output)
    print(
        f"LOCAL_BIND={json.dumps(f'127.0.0.1:{args.bind_port}', ensure_ascii=True)}",
        file=output,
    )

    try:
        grant = wait_for_grant(server, timeout_seconds=args.timeout_seconds)
    finally:
        server.server_close()

    summary = complete_install_fn(
        grant=grant,
        client_id=client_id,
        expected_organization_id=args.expected_organization_id,
        destination=args.destination,
        profiles_root=profiles_root,
        query_viewer_fn=query_viewer_context,
    )
    client_id = ""
    print("MOBILE_PKCE_COMPLETE=true", file=output)
    print(
        f"APP_USER_ID={json.dumps(summary['app_user_id'], ensure_ascii=True)}",
        file=output,
    )
    print(
        f"APP_USER_NAME_JSON={json.dumps(summary['app_user_name'], ensure_ascii=True)}",
        file=output,
    )
    print(
        f"GRANTED_SCOPES={json.dumps(summary['granted_scopes'], ensure_ascii=True)}",
        file=output,
    )
    print(f"DESTINATION={json.dumps(str(args.destination), ensure_ascii=True)}", file=output)
    print('DESTINATION_MODE="0600"', file=output)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    run_fn: Any = run,
    error_output: Any = sys.stderr,
) -> int:
    try:
        args = parse_args(argv)
        return run_fn(args)
    except FileExistsError:
        marker = "DESTINATION_EXISTS"
    except TimeoutError:
        marker = "CALLBACK_TIMEOUT"
    except PermissionError:
        marker = "AUTHORIZATION_REJECTED"
    except ValueError:
        marker = "INPUT_OR_VERIFICATION_FAILED"
    except OSError:
        marker = "LOCAL_LISTENER_OR_WRITE_FAILED"
    except Exception:
        marker = "UNEXPECTED_FAILURE"
    print(
        f"MOBILE_PKCE_ERROR={json.dumps(marker, ensure_ascii=True)}",
        file=error_output,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
