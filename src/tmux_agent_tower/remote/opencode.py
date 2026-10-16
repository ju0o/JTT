"""Probe an SSH-bound OpenCode TUI without guessing its active session."""

from __future__ import annotations

import base64
import ipaddress
import json
import shlex
import subprocess
from typing import Dict

from .collector import _normalize_ip, _run_ssh

_MARKER = "__TOWER_OPENCODE_RESULT__"
_MAX_WIRE_BYTES = 6 * 1024 * 1024

# This runs on the remote host and returns only the selected assistant body.
_REMOTE_OPENCODE_SCRIPT = r'''import ipaddress, json, os, re, selectors, subprocess, sys, time
MARKER = "__TOWER_OPENCODE_RESULT__"
PROC = sys.argv[5] if len(sys.argv) > 5 else "/proc"

def emit(status, **fields):
    print(MARKER + json.dumps({"status": status, **fields}, ensure_ascii=True, separators=(",", ":")))

def norm_ip(value):
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        return ""

def proc_ip(value, ipv6):
    try:
        raw = bytes.fromhex(value)
        if ipv6:
            if len(raw) != 16:
                return ""
            raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        else:
            if len(raw) != 4:
                return ""
            raw = raw[::-1]
        return norm_ip(str(ipaddress.ip_address(raw)))
    except ValueError:
        return ""

def connection_live(expected):
    for table, ipv6 in (("tcp", False), ("tcp6", True)):
        try:
            lines = open(os.path.join(PROC, "net", table), encoding="ascii").read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "01":
                continue
            try:
                local_ip, local_port = fields[1].split(":", 1)
                peer_ip, peer_port = fields[2].split(":", 1)
                actual = (proc_ip(peer_ip, ipv6), str(int(peer_port, 16)), proc_ip(local_ip, ipv6), str(int(local_port, 16)))
            except ValueError:
                continue
            if actual == expected:
                return True
    return False

def process(pid):
    base = os.path.join(PROC, pid)
    try:
        raw = open(os.path.join(base, "stat"), "rb").read().decode("ascii", "replace")
        fields = raw[raw.rfind(")") + 2:].split()
        if len(fields) < 6 or fields[0] == "Z":
            return None
        return {
            "ppid": fields[1],
            "pgrp": fields[2],
            "tty_nr": fields[4],
            "tpgid": fields[5],
            "starttime": fields[19],
            "comm": open(os.path.join(base, "comm"), encoding="ascii", errors="replace").read().strip(),
        }
    except (OSError, IndexError):
        return None

# A TUI's argv identifies its launch session, not necessarily the session currently selected in the UI.
def process_argv(pid):
    try:
        raw = open(os.path.join(PROC, pid, "cmdline"), "rb").read().split(b"\0")
        return [part.decode("utf-8", "strict") for part in raw if part]
    except (OSError, UnicodeError):
        return []

def explicit_session_id(argv):
    if not argv:
        return None
    args = argv[1:]
    if args.count("--pure") > 1:
        return None
    if args and args[-1] == "--pure":
        args = args[:-1]
    if len(args) == 2 and args[0] in ("--session", "-s"):
        value = args[1]
    elif len(args) == 1 and args[0].startswith("--session="):
        value = args[0].split("=", 1)[1]
    else:
        return None
    return value if re.fullmatch(r"ses_[A-Za-z0-9_-]{8,128}", value) else None

def env_for(pid, names):
    try:
        result = {}
        for part in open(os.path.join(PROC, pid, "environ"), "rb").read().split(b"\0"):
            key, separator, value = part.partition(b"=")
            name = key.decode("ascii", "ignore")
            if separator and name in names:
                result[name] = "" if name == "OPENCODE_CONFIG_CONTENT" else value.decode("utf-8", "surrogateescape")
        return result
    except (OSError, UnicodeError):
        return {}

def ssh_connection(pid):
    fields = env_for(pid, {"SSH_CONNECTION"}).get("SSH_CONNECTION", "").split()
    if len(fields) != 4:
        return ()
    try:
        return (norm_ip(fields[0]), str(int(fields[1])), norm_ip(fields[2]), str(int(fields[3])))
    except ValueError:
        return ()

def descendants(root, parents):
    children = {}
    for pid, ppid in parents.items():
        children.setdefault(ppid, []).append(pid)
    found, stack = set(), [root]
    # shortcut: cap process ancestry at 4096 entries; raise only if real hosts exceed it.
    while stack and len(found) < 4096:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack.extend(children.get(pid, ()))
    return found, bool(stack)

def run_bounded(argv, cwd, env, limit, timeout):
    child = None
    selector = selectors.DefaultSelector()
    data = bytearray()
    try:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(child.stdout.fileno(), False)
        selector.register(child.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                child.kill()
                child.wait()
                return None, "timeout"
            events = selector.select(min(remaining, 0.25))
            if not events and child.poll() is None:
                continue
            chunk = os.read(child.stdout.fileno(), min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > limit:
                child.kill()
                child.wait()
                return None, "large"
        if child.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            return None, "failed"
        return bytes(data), "ok"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        return None, "failed"
    finally:
        selector.close()
        if child is not None and child.stdout:
            child.stdout.close()

try:
    expected = (norm_ip(sys.argv[1]), str(int(sys.argv[2])), norm_ip(sys.argv[3]), str(int(sys.argv[4])))
except (IndexError, ValueError, UnicodeError):
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
if not all(expected):
    emit("AGENT_SESSION_UNBOUND")
    raise SystemExit(0)
if not connection_live(expected):
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)

processes = {name: item for name in os.listdir(PROC) if name.isdigit() and (item := process(name))}
matching = {pid for pid in processes if ssh_connection(pid) == expected}
roots = [pid for pid in matching if processes[pid]["ppid"] not in matching]
if len(roots) != 1:
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
root = roots[0]
parents = {pid: item["ppid"] for pid, item in processes.items()}
desc, overflow = descendants(root, parents)
if overflow:
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
sshd = ""
parent = root
for _ in range(32):
    item = processes.get(parent)
    if not item:
        break
    if item["comm"].lower().startswith("sshd"):
        sshd = parent
        break
    parent = item["ppid"]
if not sshd:
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
open_codes = [pid for pid in desc if pid in processes and ssh_connection(pid) == expected and processes[pid]["comm"].lower() in ("opencode", "opencode.exe")]
if len(open_codes) != 1:
    emit("REMOTE_PROVIDER_UNBOUND")
    raise SystemExit(0)
pid = open_codes[0]
proc = processes[pid]
root_proc = processes[root]
if proc["tty_nr"] == "0" or proc["tty_nr"] != root_proc["tty_nr"] or proc["pgrp"] != proc["tpgid"]:
    emit("AGENT_SESSION_UNBOUND", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
argv = process_argv(pid)
sid = explicit_session_id(argv)
implicit_tui = (
    not sid and len(argv) == 1
    and os.path.basename(argv[0]).lower() in ("opencode", "opencode.exe")
)
if not sid and not implicit_tui:
    emit("AGENT_SESSION_UNBOUND", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
try:
    exe = os.readlink(os.path.join(PROC, pid, "exe"))
    cwd = os.path.realpath(os.readlink(os.path.join(PROC, pid, "cwd")))
except OSError:
    emit("REMOTE_PROVIDER_UNBOUND", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
exe_name = os.path.basename(exe)
exe_name_lower = exe_name.lower()
comm_name = proc["comm"].lower()
# The supported .exe launcher may report "opencode" as its Linux process comm.
comm_matches_executable = (
    exe_name_lower == comm_name
    or (exe_name_lower == "opencode.exe" and comm_name == "opencode")
)
if exe_name_lower not in ("opencode", "opencode.exe") or not comm_matches_executable or exe.endswith(" (deleted)"):
    emit("REMOTE_PROVIDER_UNAVAILABLE", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
proc_env = env_for(pid, {"HOME", "PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_DATA_DIR", "OPENCODE_DB", "OPENCODE_CONFIG_CONTENT"})
if not cwd or not os.path.isabs(cwd) or not os.path.isabs(proc_env.get("HOME", "")) or proc_env.get("OPENCODE_CONFIG_CONTENT") is not None:
    emit("REMOTE_PROVIDER_UNAVAILABLE", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
env = {"PATH": proc_env.get("PATH") or os.environ.get("PATH", "/usr/bin:/bin"), "HOME": proc_env["HOME"]}
for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_DATA_DIR", "OPENCODE_DB"):
    if key in proc_env:
        env[key] = proc_env[key]
version, state = run_bounded([exe, "--version"], cwd, env, 1024, 2.0)
if state != "ok" or not version or version.decode("utf-8", "replace").strip().splitlines()[0] != "1.19.0":
    emit("REMOTE_PROVIDER_UNAVAILABLE", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
# A TUI's argv identifies its launch session, not necessarily the session currently selected in the UI.
emit(
    "AGENT_SESSION_UNBOUND", remote_session_pid=root, remote_sshd_pid=sshd,
    opencode_pid=pid, session_binding="active_tui_session_unexposed",
)
'''

_REMOTE_STATUSES = {
    "ok", "AGENT_SESSION_UNBOUND", "REMOTE_PROVIDER_UNBOUND", "REMOTE_PROVIDER_UNAVAILABLE",
    "REMOTE_PROVIDER_TIMEOUT", "REMOTE_RESULT_INCOMPLETE", "OUTPUT_RANGE_EXCEEDED",
}


def fetch_remote_opencode_result(
    host_alias: str,
    connection: Dict[str, str],
    timeout: float = 16.0,
) -> Dict | None:
    """Probe a verified OpenCode TUI; fail closed while its active session is not observable."""

    if (
        not host_alias or host_alias.startswith("-") or any(ch.isspace() or not ch.isprintable() for ch in host_alias)
        or not isinstance(connection, dict) or not str(connection.get("ssh_client_pid") or "").isdecimal()
    ):
        return {"status": "AGENT_SESSION_UNBOUND"}
    try:
        client_ip = _normalize_ip(connection["client_ip"])
        client_port = int(connection["client_port"])
        server_ip = _normalize_ip(connection["server_ip"])
        server_port = int(connection["server_port"])
    except (KeyError, TypeError, ValueError, UnicodeError, ipaddress.AddressValueError):
        return {"status": "REMOTE_PROVIDER_UNBOUND"}
    if not (client_ip and server_ip and 1 <= client_port <= 65535 and 1 <= server_port <= 65535):
        return {"status": "REMOTE_PROVIDER_UNBOUND"}
    args = " ".join(shlex.quote(value) for value in (client_ip, str(client_port), server_ip, str(server_port)))
    script = f"python3 -c {shlex.quote(_REMOTE_OPENCODE_SCRIPT)} {args}"
    try:
        completed = _run_ssh(host_alias, script, timeout)
    except subprocess.TimeoutExpired:
        return {"status": "REMOTE_PROVIDER_TIMEOUT"}
    except Exception:
        return {"status": "REMOTE_PROVIDER_UNAVAILABLE"}
    stdout = completed.stdout or ""
    if completed.returncode != 0 or len(stdout.encode("utf-8", "replace")) > _MAX_WIRE_BYTES:
        return {"status": "OUTPUT_RANGE_EXCEEDED" if len(stdout.encode("utf-8", "replace")) > _MAX_WIRE_BYTES else "REMOTE_PROVIDER_UNAVAILABLE"}
    record = next((line[len(_MARKER):] for line in reversed(stdout.splitlines()) if line.startswith(_MARKER)), "")
    try:
        payload = json.loads(record)
    except (TypeError, ValueError):
        return {"status": "REMOTE_PROVIDER_UNAVAILABLE"}
    if not isinstance(payload, dict) or payload.get("status") not in _REMOTE_STATUSES:
        return {"status": "REMOTE_PROVIDER_UNAVAILABLE"}
    if payload.get("status") != "ok":
        return payload
    try:
        body_bytes = base64.b64decode(payload.pop("body_b64"), validate=True)
        body = body_bytes.decode("utf-8", "strict")
    except (KeyError, TypeError, ValueError, UnicodeError):
        return {"status": "REMOTE_RESULT_INCOMPLETE"}
    payload["text"] = body
    return payload
