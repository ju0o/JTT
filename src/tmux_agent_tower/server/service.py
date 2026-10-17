"""Tower Remote lifecycle shared by the CLI and the TUI.

``tower serve`` (foreground) and the TUI's ``M`` menu (background) both
call into this module. Tailscale setup is not reimplemented in the UI:
the detached child is this same module's foreground loop.

The background process outlives the TUI on purpose. Quitting the TUI
does not stop it; only ``stop()`` does, and only when the recorded PID
is still a Tower Remote process.

The tmux session a remote watches is always passed in explicitly by the
Tower (or CLI process) that starts it. The detached child never infers
it from its own environment: a child spawned from a scratch session that
was later deleted would otherwise keep serving an empty pane list while
looking healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..i18n import t
from ..paths import CONFIG_DIR
from ..tmux import capture as tmux_capture
from . import httpapi, netutil, tailscale

DEFAULT_PORT = 4312
_START_TIMEOUT_SECONDS = 20.0
_STATUS_CACHE_SECONDS = 2.0

# Set by _setup_tailscale so a detached child can record a reason code
# without changing the (None, None) return the CLI tests assert on.
_last_setup_reason = ""


@dataclass
class ServiceStatus:
    state: str  # "stopped" | "running" | "error"
    mode: str = ""
    url: str = ""
    https: bool = False
    port: int = 0
    pid: int = 0
    reason: str = ""
    stale: bool = False
    session: str = ""  # tmux session the running remote was bound to
    own_pane_id: str = ""


@dataclass
class StartResult:
    ok: bool
    already_running: bool = False
    reason: str = ""
    url: str = ""
    pairing_code: str = ""
    https: bool = False
    tailscale_ok: bool = False
    session: str = ""  # session this result refers to
    running_session: str = ""  # set with reason "session_mismatch"


def _runtime_path() -> Path:
    return CONFIG_DIR / "remote-runtime.json"


def _error_path() -> Path:
    return CONFIG_DIR / "remote-last-error.json"


def _control_dir() -> Path:
    return CONFIG_DIR / "remote-control"


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _prepare_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass


def _write_json(path: Path, payload: dict) -> None:
    _prepare_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    _secure_file(tmp)
    os.replace(tmp, path)
    _secure_file(path)


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_last_error(reason: str) -> None:
    _write_json(_error_path(), {"reason": reason, "at": time.time()})


def _clear_last_error() -> None:
    try:
        _error_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _clear_runtime() -> None:
    try:
        _runtime_path().unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def process_is_ours(pid: int) -> bool:
    """True only when ``pid`` is a Tower Remote server we started.

    A recycled PID that now belongs to something else must never be
    signalled. Matches either the detached ``-m ...server.service``
    child or a foreground ``tower serve``.
    """

    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except Exception:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    if "tmux_agent_tower.server.service" in cmdline:
        return True
    return "tower" in cmdline and "serve" in cmdline


def _health_ok(port: int) -> bool:
    import urllib.request

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
        req.add_header("Host", "127.0.0.1")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _tailscale_mapping_ok(port: int) -> bool:
    exe = tailscale.find_tailscale_exe()
    if not exe:
        return False
    # Short timeout: this runs on the TUI refresh path and must not stall it.
    ts_status = tailscale.get_status(exe, timeout=1.5)
    dns_name = tailscale.self_dns_name(ts_status) if ts_status else None
    if not dns_name:
        return False
    proxy = tailscale.existing_mapping_proxy(tailscale.get_serve_status(exe, timeout=1.5), dns_name)
    return proxy == f"http://127.0.0.1:{port}"


_status_cache = {"at": 0.0, "value": None}


def status(force: bool = False) -> ServiceStatus:
    """Real backend + (for tailscale mode) Serve mapping. Not a flag."""

    now = time.monotonic()
    cached = _status_cache["value"]
    if not force and cached is not None and now - _status_cache["at"] < _STATUS_CACHE_SECONDS:
        return cached

    value = _status_uncached()
    _status_cache["at"] = now
    _status_cache["value"] = value
    return value


def _status_uncached() -> ServiceStatus:
    runtime = _read_json(_runtime_path()) or {}
    pid = int(runtime.get("pid") or 0)
    port = int(runtime.get("port") or 0)
    mode = runtime.get("mode") or ""

    if pid and not pid_alive(pid):
        _clear_runtime()
        err = _read_json(_error_path()) or {}
        if err.get("reason"):
            return ServiceStatus(state="error", reason=str(err["reason"]), stale=True, mode=mode, port=port)
        return ServiceStatus(state="stopped", stale=True)

    if pid and not process_is_ours(pid):
        # PID was reused by an unrelated process. Forget it; do not kill it.
        _clear_runtime()
        return ServiceStatus(state="stopped", stale=True, reason="not_ours")

    if not pid:
        err = _read_json(_error_path()) or {}
        if err.get("reason"):
            return ServiceStatus(state="error", reason=str(err["reason"]))
        return ServiceStatus(state="stopped")

    session = str(runtime.get("tmux_session") or "")
    own_pane_id = str(runtime.get("own_pane_id") or "")
    https = bool(runtime.get("https"))
    url = runtime.get("url") or ""
    common = dict(mode=mode, port=port, pid=pid, url=url, https=https, session=session, own_pane_id=own_pane_id)

    if not _health_ok(port):
        return ServiceStatus(state="error", reason="health_failed", **common)

    # A live HTTP process is not enough: the tmux session it was bound to
    # must still exist, or the phone would see an empty (or remote-only)
    # list that looks like a healthy Tower.
    if not session or not tmux_capture.session_exists(session):
        return ServiceStatus(state="error", reason="source_session_missing", **common)

    if mode == "tailscale" and not _tailscale_mapping_ok(port):
        return ServiceStatus(state="error", reason="serve_mapping_missing", **common)

    return ServiceStatus(state="running", **common)


def pairing_info() -> dict:
    runtime = _read_json(_runtime_path()) or {}
    code = runtime.get("pairing_code") or ""
    expires_at = runtime.get("pairing_expires_at")
    expired = not code or not expires_at or time.time() >= float(expires_at)
    return {
        "url": runtime.get("url") or "",
        "code": None if expired else code,
        "expires_at": expires_at,
        "expired": expired,
        "https": bool(runtime.get("https")),
        "session": str(runtime.get("tmux_session") or ""),
    }


def _publish(server, mode: str, port: int, url: str, https: bool, session: str = "", own_pane_id: str = "") -> None:
    snap = server.pairing.snapshot()
    _write_json(
        _runtime_path(),
        {
            "pid": os.getpid(),
            "port": port,
            "mode": mode,
            "url": url,
            "https": https,
            "ready": True,
            "tmux_session": session,
            "own_pane_id": own_pane_id,
            "pairing_code": snap["code"],
            "pairing_expires_at": snap["expires_at"],
        },
    )


def _consume_control(server, detached: bool) -> None:
    control = _control_dir()
    if not control.is_dir():
        return
    for path in list(control.iterdir()):
        name = path.name
        try:
            path.unlink()
        except Exception:
            continue
        if name == "regenerate":
            code = server.pairing.regenerate()
            if not detached:
                print(f"\nNew pairing code: {code}  (valid 5 minutes, one-time use)")
                sys.stdout.flush()
        elif name == "revoke-all":
            server.token_store.revoke_all()
        elif name.startswith("revoke-"):
            server.token_store.revoke_id(name[len("revoke-"):])


def _request_stop(signum, frame) -> None:  # noqa: ARG001
    raise KeyboardInterrupt


_TAILSCALE_ERROR_MESSAGES = {
    "tailscale_status_unavailable": "Could not reach the Windows Tailscale daemon (is Tailscale running and logged in?).",
    "no_magicdns": "MagicDNS is not enabled for this tailnet -- tailscale serve's HTTPS mode has no hostname to use. See docs/REMOTE.md.",
    "serve_command_failed": "The 'tailscale serve' command itself failed. Run it by hand to see the real error.",
}


def _setup_tailscale(server, port: int):
    """Returns ``(tailscale_exe, dns_name)`` on success, ``(None, None)``
    on any failure. Prints a specific reason for the foreground CLI.
    """

    global _last_setup_reason

    exe = tailscale.find_tailscale_exe()
    if not exe:
        _last_setup_reason = "tailscale_missing"
        print("Tailscale was not found (checked the Windows install path and PATH).", file=sys.stderr)
        print("Install Tailscale on Windows and make sure you're logged in, then try again.", file=sys.stderr)
        return None, None

    reachable = tailscale.windows_can_reach_backend(port)
    if reachable is None:
        _last_setup_reason = "windows_unverified"
        print("Could not verify Windows -> WSL connectivity (curl.exe not found).", file=sys.stderr)
        print("Windows localhost에서 Tower Remote에 연결할 수 없습니다 -- 확인할 수 없어 진행하지 않습니다.", file=sys.stderr)
        return None, None
    if not reachable:
        _last_setup_reason = "windows_unreachable"
        print("Windows localhost에서 Tower Remote에 연결할 수 없습니다.", file=sys.stderr)
        print(f"(Windows curl.exe could not reach http://127.0.0.1:{port}/api/health)", file=sys.stderr)
        return None, None

    ok, detail = tailscale.start_serve(exe, port)
    if not ok:
        if detail.startswith("existing_mapping:"):
            _last_setup_reason = "existing_mapping"
            existing = detail.split(":", 1)[1]
            print("A Tailscale Serve mapping already exists at :443, pointing somewhere else:", file=sys.stderr)
            print(f"  {existing}", file=sys.stderr)
            print("Tower will not overwrite it. Free it up yourself (tailscale serve --https=443 off)", file=sys.stderr)
            print("if that mapping is no longer needed, then try again.", file=sys.stderr)
        else:
            _last_setup_reason = detail or "serve_command_failed"
            print(_TAILSCALE_ERROR_MESSAGES.get(detail, f"Tailscale Serve setup failed: {detail}"), file=sys.stderr)
        return None, None

    dns_name = detail
    ts_status = tailscale.get_status(exe)
    self_ip = tailscale.self_tailscale_ip(ts_status) if ts_status else None
    if self_ip:
        server.allowed_hosts.append(self_ip)
    server.allowed_hosts.append(dns_name)
    _last_setup_reason = ""
    return exe, dns_name


def serve_foreground(
    mode: str,
    port: int = DEFAULT_PORT,
    detached: bool = False,
    session: str = "",
    own_pane_id: str = "",
) -> int:
    """Blocking server used by ``tower serve`` and by the detached child.

    Returns a process exit code. ``mode`` is ``local``, ``lan``, or
    ``tailscale``. ``session`` is the tmux session to watch and must be
    resolved by the caller -- this function never asks tmux which session
    "this process" is in.
    """

    previous = signal.signal(signal.SIGTERM, _request_stop)
    try:
        return _serve_foreground(mode, port, detached, session, own_pane_id)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _serve_foreground(mode: str, port: int, detached: bool, session: str, own_pane_id: str) -> int:
    if not session:
        _write_last_error("no_tmux")
        if not detached:
            print(t("cli.no_tmux_session"), file=sys.stderr)
        return 1

    if not tmux_capture.session_exists(session):
        _write_last_error("source_session_missing")
        if not detached:
            print(t("remote.error.source_session_missing"), file=sys.stderr)
        return 1

    use_tailscale = mode == "tailscale"
    lan = mode == "lan"
    host = "0.0.0.0" if lan else "127.0.0.1"
    allowed_hosts = ["localhost", "127.0.0.1"]
    lan_ip = netutil.detect_lan_ip() if lan else None
    if lan_ip:
        allowed_hosts.append(lan_ip)

    try:
        server = httpapi.create_server(
            host, port, session, own_pane_id, allowed_hosts, token_path=CONFIG_DIR / "remote-tokens.json"
        )
    except OSError as exc:
        _write_last_error("port_in_use")
        if not detached:
            print(f"Could not start Tower Remote on port {port}: {exc}", file=sys.stderr)
        return 1

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    tailscale_exe = None
    tailscale_dns_name = None
    if use_tailscale:
        tailscale_exe, tailscale_dns_name = _setup_tailscale(server, port)
        if tailscale_exe is None:
            _write_last_error(_last_setup_reason or "tailscale_setup_failed")
            server.shutdown()
            server.server_close()
            return 1

    if use_tailscale:
        url = f"https://{tailscale_dns_name}/"
        https = True
    elif lan and lan_ip:
        url = f"http://{lan_ip}:{port}"
        https = False
    else:
        url = f"http://127.0.0.1:{port}"
        https = False

    if not detached:
        print("TMUX AGENT TOWER REMOTE")
        print()
        if use_tailscale:
            print("Tailscale: ONLINE")
            print(url)
        else:
            print(f"Local:  http://127.0.0.1:{port}")
            if lan:
                if lan_ip:
                    print(f"LAN:    http://{lan_ip}:{port}")
                else:
                    print("LAN:    (could not detect a LAN IP address on this machine)")
            else:
                print("(localhost only -- pass --lan to allow your phone to connect)")
        print()
        code = server.pairing.current_code()
        print(f"Pairing code: {code}  (valid 5 minutes, one-time use)")
        print("Open the address above on your phone's browser and enter this code.")
        print()
        print("Type 'r' + Enter any time for a new pairing code. Ctrl+C to stop.")
        sys.stdout.flush()
    else:
        server.pairing.current_code()

    _clear_last_error()
    _publish(server, mode, port, url, https, session, own_pane_id)

    if not detached:
        stdin_thread = threading.Thread(target=_watch_stdin_for_regenerate, args=(server,), daemon=True)
        stdin_thread.start()

    exit_code = 0
    try:
        while thread.is_alive():
            _consume_control(server, detached)
            _publish(server, mode, port, url, https, session, own_pane_id)
            time.sleep(0.4)
    except KeyboardInterrupt:
        if not detached:
            print()
            print("Stopping Tower Remote...")
    except Exception:
        exit_code = 1
        _write_last_error("exited")
    finally:
        if use_tailscale and tailscale_exe:
            if tailscale.stop_serve(tailscale_exe, port):
                if not detached:
                    print("Tailscale Serve mapping removed.")
            elif not detached:
                print(
                    "Tailscale Serve mapping was left in place (it no longer points at this backend, "
                    "or removal failed). To remove it by hand: tailscale serve --https=443 off",
                    file=sys.stderr,
                )
        server.shutdown()
        server.server_close()
        _clear_runtime()

    return exit_code


def _watch_stdin_for_regenerate(server) -> None:
    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            return
        if line.strip().lower() == "r":
            new_code = server.pairing.regenerate()
            print(f"\nNew pairing code: {new_code}  (valid 5 minutes, one-time use)")
            sys.stdout.flush()


def _spawn(mode: str, port: int, session: str, own_pane_id: str = "") -> subprocess.Popen:
    """Detached child. The session is an explicit argument so the child
    cannot pick up a different (or since-deleted) one from its environment.
    """

    log_path = CONFIG_DIR / "remote-serve.log"
    _prepare_dir(CONFIG_DIR)
    log_file = open(log_path, "ab")  # noqa: SIM115 -- closed after the child inherits it
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tmux_agent_tower.server.service",
                "--foreground",
                "--detached",
                "--mode",
                mode,
                "--port",
                str(port),
                "--tmux-session",
                session,
                "--own-pane-id",
                own_pane_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()


def start(mode: str = "tailscale", port: int = DEFAULT_PORT, session: str = "", own_pane_id: str = "") -> StartResult:
    """Start a detached Tower Remote bound to ``session``.

    No-op when one is already healthy for the same session. If one is
    alive for a *different* session the result is ``session_mismatch``
    with ``running_session`` set; nothing is stopped or switched
    automatically -- see ``rebind``.

    Never uses ``shell=True``. A start failure is a result, not an exception.
    """

    global _status_cache
    if not session:
        return StartResult(ok=False, reason="no_tmux")

    current = status(force=True)
    alive = bool(current.pid) and pid_alive(current.pid)
    if alive and current.state in ("running", "error") and current.session != session:
        info = pairing_info()
        return StartResult(
            ok=False,
            already_running=True,
            reason="session_mismatch",
            running_session=current.session,
            session=session,
            url=current.url,
            pairing_code=info.get("code") or "",
            https=current.https,
        )
    if current.state == "running":
        info = pairing_info()
        return StartResult(
            ok=True,
            already_running=True,
            url=current.url,
            pairing_code=info.get("code") or "",
            https=current.https,
            tailscale_ok=current.mode == "tailscale",
            session=current.session,
        )
    if current.state == "error" and alive:
        return StartResult(ok=False, reason=current.reason or "health_failed", session=session)

    _clear_runtime()
    _clear_last_error()
    _status_cache = {"at": 0.0, "value": None}

    try:
        proc = _spawn(mode, port, session, own_pane_id)
    except Exception:
        _write_last_error("spawn_failed")
        return StartResult(ok=False, reason="spawn_failed", session=session)

    deadline = time.time() + _START_TIMEOUT_SECONDS
    while time.time() < deadline:
        runtime = _read_json(_runtime_path()) or {}
        if runtime.get("ready") and int(runtime.get("pid") or 0) == proc.pid:
            _status_cache = {"at": 0.0, "value": None}
            return StartResult(
                ok=True,
                url=runtime.get("url") or "",
                pairing_code=runtime.get("pairing_code") or "",
                https=bool(runtime.get("https")),
                tailscale_ok=mode == "tailscale",
                session=str(runtime.get("tmux_session") or session),
            )
        if proc.poll() is not None:
            err = _read_json(_error_path()) or {}
            _status_cache = {"at": 0.0, "value": None}
            return StartResult(ok=False, reason=str(err.get("reason") or "exited"), session=session)
        time.sleep(0.2)

    _status_cache = {"at": 0.0, "value": None}
    return StartResult(ok=False, reason="timeout", session=session)


def rebind(mode: str = "tailscale", port: int = DEFAULT_PORT, session: str = "", own_pane_id: str = "") -> StartResult:
    """Explicit user choice after ``session_mismatch``: stop the remote we
    own, then start one for ``session``. Never called automatically.
    """

    outcome = stop()
    if outcome == "not_ours":
        return StartResult(ok=False, reason="not_ours", session=session)
    return start(mode, port, session=session, own_pane_id=own_pane_id)


def stop() -> str:
    """Stop the Tower Remote we started.

    Returns ``"stopped"``, ``"not_running"``, or ``"not_ours"``. A PID
    that is not a Tower Remote process is never signalled.
    """

    global _status_cache
    runtime = _read_json(_runtime_path()) or {}
    pid = int(runtime.get("pid") or 0)
    port = int(runtime.get("port") or DEFAULT_PORT)
    mode = runtime.get("mode") or ""

    if not pid or not pid_alive(pid):
        _clear_runtime()
        _clear_last_error()
        _status_cache = {"at": 0.0, "value": None}
        return "not_running"

    if not process_is_ours(pid):
        _clear_runtime()
        _status_cache = {"at": 0.0, "value": None}
        return "not_ours"

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return "not_running"

    deadline = time.time() + 5
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)

    if mode == "tailscale":
        # SIGKILL is not used: the child's SIGTERM handler runs the same
        # stop_serve. This second call is a no-op when that already
        # removed the mapping, and a safety net if the child died first.
        exe = tailscale.find_tailscale_exe()
        if exe:
            tailscale.stop_serve(exe, port)

    _clear_runtime()
    _clear_last_error()
    _status_cache = {"at": 0.0, "value": None}
    return "stopped"


def regenerate_pairing(timeout: float = 3.0) -> Optional[dict]:
    """Ask the running server for a new code. Local control file only --
    there is no HTTP endpoint for this.
    """

    if status(force=True).state != "running":
        return None
    before = pairing_info().get("code")
    _prepare_dir(_control_dir())
    (_control_dir() / "regenerate").write_text("1", encoding="utf-8")
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = pairing_info()
        if info.get("code") and info["code"] != before and not info["expired"]:
            return info
        time.sleep(0.1)
    return None


def request_revoke_all() -> None:
    if status(force=True).state == "running":
        _prepare_dir(_control_dir())
        (_control_dir() / "revoke-all").write_text("1", encoding="utf-8")
        return
    httpapi.auth.TokenStore(CONFIG_DIR / "remote-tokens.json").revoke_all()


def request_revoke(device_id: str) -> None:
    if status(force=True).state == "running":
        _prepare_dir(_control_dir())
        (_control_dir() / f"revoke-{device_id}").write_text("1", encoding="utf-8")
        return
    httpapi.auth.TokenStore(CONFIG_DIR / "remote-tokens.json").revoke_id(device_id)


def list_devices() -> list:
    return httpapi.auth.TokenStore(CONFIG_DIR / "remote-tokens.json").list_devices()


def maybe_autostart(session: str = "", own_pane_id: str = "") -> Optional[StartResult]:
    """Used when the TUI starts, for that TUI's own session. Never raises
    -- a failure leaves the badge at ``error`` and the TUI keeps running.

    Without an explicit session nothing is started: autostart must not
    guess which tmux session to watch.
    """

    from ..launcher.config import load_config

    try:
        if not load_config().get("remote_autostart"):
            return None
        if not session:
            return None
        current = status(force=True)
        if current.state == "running" and current.session == session:
            return None
        return start("tailscale", session=session, own_pane_id=own_pane_id)
    except Exception:
        _write_last_error("autostart_failed")
        return StartResult(ok=False, reason="autostart_failed", session=session)


def on_tui_exit() -> None:
    """Quitting the TUI must not stop Remote. Stop is explicit."""

    return


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tmux_agent_tower.server.service")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--detached", action="store_true")
    parser.add_argument("--mode", default="tailscale")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--tmux-session", default="", help="tmux session to watch (required; never inferred here)")
    parser.add_argument("--own-pane-id", default="", help="pane of the Tower that started this remote (excluded from the list)")
    args = parser.parse_args(argv)
    if not args.foreground:
        return 2
    if not args.tmux_session:
        _write_last_error("no_tmux")
        print("--tmux-session is required", file=sys.stderr)
        return 2
    return serve_foreground(
        args.mode, args.port, detached=args.detached, session=args.tmux_session, own_pane_id=args.own_pane_id
    )


if __name__ == "__main__":
    sys.exit(_main())
