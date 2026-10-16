"""Read a complete OpenCode result only when the TUI session is process-bound."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import shlex
import subprocess
from typing import Dict

from .collector import _normalize_ip, _run_ssh

_MARKER = "__TOWER_OPENCODE_RESULT__"
_MAX_WIRE_BYTES = 6 * 1024 * 1024

# Remote helper returns validated binding metadata and a bounded, base64-encoded Result body.
_REMOTE_OPENCODE_SCRIPT = r'''import base64, hashlib, ipaddress, json, os, re, selectors, stat, subprocess, sys, time
MARKER = "__TOWER_OPENCODE_RESULT__"
PROC = sys.argv[5] if len(sys.argv) > 5 else "/proc"
BINDING_DIR = sys.argv[6] if len(sys.argv) > 6 else "/tmp"
MAX_EXPORT = 32 * 1024 * 1024
MAX_BODY = 4 * 1024 * 1024

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

def binding_record(pid, process_info):
    path = os.path.join(BINDING_DIR, "tower-opencode-session-%s.json" % pid)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            owner = os.stat(os.path.join(PROC, pid)).st_uid
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096
            ):
                return None
            raw = stream.read(4097)
        if len(raw) > 4096:
            return None
        value = json.loads(raw.decode("utf-8", "strict"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    observed = value.get("observed_at_ms")
    now_ms = int(time.time() * 1000)
    if (
        value.get("schema") != "tower-opencode-tui-binding/v1"
        or value.get("pid") != int(pid)
        or value.get("process_starttime") != process_info.get("starttime")
        or value.get("route_name") not in ("home", "session", "other")
        or isinstance(value.get("generation"), bool)
        or not isinstance(value.get("generation"), int) or value["generation"] < 1
        or isinstance(observed, bool) or not isinstance(observed, int)
        or observed > now_ms + 500 or now_ms - observed > 2000
        or not isinstance(value.get("nonce"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["nonce"])
    ):
        return None
    if value["route_name"] == "session":
        session_id = value.get("session_id")
        if not isinstance(session_id, str) or not re.fullmatch(r"ses_[A-Za-z0-9_-]{8,128}", session_id):
            return None
    elif value.get("session_id") is not None:
        return None
    return value

def session_body(exported, session_id, cwd):
    if not isinstance(exported, dict):
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    info = exported.get("info")
    messages = exported.get("messages")
    if not isinstance(info, dict) or info.get("id") != session_id or not isinstance(messages, list):
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    try:
        if os.path.realpath(info.get("directory", "")) != cwd:
            return "AGENT_SESSION_UNBOUND", "", {}
    except (TypeError, OSError):
        return "AGENT_SESSION_UNBOUND", "", {}
    revert = info.get("revert")
    revert_id = revert.get("messageID") if isinstance(revert, dict) else None
    if revert_id:
        index = next((i for i, item in enumerate(messages) if isinstance(item, dict) and isinstance(item.get("info"), dict) and item["info"].get("id") == revert_id), None)
        if index is None:
            return "REMOTE_RESULT_INCOMPLETE", "", {}
        messages = messages[:index]
    last_user_index = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], dict) and isinstance(messages[i].get("info"), dict) and messages[i]["info"].get("role") == "user"), None)
    if last_user_index is None:
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    user_info = messages[last_user_index]["info"]
    user_id = user_info.get("id")
    if user_info.get("sessionID") != session_id or not isinstance(user_id, str) or not user_id.startswith("msg_"):
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    assistant_index = next((i for i in range(len(messages) - 1, last_user_index, -1) if isinstance(messages[i], dict) and isinstance(messages[i].get("info"), dict) and messages[i]["info"].get("role") == "assistant"), None)
    if assistant_index is None:
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    assistant = messages[assistant_index]
    message = assistant["info"]
    if message.get("sessionID") != session_id or message.get("parentID") != user_id:
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    msg_time = message.get("time")
    completed = msg_time.get("completed") if isinstance(msg_time, dict) else None
    if isinstance(completed, bool) or not isinstance(completed, (int, float)) or completed < 0:
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    if message.get("error") or message.get("finish") != "stop":
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    parts = assistant.get("parts")
    if not isinstance(parts, list):
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    text_parts = []
    for part in parts:
        if not isinstance(part, dict):
            return "REMOTE_RESULT_INCOMPLETE", "", {}
        if part.get("type") == "tool":
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") in ("pending", "running"):
                return "REMOTE_RESULT_INCOMPLETE", "", {}
        if part.get("type") == "text":
            value = part.get("text")
            if part.get("messageID") != message.get("id") or part.get("sessionID") != session_id or not isinstance(value, str):
                return "REMOTE_RESULT_INCOMPLETE", "", {}
            text_parts.append(value)
    if not text_parts:
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    # Text parts are sequential fragments of one message; inserting separators changes source bytes.
    body = "".join(text_parts)
    try:
        body_bytes = body.encode("utf-8", "strict")
    except UnicodeError:
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    if not body.strip():
        return "REMOTE_RESULT_INCOMPLETE", "", {}
    if len(body_bytes) > MAX_BODY:
        return "OUTPUT_RANGE_EXCEEDED", "", {}
    return "ok", body, {
        "session_id": session_id, "user_id": user_id, "message_id": message.get("id"),
        "native_length": len(body), "native_utf8_bytes": len(body_bytes),
        "native_sha256": hashlib.sha256(body_bytes).hexdigest(),
    }

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
before = binding_record(pid, proc)
if before is None:
    emit(
        "AGENT_SESSION_UNBOUND", remote_session_pid=root, remote_sshd_pid=sshd,
        opencode_pid=pid, session_binding="TUI_BINDING_UNAVAILABLE",
    )
    raise SystemExit(0)
if before["route_name"] != "session":
    emit(
        "AGENT_SESSION_UNBOUND", remote_session_pid=root, remote_sshd_pid=sshd,
        opencode_pid=pid, session_binding="NO_ACTIVE_SESSION",
    )
    raise SystemExit(0)
session_id = before["session_id"]
exported, export_state = run_bounded([exe, "export", session_id], cwd, env, MAX_EXPORT, 8.0)
if export_state == "timeout":
    emit("REMOTE_PROVIDER_TIMEOUT", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
if export_state == "large":
    emit("OUTPUT_RANGE_EXCEEDED", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
if export_state != "ok" or exported is None:
    emit("REMOTE_RESULT_INCOMPLETE", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
try:
    export_value = json.loads(exported.decode("utf-8", "strict"))
except (UnicodeError, ValueError):
    emit("REMOTE_RESULT_INCOMPLETE", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)

after = binding_record(pid, proc)
current_proc = process(pid)
current_root = process(root)
current_sshd = process(sshd)
if (
    after is None or current_proc is None or current_proc["starttime"] != proc["starttime"]
    or current_root is None or current_root["starttime"] != root_proc["starttime"]
    or current_sshd is None or current_sshd["starttime"] != processes[sshd]["starttime"]
    or current_proc["tty_nr"] != root_proc["tty_nr"] or current_proc["pgrp"] != current_proc["tpgid"]
    or ssh_connection(pid) != expected or not connection_live(expected)
):
    emit("REMOTE_PROVIDER_UNBOUND", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid)
    raise SystemExit(0)
binding_identity = lambda item: (
    item.get("pid"), item.get("process_starttime"), item.get("route_name"),
    item.get("session_id"), item.get("generation"), item.get("nonce"),
)
if binding_identity(before) != binding_identity(after):
    emit(
        "REMOTE_RESULT_INCOMPLETE", remote_session_pid=root, remote_sshd_pid=sshd,
        opencode_pid=pid, reason_code="SESSION_BINDING_CHANGED",
    )
    raise SystemExit(0)
result_status, body, result_fields = session_body(export_value, session_id, cwd)
if result_status != "ok":
    emit(
        result_status, remote_session_pid=root, remote_sshd_pid=sshd,
        opencode_pid=pid, reason_code=result_status,
    )
    raise SystemExit(0)
body_bytes = body.encode("utf-8", "strict")
emit(
    "ok", remote_session_pid=root, remote_sshd_pid=sshd, opencode_pid=pid,
    ssh_connection=list(expected), session_binding="route.current",
    turn_complete=True, body_complete=True, body_b64=base64.b64encode(body_bytes).decode("ascii"),
    **result_fields,
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
    """Read a verified TUI result only while its active-session binding stays stable."""

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
    native_length = payload.get("native_length")
    native_bytes = payload.get("native_utf8_bytes")
    if (
        isinstance(native_length, bool) or not isinstance(native_length, int) or native_length != len(body)
        or isinstance(native_bytes, bool) or not isinstance(native_bytes, int) or native_bytes != len(body_bytes)
        or payload.get("native_sha256") != hashlib.sha256(body_bytes).hexdigest()
    ):
        return {"status": "REMOTE_RESULT_INCOMPLETE"}
    payload["text"] = body
    return payload
