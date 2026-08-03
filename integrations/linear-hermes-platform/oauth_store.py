"""Shared profile-local OAuth credential store for Linear clients."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"


class LinearAPIError(RuntimeError):
    """Safe Linear error with retry metadata and no credential values."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class LinearOAuthStore:
    """Own one writable OAuth bundle shared by GraphQL and MCP transports."""

    def __init__(
        self,
        oauth_file: str,
        *,
        refresh_margin_seconds: int = 300,
        token_url: str = LINEAR_TOKEN_URL,
        lock_timeout_seconds: float = 8.0,
    ) -> None:
        supplied_file = Path(oauth_file)
        try:
            canonical_parent = supplied_file.parent.resolve(strict=True)
        except OSError as exc:
            raise LinearAPIError("OAuth credential parent is unavailable") from exc
        self.oauth_file = canonical_parent / supplied_file.name
        self.lock_file = Path(f"{self.oauth_file}.lock")
        self.refresh_margin_seconds = int(refresh_margin_seconds)
        self.token_url = token_url
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self._instance_lock = asyncio.Lock()

    def _validate_parent(self) -> None:
        try:
            parent_lstat = self.oauth_file.parent.lstat()
            parent_stat = self.oauth_file.parent.stat()
        except OSError as exc:
            raise LinearAPIError("OAuth credential parent is unavailable") from exc
        if stat.S_ISLNK(parent_lstat.st_mode):
            raise LinearAPIError("OAuth credential parent must not be a symlink")
        if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid():
            raise LinearAPIError("OAuth credential parent is not an owned directory")
        if parent_stat.st_mode & 0o022:
            raise LinearAPIError("OAuth credential parent is writable by another user")

    def _read(self) -> dict[str, Any]:
        self._validate_parent()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.oauth_file, flags)
        except FileNotFoundError as exc:
            raise LinearAPIError("OAuth credential file is missing") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise LinearAPIError("OAuth credential file must not be a symlink") from exc
            raise LinearAPIError("OAuth credential file is unavailable") from exc
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise LinearAPIError("OAuth credential path is not a regular file")
            if file_stat.st_uid != os.getuid():
                raise LinearAPIError("OAuth credential file owner is invalid")
            mode = file_stat.st_mode & 0o777
            if mode & 0o077:
                raise LinearAPIError(
                    f"OAuth credential file permissions are too broad: {oct(mode)}"
                )
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise LinearAPIError("OAuth credential file is not valid JSON") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(data, dict):
            raise LinearAPIError("OAuth credential file must contain a JSON object")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self._validate_parent()
        tmp = self.oauth_file.with_name(
            f".{self.oauth_file.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(tmp, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.oauth_file)
            directory_fd = os.open(
                self.oauth_file.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    async def _acquire_file_lock(self) -> int:
        self._validate_parent()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_file, flags, 0o600)
        except OSError as exc:
            raise LinearAPIError("OAuth lock file is unsafe or unavailable") from exc
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
            os.close(fd)
            raise LinearAPIError("OAuth lock file is not a safe owned regular file")
        os.fchmod(fd, 0o600)
        deadline = asyncio.get_running_loop().time() + self.lock_timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    os.close(fd)
                    raise LinearAPIError("OAuth credential lock timed out")
                await asyncio.sleep(0.02)
            except BaseException:
                os.close(fd)
                raise

    @staticmethod
    def _release_file_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _needs_refresh(self, data: dict[str, Any]) -> bool:
        if not data.get("access_token"):
            return True
        expires_at = int(data.get("expires_at") or 0)
        return bool(expires_at and expires_at <= int(time.time()) + self.refresh_margin_seconds)

    async def _refresh(self, data: dict[str, Any]) -> dict[str, Any]:
        refresh_token = data.get("refresh_token")
        client_id = data.get("oauth_client_id") or data.get("client_id")
        if not refresh_token or not client_id:
            raise LinearAPIError("OAuth refresh_token or client_id is missing")
        form = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": str(refresh_token),
                "client_id": str(client_id),
            }
        )
        timeout = aiohttp.ClientTimeout(total=8, connect=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.token_url,
                    data=form,
                    allow_redirects=False,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                ) as response:
                    if response.status != 200:
                        raise LinearAPIError(f"OAuth refresh failed with HTTP {response.status}")
                    payload = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise LinearAPIError("OAuth refresh timed out") from exc
        except aiohttp.ClientError as exc:
            raise LinearAPIError("OAuth refresh connection failed", retryable=True) from exc
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise LinearAPIError("OAuth refresh response did not contain access_token")

        now = int(time.time())
        updated = dict(data)
        updated["access_token"] = payload["access_token"]
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        if payload.get("token_type"):
            updated["token_type"] = payload["token_type"]
        if payload.get("scope"):
            updated["granted_scope"] = payload["scope"]
        expires_in = int(payload.get("expires_in") or 0)
        updated["expires_in"] = expires_in
        updated["obtained_at"] = now
        updated["expires_at"] = now + expires_in if expires_in else 0
        self._write(updated)
        return updated

    async def access_token(
        self,
        *,
        force_refresh: bool = False,
        stale_token: str | None = None,
    ) -> str:
        """Return a usable token, refreshing exactly once across consumers."""

        async with self._instance_lock:
            lock_fd = await self._acquire_file_lock()
            try:
                data = self._read()
                token = data.get("access_token")
                another_consumer_refreshed = bool(
                    force_refresh and stale_token and token and str(token) != stale_token
                )
                if self._needs_refresh(data) or (force_refresh and not another_consumer_refreshed):
                    data = await self._refresh(data)
                token = data.get("access_token")
                if not token:
                    raise LinearAPIError("OAuth access_token is missing")
                return str(token)
            finally:
                self._release_file_lock(lock_fd)
