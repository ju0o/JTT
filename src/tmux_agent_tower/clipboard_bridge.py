"""Foreground, loopback-only clipboard bridge for a local Wayland session."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from .access_context import AccessContext, context_from_dict
from .paths import LOCAL_STATE_DIR

HOST = "127.0.0.1"
MAX_BYTES = 48 * 1024
PROVIDER_TIMEOUT = 8
_CLIENT_ID = re.compile(r"^[a-f0-9]{32}$")
CLIENT_ID_ENV = "TOWER_CLIPBOARD_BRIDGE_ID"


@dataclass(frozen=True)
class BridgeRegistration:
    port: int
    token: str = field(repr=False)
    access_context: Optional[AccessContext] = None


def bridge_state_dir() -> Path:
    return LOCAL_STATE_DIR / "clipboard-bridges"


def register_bridge_client(instance_id: str, registration: dict) -> None:
    """Store one short-lived bridge credential under a random client id."""

    if not _CLIENT_ID.fullmatch(instance_id):
        raise ValueError("invalid client id")
    if not isinstance(registration, dict):
        raise ValueError("invalid registration")
    port = registration.get("port")
    token = registration.get("token")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid port")
    if not isinstance(token, str) or len(token) < 32 or not token.isascii():
        raise ValueError("invalid token")
    access = context_from_dict(registration.get("access_context"))
    if registration.get("access_context") is not None and access is None:
        raise ValueError("invalid access context")
    directory = bridge_state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / f"{instance_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            payload = {"port": port, "token": token}
            if access is not None:
                payload["access_context"] = access.__dict__
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def unregister_bridge_client(instance_id: str) -> None:
    if _CLIENT_ID.fullmatch(instance_id):
        try:
            (bridge_state_dir() / f"{instance_id}.json").unlink()
        except FileNotFoundError:
            pass


def bridge_id_for_process(pid: int) -> Optional[str]:
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError):
        return None
    prefix = f"{CLIENT_ID_ENV}=".encode("ascii")
    for entry in raw.split(b"\0"):
        if entry.startswith(prefix):
            value = entry[len(prefix):].decode("ascii", "ignore")
            return value if _CLIENT_ID.fullmatch(value) else None
    return None


def load_bridge_client(instance_id: str) -> Optional[BridgeRegistration]:
    if not _CLIENT_ID.fullmatch(instance_id):
        return None
    directory = bridge_state_dir()
    path = directory / f"{instance_id}.json"
    try:
        directory_stat = directory.lstat()
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.getuid()
            or directory_stat.st_mode & 0o077
        ):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 2048
            ):
                return None
            payload = json.load(handle)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    try:
        port = payload["port"]
        token = payload["token"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            return None
    except (KeyError, TypeError):
        return None
    return BridgeRegistration(port, token, context_from_dict(payload.get("access_context")))


def _copy_with_wl_copy(data: bytes) -> bool:
    try:
        copied = subprocess.run(
            ["wl-copy"],
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROVIDER_TIMEOUT,
            check=False,
        ).returncode == 0
        if not copied:
            return False
        readback = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PROVIDER_TIMEOUT,
            check=False,
        )
        return readback.returncode == 0 and readback.stdout == data
    except (OSError, subprocess.TimeoutExpired):
        return False


def _copy_with_native_clipboard(data: bytes) -> bool:
    """Use the detected local OS provider and require exact clipboard readback."""

    try:
        text = data.decode("utf-8", "strict")
        from .clipboard import copy_host_clipboard

        result = copy_host_clipboard(text)
        return result.clipboard
    except (UnicodeError, OSError, ValueError):
        return False


class _Handler(BaseHTTPRequestHandler):
    server: "ClipboardBridgeServer"
    server_version = "TowerClipboardBridge"
    sys_version = ""

    def log_message(self, _format: str, *args: object) -> None:
        pass

    def _reply(self, status: int, *, error: Optional[str] = None, digest: Optional[str] = None) -> None:
        result = {"error": error} if error else {"success": True, "payload_sha256": digest}
        body = json.dumps(result, separators=(",", ":")).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        self._reply(code, error="request_rejected")

    def _method_not_allowed(self) -> None:
        self._reply(405, error="method_not_allowed")

    do_GET = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _method_not_allowed

    def do_POST(self) -> None:
        if self.path != "/copy":
            self._reply(404, error="not_found")
            return

        supplied = self.headers.get("X-Tower-Token", "")
        try:
            authorized = hmac.compare_digest(
                self.server.token.encode("ascii"), supplied.encode("ascii")
            )
        except UnicodeEncodeError:
            authorized = False
        if not authorized:
            self._reply(401, error="unauthorized")
            return

        length_text = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not length_text.isascii() or not length_text.isdecimal():
            self._reply(400, error="invalid_request")
            return
        normalized_length = length_text.lstrip("0") or "0"
        if len(normalized_length) > len(str(MAX_BYTES)):
            self._reply(413, error="too_large")
            return
        length = int(normalized_length)
        if length > MAX_BYTES:
            self._reply(413, error="too_large")
            return

        data = self.rfile.read(length)
        if len(data) != length:
            self._reply(400, error="invalid_request")
            return
        try:
            text = data.decode("utf-8", "strict")
        except UnicodeDecodeError:
            self._reply(400, error="invalid_text")
            return
        if "\0" in text:
            self._reply(400, error="invalid_text")
            return
        try:
            copied = self.server.provider(data)
        except Exception:
            copied = False
        if not copied:
            self._reply(502, error="provider_failed")
            return
        self._reply(200, digest=hashlib.sha256(data).hexdigest())


class ClipboardBridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, token: str, port: int = 0, provider: Optional[Callable[[bytes], bool]] = None):
        self.token = token
        self.provider = provider or _copy_with_native_clipboard
        super().__init__((HOST, port), _Handler)

    def handle_error(self, _request: object, _client_address: object) -> None:
        pass


def _create_token_state() -> tuple[Path, Path, str]:
    state_dir = Path(tempfile.mkdtemp(prefix="tower-clipboard-bridge-"))
    os.chmod(state_dir, 0o700)
    token = secrets.token_urlsafe(32)
    token_file = state_dir / "token"
    try:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token)
    except BaseException:
        shutil.rmtree(state_dir, ignore_errors=True)
        raise
    return state_dir, token_file, token


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a private loopback wl-copy bridge; stop with Ctrl-C.")
    parser.add_argument("--port", type=int, default=0, help="loopback port (default: choose a free port)")
    parser.add_argument("--register-client", metavar="ID")
    parser.add_argument("--unregister-client", metavar="ID")
    args = parser.parse_args(argv)
    if args.register_client:
        try:
            register_bridge_client(args.register_client, json.load(sys.stdin))
        except (OSError, ValueError, json.JSONDecodeError):
            return 2
        return 0
    if args.unregister_client:
        unregister_bridge_client(args.unregister_client)
        return 0
    state_dir, token_file, token = _create_token_state()
    server = None
    try:
        server = ClipboardBridgeServer(token, args.port)
        host, port = server.server_address
        print(
            f"clipboard-bridge pid={os.getpid()} listen=http://{host}:{port} token_file={token_file}",
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        shutil.rmtree(state_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
