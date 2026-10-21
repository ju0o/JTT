"""Copy text to the system clipboard, with a tmux buffer fallback.

The result body is written to the tool's stdin. It is never placed in
argv, and nothing here uses ``shell=True``. A missing tool is not
reported as a successful copy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .access_context import discover_wayland_display


@dataclass(frozen=True)
class CopyOutcome:
    """``clipboard`` is a clipboard we read back. ``buffer`` is tmux only.

    ``terminal_requested`` means OSC 52 was sent to one identified client
    and the host clipboard did not change. That is not a confirmed paste.
    ``ambiguous`` means more than one client was attached and none was
    the single focused client, so no terminal was chosen.
    """

    clipboard: bool
    buffer: bool
    tool: str
    terminal_requested: bool = False
    ambiguous: bool = False


@dataclass(frozen=True)
class TerminalTarget:
    """The one client that may receive OSC 52, or an explicit ambiguity."""

    tty: Optional[str]
    ambiguous: bool
    ssh: bool


OSC_BYTES = 48 * 1024
_CLIENT_TTY = re.compile(r"^/dev/pts/\d+$")
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


_last_run_note = ""


def _remember_run(note: str) -> None:
    global _last_run_note
    _last_run_note = note


_CLIP_HELPER = (
    "import os, pathlib, subprocess, sys\n"
    "blob, status, program = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "data = pathlib.Path(blob).read_bytes()\n"
    "os.remove(blob)\n"
    "code = subprocess.run([program], input=data, timeout=8).returncode\n"
    "pathlib.Path(status).write_text(str(code), encoding=\"ascii\")\n"
)


def _run_windows_clip(argv: List[str], data: bytes) -> int:
    """Run ``clip.exe`` from the tmux server, not from the Tower pane.

    A child of the curses pane exits 1 and does not change the Windows
    clipboard. The same UTF-16LE bytes succeed when tmux starts the
    process. The payload stays in a private file, not in argv.
    """

    import tempfile

    directory = tempfile.mkdtemp(prefix="tower-clip-")
    blob = os.path.join(directory, "payload.bin")
    status = os.path.join(directory, "status")
    helper = os.path.join(directory, "run.py")
    try:
        os.chmod(directory, 0o700)
        with open(blob, "wb") as handle:
            handle.write(data)
        os.chmod(blob, 0o600)
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(_CLIP_HELPER)
        command = f"{sys.executable} {helper} {blob} {status} {argv[0]}"
        launched = subprocess.run(
            ["tmux", "run-shell", "-b", command],
            capture_output=True,
            timeout=8,
            check=False,
            start_new_session=True,
        )
        if launched.returncode != 0:
            _remember_run(f"tmux={launched.returncode}")
            return 1
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not os.path.exists(status):
            time.sleep(0.05)
        if not os.path.exists(status):
            _remember_run("timeout")
            return 1
        with open(status, encoding="ascii") as handle:
            code = int(handle.read().strip() or "1")
    except Exception as exc:
        _remember_run(type(exc).__name__)
        return 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    _remember_run(f"rc={code}")
    return code


def _default_run(argv: List[str], data: bytes) -> int:
    """Run a clipboard tool. The note is a return code, never the payload."""

    if argv and os.path.basename(argv[0]).lower() == "clip.exe":
        return _run_windows_clip(argv, data)
    # Clipboard owners such as wl-copy fork a background selection server.
    # Captured pipes stay inherited by that server, so run() waits forever
    # even after the parent accepted the payload.
    background_owner = bool(argv) and os.path.basename(argv[0]).lower() in {
        "wl-copy", "xclip", "xsel",
    }
    try:
        options = {
            "input": data,
            "timeout": 8,
            "check": False,
            "start_new_session": True,
        }
        if background_owner:
            options.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            options["capture_output"] = True
        proc = subprocess.run(argv, **options)
    except Exception as exc:
        _remember_run(type(exc).__name__)
        return 1
    err = (proc.stderr or b"").decode("utf-8", "replace")[:80]
    safe = "".join(ch if ch.isascii() and ch.isprintable() else " " for ch in err).strip()
    _remember_run(f"rc={proc.returncode}" + (f" {safe}" if safe else ""))
    return proc.returncode


def _wsl_fallback(name: str) -> Optional[str]:
    """Windows tools when a tmux pane's PATH does not include System32."""

    candidates = {
        "clip.exe": (
            "/mnt/c/Windows/System32/clip.exe",
            "/mnt/c/WINDOWS/System32/clip.exe",
        ),
        "powershell.exe": (
            "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
            "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe",
        ),
        "curl.exe": (
            "/mnt/c/Windows/System32/curl.exe",
            "/mnt/c/WINDOWS/System32/curl.exe",
        ),
    }
    for path in candidates.get(name, ()):
        if os.path.isfile(path):
            return path
    return None


def _find_executable(which: Callable[[str], Optional[str]], name: str) -> Optional[str]:
    """Resolve ``name``. A test double does not see this machine's fallback."""

    found = which(name)
    if found or which is not shutil.which:
        return found
    return _wsl_fallback(name)


def choose_clipboard_tool(
    which: Callable[[str], Optional[str]],
    environ: Dict[str, str],
    system: str,
) -> str:
    """First installed tool for this machine. Empty when none exist."""

    release = environ.get("TOWER_UNAME", "")
    wsl = system == "win32" or "microsoft" in release.lower()
    if wsl and _find_executable(which, "clip.exe"):
        return "clip.exe"
    if system == "darwin" and which("pbcopy"):
        return "pbcopy"
    if environ.get("WAYLAND_DISPLAY") and which("wl-copy"):
        return "wl-copy"
    if which("xclip"):
        return "xclip"
    if which("xsel"):
        return "xsel"
    return ""


def native_clipboard_provider(
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    environ: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
) -> str:
    """Provider with a matching native readback command, or empty."""

    finder = which or shutil.which
    env = _environ() if environ is None else environ
    tool = choose_clipboard_tool(finder, env, system or sys.platform)
    reader = {"wl-copy": "wl-paste", "pbcopy": "pbpaste", "clip.exe": "powershell.exe"}.get(tool)
    return tool if tool and (reader is None or _find_executable(finder, reader)) else ""


def native_clipboard_provider(
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    environ: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
) -> str:
    """Provider with a matching native readback command, or empty."""

    finder = which or shutil.which
    env = _environ() if environ is None else environ
    tool = choose_clipboard_tool(finder, env, system or sys.platform)
    reader = {"wl-copy": "wl-paste", "pbcopy": "pbpaste", "clip.exe": "powershell.exe"}.get(tool)
    return tool if tool and (reader is None or _find_executable(finder, reader)) else ""


def _argv(tool: str, path: str) -> List[str]:
    if tool == "xclip":
        return [path, "-selection", "clipboard"]
    if tool == "xsel":
        return [path, "--clipboard", "--input"]
    return [path]


def _payload(tool: str, text: str) -> bytes:
    # clip.exe reads UTF-16LE from a pipe. UTF-8 Korean becomes mojibake.
    if tool == "clip.exe":
        return text.encode("utf-16le")
    return text.encode("utf-8")


def sole_client_tty(lines: List[str]) -> Optional[str]:
    """The one attached tty, or None when zero or several clients exist.

    Several attached clients do not get a guessed clipboard.
    """

    ttys = [line.strip() for line in lines if line.strip()]
    if len(ttys) != 1 or not _CLIENT_TTY.match(ttys[0]):
        return None
    return ttys[0]


def _client_lines() -> List[str]:
    from .tmux.capture import run_tmux

    command = ["list-clients"]
    pane = os.environ.get("TMUX_PANE", "").strip()
    if pane:
        session = run_tmux(["display-message", "-p", "-t", pane, "#{session_id}"]).strip()
        if not session:
            return []
        command.extend(["-t", session])
    command.extend(["-F", "#{client_tty}\t#{client_pid}\t#{client_flags}"])
    return run_tmux(command).splitlines()


def client_rows(lines: List[str]) -> List[tuple]:
    """Attached tmux clients as ``(tty, pid, flags)``. Unusable rows are dropped."""

    rows = []
    for line in lines:
        parts = line.strip().split("\t")
        if len(parts) < 2 or not parts[1].isdigit() or not _CLIENT_TTY.match(parts[0]):
            continue
        flags = parts[2] if len(parts) > 2 else ""
        rows.append((parts[0], int(parts[1]), flags))
    return rows


def resolve_terminal_target(
    lines: Optional[List[str]] = None,
    ssh_check: Optional[Callable[[int], bool]] = None,
) -> TerminalTarget:
    """Identify the operator only when exactly one tmux client is attached.

    Pane input does not carry a client identity, so SSH ancestry and the
    focused flag cannot identify who pressed Y when clients are shared.
    """

    if lines is None:
        lines = _client_lines()
    rows = client_rows(lines)
    if len(rows) != 1:
        return TerminalTarget(None, bool(rows), False)
    tty, pid, _flags = rows[0]
    check = ssh_check or _pid_came_through_ssh
    return TerminalTarget(tty, False, bool(check(pid)))


def terminal_clipboard_client(
    lines: Optional[List[str]] = None,
    ssh_check: Optional[Callable[[int], bool]] = None,
) -> Optional[str]:
    """TTY for an identified client, or None when there is no safe choice."""

    target = resolve_terminal_target(lines, ssh_check)
    if target.ambiguous:
        return None
    return target.tty


def _pid_came_through_ssh(pid: int) -> bool:
    """True only with SSH evidence on this process or an ancestor."""

    seen = set()
    current = pid
    while current and current not in seen and current > 1:
        seen.add(current)
        if _process_has_ssh_evidence(current):
            return True
        current = _parent_pid(current)
    return False


def _process_has_ssh_evidence(pid: int) -> bool:
    environ = _read_proc(f"/proc/{pid}/environ")
    if b"SSH_CONNECTION=" in environ or b"SSH_CLIENT=" in environ:
        return True
    from .clipboard_bridge import bridge_id_for_process, load_bridge_client

    bridge_id = bridge_id_for_process(pid)
    if bridge_id and load_bridge_client(bridge_id):
        # A private, live registration is explicit evidence that this tmux
        # client came through the registered access-client helper, even when
        # WSL hides the Windows OpenSSH ancestry from /proc.
        return True
    comm = _read_proc(f"/proc/{pid}/comm").decode("utf-8", "replace").strip()
    if comm == "sshd":
        return True
    # SSH to Windows OpenSSH then `wsl` leaves no SSH_CONNECTION in the
    # distro. The WSL session's start time matches that wsl.exe, whose
    # Windows parent chain contains sshd.exe.
    started = _process_start_unix(pid)
    if started is None:
        return False
    return any(abs(started - stamp) <= 2 for stamp in _ssh_wsl_start_seconds())


_ssh_wsl_cache: tuple = (-1000.0, frozenset())


def _ssh_wsl_start_seconds() -> frozenset:
    """Unix start seconds of wsl.exe processes descended from sshd.exe."""

    global _ssh_wsl_cache
    now = time.monotonic()
    if now - _ssh_wsl_cache[0] < 30:
        return _ssh_wsl_cache[1]
    found = _query_ssh_wsl_starts()
    _ssh_wsl_cache = (now, found)
    return found


def _query_ssh_wsl_starts() -> frozenset:
    powershell = _wsl_fallback("powershell.exe")
    if not powershell:
        return frozenset()
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'wsl.exe' } | "
        "ForEach-Object { "
        "$cur = $_; $ssh = $false; $guard = 0; "
        "while ($cur -and $guard -lt 10) { "
        "if ($cur.Name -eq 'sshd.exe') { $ssh = $true; break }; "
        "if ($cur.ParentProcessId -eq 0) { break }; "
        "$cur = Get-CimInstance Win32_Process -Filter \"ProcessId=$($cur.ParentProcessId)\" "
        "-ErrorAction SilentlyContinue; $guard++ }; "
        "if ($ssh) { "
        "$epoch = [DateTime]'1970-01-01Z'; "
        "$local = [DateTime]::SpecifyKind($_.CreationDate, 'Local').ToUniversalTime(); "
        "[int64]($local - $epoch).TotalSeconds } }"
    )
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=12,
            check=False,
            start_new_session=True,
        )
    except Exception:
        return frozenset()
    stamps = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        text = line.strip()
        if text.isdigit():
            stamps.append(int(text))
    return frozenset(stamps)


def _boot_unix() -> Optional[int]:
    raw = _read_proc("/proc/stat").decode("utf-8", "replace")
    for line in raw.splitlines():
        if line.startswith("btime "):
            value = line.split()[-1]
            if value.isdigit():
                return int(value)
    return None


def _process_start_unix(pid: int) -> Optional[int]:
    raw = _read_proc(f"/proc/{pid}/stat").decode("utf-8", "replace")
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    boot = _boot_unix()
    if boot is None:
        return None
    hz = os.sysconf("SC_CLK_TCK") or 100
    return int(boot + int(fields[19]) / hz)


def _parent_pid(pid: int) -> int:
    raw = _read_proc(f"/proc/{pid}/stat").decode("utf-8", "replace")
    end = raw.rfind(")")
    if end < 0:
        return 0
    fields = raw[end + 2 :].split()
    if len(fields) < 2 or not fields[1].isdigit():
        return 0
    return int(fields[1])


def _read_proc(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except Exception:
        return b""


def plain_clipboard_text(text: str) -> str:
    """Drop ANSI sequences and other control bytes. Newlines and tabs stay."""

    cleaned = _ANSI_RE.sub("", text or "")
    kept = []
    for char in cleaned:
        if char in "\n\t" or (ord(char) >= 32 and ord(char) != 127):
            kept.append(char)
    return "".join(kept)


# Same process reads the desktop clipboard it just wrote. A bare
# ``Get-Clipboard`` prints an extra newline, and decoding that stdout as
# anything but UTF-8 turns Korean into U+FFFD. ``Out.Write`` keeps the
# .NET string, including trailing spaces.
_CLIP_SESSION_COMMAND = (
    "$me = (Get-Process -Id $PID).SessionId; "
    "$found = @(Get-Process explorer -ErrorAction SilentlyContinue); "
    "$same = @($found | Where-Object { $_.SessionId -eq $me }); "
    "$other = if ($found.Count -eq 0 -or $same.Count -gt 0) { $me } else { $found[0].SessionId }; "
    "Write-Output ($me.ToString() + ' ' + $other.ToString())"
)
_CLIP_UTF8_COMMAND = (
    "$utf8 = New-Object System.Text.UTF8Encoding $false; "
    "[Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8; "
    "$text = Get-Clipboard -Raw; "
    "if ($null -eq $text) { exit 3 }; "
    "[Console]::Out.Write($text)"
)
_CLIP_UTF8_B64_COMMAND = (
    "$text = Get-Clipboard -Raw; "
    "if ($null -eq $text) { exit 3 }; "
    "$bytes = [System.Text.Encoding]::UTF8.GetBytes($text); "
    "[Convert]::ToBase64String($bytes)"
)


def _capture(argv: List[str]) -> Optional[tuple]:
    """Stdout bytes from a process that does not inherit the Tower tty."""

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=8,
            check=False,
            # Detach from the Tower pty. A console-attached Get-Clipboard
            # can echo a terminal-local buffer while the Windows clipboard
            # other processes see stays empty.
            start_new_session=True,
        )
    except Exception:
        return None
    return proc.returncode, proc.stdout or b""


def _read_process(argv: List[str]) -> Optional[str]:
    # Never inherit the Tower pane tty. PowerShell would block on it.
    captured = _capture(argv)
    if captured is None:
        return None
    code, stdout = captured
    if code != 0 and not stdout:
        return None
    text = _decode_powershell_stdout(stdout)
    if text is None:
        return None
    return text.replace("\ufeff", "")


def normalize_newlines(text: str) -> str:
    """Normalize CRLF to LF; preserve lone CR and every other character."""

    return (text or "").replace("\r\n", "\n")


def _decode_powershell_stdout(data: bytes) -> Optional[str]:
    """UTF-8 stdout, or UTF-16LE when Windows PowerShell piped Unicode.

    Invalid UTF-8 is a reader failure. It is not turned into U+FFFD and
    then compared as if it were the clipboard text.
    """

    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        data = data[2:]
        try:
            return data.decode("utf-16le")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:6]:
        try:
            return data.decode("utf-16le")
        except UnicodeDecodeError:
            return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _powershell_argv(powershell: str, command: str) -> List[str]:
    return [powershell, "-NoProfile", "-Command", command]


def _sessions_differ(text: str) -> bool:
    """True when the reader and the interactive desktop are different sessions."""

    match = re.search(r"(\d+)\s+(\d+)", text or "")
    if not match:
        return False
    reader, desktop = int(match.group(1)), int(match.group(2))
    return reader != desktop


def _post_clipboard_bridge(port: int, token: str, data: bytes) -> bool:
    """Post through this Tower host's loopback end of a reverse SSH tunnel."""

    if not 1 <= int(port) <= 65535 or not token or len(data) > 48 * 1024:
        return False
    powershell = _wsl_fallback("powershell.exe")
    if not powershell:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=10)
        try:
            connection.request(
                "POST", "/copy", body=data,
                headers={"Content-Type": "application/octet-stream", "X-Tower-Token": token},
            )
            response = connection.getresponse()
            result = json.loads(response.read(2048).decode("ascii"))
            return (
                response.status == 200
                and isinstance(result, dict)
                and result.get("success") is True
                and result.get("payload_sha256") == hashlib.sha256(data).hexdigest()
            )
        except (OSError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
            return False
        finally:
            connection.close()
    script = (
        "$ErrorActionPreference='Stop';"
        "$m=New-Object IO.MemoryStream;[Console]::OpenStandardInput().CopyTo($m);"
        "$a=$m.ToArray();$i=[Array]::IndexOf($a,[byte]10);if($i -lt 1){exit 2};"
        "$k=[Text.Encoding]::ASCII.GetString($a,0,$i);"
        "$b=New-Object byte[] ($a.Length-$i-1);"
        "[Array]::Copy($a,$i+1,$b,0,$b.Length);"
        f"$r=[Net.HttpWebRequest]::Create('http://127.0.0.1:{int(port)}/copy');"
        "$r.Method='POST';$r.ContentType='application/octet-stream';$r.Timeout=9000;"
        "$r.ReadWriteTimeout=9000;$r.Headers.Add('X-Tower-Token',$k);"
        "$r.ContentLength=$b.Length;$s=$r.GetRequestStream();"
        "$s.Write($b,0,$b.Length);$s.Close();$q=$r.GetResponse();"
        "$z=New-Object IO.StreamReader($q.GetResponseStream(),[Text.Encoding]::ASCII);"
        "[Console]::Out.Write($z.ReadToEnd());"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=token.encode("ascii") + b"\n" + data,
            capture_output=True,
            timeout=10,
            check=False,
            start_new_session=True,
        )
    except (OSError, UnicodeEncodeError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    text = _decode_powershell_stdout(proc.stdout or b"")
    try:
        result = json.loads(text or "")
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(result, dict)
        and result.get("success") is True
        and result.get("payload_sha256") == hashlib.sha256(data).hexdigest()
    )


_READ_HELPER = (
    "import json, pathlib, subprocess, sys\n"
    "argv = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
    "proc = subprocess.run(argv, capture_output=True, timeout=8)\n"
    "pathlib.Path(sys.argv[2]).write_bytes(proc.stdout or b'')\n"
    "pathlib.Path(sys.argv[3]).write_text(str(proc.returncode), encoding='ascii')\n"
)


def _capture_clipboard(argv: List[str]) -> Optional[tuple]:
    """Read the Windows clipboard from tmux, not from the Tower pane.

    ``Get-Clipboard`` started by the curses pane does not see the desktop
    clipboard that ``clip.exe`` just wrote. The tmux server does.
    """

    import json
    import tempfile

    if not shutil.which("tmux"):
        return _capture(argv)
    directory = tempfile.mkdtemp(prefix="tower-read-")
    request = os.path.join(directory, "argv.json")
    stdout_path = os.path.join(directory, "stdout.bin")
    status = os.path.join(directory, "status")
    helper = os.path.join(directory, "run.py")
    try:
        os.chmod(directory, 0o700)
        with open(request, "w", encoding="utf-8") as handle:
            json.dump(argv, handle)
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(_READ_HELPER)
        launched = subprocess.run(
            ["tmux", "run-shell", "-b", f"{sys.executable} {helper} {request} {stdout_path} {status}"],
            capture_output=True,
            timeout=8,
            check=False,
            start_new_session=True,
        )
        if launched.returncode != 0:
            return None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not os.path.exists(status):
            time.sleep(0.05)
        if not os.path.exists(status):
            return None
        with open(status, encoding="ascii") as handle:
            code = int(handle.read().strip() or "1")
        with open(stdout_path, "rb") as handle:
            return code, handle.read()
    except Exception:
        return None
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _read_windows_clipboard(powershell: str) -> Optional[str]:
    """Clipboard text from the same Windows session that can reach the desktop.

    A different session's clipboard is not read as a content mismatch.
    UTF-8 is the stdout. If that pipe is not UTF-8, the bytes are taken
    from the .NET string instead of a replacement-character decode.
    """

    session = _capture_clipboard(_powershell_argv(powershell, _CLIP_SESSION_COMMAND))
    if session is not None:
        report = _decode_powershell_stdout(session[1]) or ""
        if _sessions_differ(report):
            return None
    utf8 = _capture_clipboard(_powershell_argv(powershell, _CLIP_UTF8_COMMAND))
    if utf8 is not None and utf8[0] == 0:
        # This command sets UTF-8. A UTF-16 guess here would split Korean.
        try:
            return utf8[1].decode("utf-8")
        except UnicodeDecodeError:
            pass
    encoded = _capture_clipboard(_powershell_argv(powershell, _CLIP_UTF8_B64_COMMAND))
    if encoded is None or encoded[0] != 0:
        return None
    raw = _decode_powershell_stdout(encoded[1]) or ""
    try:
        import base64

        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return None


def _read_tool_clipboard(tool: str, which: Callable[[str], Optional[str]]) -> Optional[str]:
    """Read the clipboard that ``tool`` writes. A different clipboard is not proof.

    ``clip.exe`` is the Windows clipboard. Wayland ``wl-paste`` is not that
    clipboard and is not the tmux client's terminal clipboard.
    """

    if tool == "clip.exe":
        powershell = _find_executable(which, "powershell.exe")
        if not powershell:
            return None
        return _read_windows_clipboard(powershell)
    if tool == "wl-copy":
        if not which("wl-paste"):
            return None
        return _read_process(["wl-paste", "-n"])
    if tool == "xclip":
        path = which("xclip")
        if not path:
            return None
        return _read_process([path, "-selection", "clipboard", "-o"])
    if tool == "xsel":
        path = which("xsel")
        if not path:
            return None
        return _read_process([path, "--clipboard", "--output"])
    if tool == "pbcopy":
        path = which("pbpaste")
        if not path:
            return None
        return _read_process([path])
    return None


def _host_matches(tool: str, text: str, which: Callable[[str], Optional[str]]) -> bool:
    """True only when the clipboard written by ``tool`` contains ``text``."""

    expected = normalize_newlines(text)
    for _attempt in (1, 2):
        got = _read_tool_clipboard(tool, which)
        if got is not None and normalize_newlines(got) == expected:
            return True
        if _attempt == 1:
            # clip.exe can return before the desktop clipboard is visible.
            time.sleep(0.25)
    return False


def _buffer_name() -> str:
    """One private tmux buffer per copy. Never the session default buffer."""

    return f"tower-copy-{os.getpid()}-{time.monotonic_ns()}"


def _show_named_buffer(name: str) -> Optional[bytes]:
    """Exact bytes in one named buffer. ``None`` when tmux cannot show it."""

    try:
        proc = subprocess.run(
            ["tmux", "show-buffer", "-b", name],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def copy_text(
    text: str,
    *,
    which: Optional[Callable[[str], Optional[str]]] = None,
    run: Optional[Callable[[List[str], bytes], int]] = None,
    environ: Optional[Dict[str, str]] = None,
    system: Optional[str] = None,
    client: Optional[str] = None,
    ambiguous: bool = False,
    ssh: bool = False,
    destination: str = "auto",
    bridge_port: Optional[int] = None,
    bridge_token: Optional[str] = None,
    verify: Optional[Callable[[str, str], bool]] = None,
    read_buffer: Optional[Callable[[str], Optional[bytes]]] = None,
    allow_buffer_fallback: bool = True,
    diagnostic_callback: Optional[Callable[[str, str, int, str], bool]] = None,
) -> CopyOutcome:
    """Copy ``text`` to one destination.

    ``destination`` is ``auto``, ``local_host``, ``current_terminal``, or
    ``tmux_buffer``. Auto with an SSH client without a verified terminal
    transport uses the private Tower buffer. Another identified client
    uses the host clipboard. Auto with no single client writes nothing.
    An explicit choice writes only that choice. A second store is used
    only after the chosen write fails, and the private buffer is never the
    default tmux buffer.
    """

    finder = which or shutil.which
    live = run is None and verify is None
    runner = run or _default_run
    env = environ if environ is not None else _environ()
    platform = system or sys.platform
    body = plain_clipboard_text(text)
    data = body.encode("utf-8")

    def before_write(kind: str, tool: str) -> bool:
        if diagnostic_callback is None:
            return True
        try:
            return diagnostic_callback(kind, tool, len(data), hashlib.sha256(data).hexdigest()) is not False
        except Exception:
            return False

    def accepted(tool: str) -> bool:
        if verify is not None:
            return verify(tool, body)
        if not live:
            # Injected runners cover the exit-code path. Terminal success
            # still requires an explicit verifier, because OSC 52 can
            # return 0 without changing a clipboard.
            return tool != "terminal"
        return _host_matches(tool, body, finder)

    def remember(name: str) -> Optional[bytes]:
        if read_buffer is not None:
            return read_buffer(name)
        if not live:
            return data
        return _show_named_buffer(name)

    def drop(name: str) -> None:
        runner(["tmux", "delete-buffer", "-b", name], b"")

    def keep_buffer() -> CopyOutcome:
        name = _buffer_name()
        if runner(["tmux", "load-buffer", "-b", name, "-"], data) != 0:
            return CopyOutcome(False, False, "tmux", False, ambiguous)
        if remember(name) != data:
            drop(name)
            return CopyOutcome(False, False, "tmux", False, ambiguous)
        return CopyOutcome(False, True, "tmux", False, ambiguous)

    def send_terminal(client_tty: str) -> CopyOutcome:
        _audit("terminal", data)
        name = _buffer_name()
        if runner(["tmux", "load-buffer", "-b", name, "-"], data) != 0:
            return CopyOutcome(False, False, "none", False, ambiguous)
        if remember(name) != data:
            drop(name)
            return CopyOutcome(False, False, "none", False, ambiguous)
        if not before_write("current_terminal", "tmux-osc52"):
            drop(name)
            return CopyOutcome(False, False, "none", False, ambiguous)
        code = runner(["tmux", "load-buffer", "-w", "-b", name, "-t", client_tty, "-"], data)
        drop(name)
        if code != 0:
            return keep_buffer()
        if verify is not None and verify("terminal", body):
            return CopyOutcome(True, False, "terminal", False, ambiguous)
        return CopyOutcome(False, False, "terminal", True, ambiguous)

    def write_host() -> CopyOutcome:
        tool = choose_clipboard_tool(finder, env, platform)
        if tool:
            path = _find_executable(finder, tool) or tool
            _audit("host", data)
            if not before_write("local_host", os.path.basename(path)):
                return CopyOutcome(False, False, tool, False, False)
            wrote = runner(_argv(tool, path), _payload(tool, body)) == 0
            if wrote and accepted(tool):
                return CopyOutcome(True, False, tool, False, False)
            if not allow_buffer_fallback:
                return CopyOutcome(False, False, tool, False, False)
            why = "readback" if wrote else ("clip " + _last_run_note).strip()
            _audit("buffer", data, why)
            return keep_buffer()
        if not allow_buffer_fallback:
            return CopyOutcome(False, False, "none", False, False)
        _audit("buffer", data, "no-tool")
        return keep_buffer()

    def write_terminal() -> CopyOutcome:
        # An unidentified terminal never receives a guessed terminal write.
        if ambiguous or not client:
            return CopyOutcome(False, False, "none", False, True)
        if bridge_port is not None or bridge_token is not None:
            if bridge_port is None or not bridge_token or len(data) > 48 * 1024:
                return CopyOutcome(False, False, "none", False, ambiguous)
            _audit("terminal", data)
            if not before_write("current_terminal", "clipboard-bridge"):
                return CopyOutcome(False, False, "none", False, ambiguous)
            if _post_clipboard_bridge(bridge_port, bridge_token, data):
                return CopyOutcome(True, False, "terminal", False, ambiguous)
            # The bridge may have accepted the request before a response was
            # lost. Never write a second destination to recover that result.
            return CopyOutcome(False, False, "none", False, ambiguous)
        if len(data) > OSC_BYTES:
            _audit("buffer", data, "too-big")
            return keep_buffer()
        return send_terminal(client)

    mode = destination or "auto"
    if mode == "tmux_buffer":
        _audit("buffer", data)
        return keep_buffer()
    if mode == "current_terminal":
        return write_terminal()
    if mode == "local_host":
        return write_host()
    if mode != "auto" or ambiguous:
        return CopyOutcome(False, False, "none", False, ambiguous)
    if client and ssh:
        if bridge_port is not None and bridge_token:
            return write_terminal()
        _audit("buffer", data, "no-verified-terminal-transport")
        return keep_buffer()
    return write_host()


def _audit(dest: str, data: bytes, note: str = "") -> None:
    """Lengths and digest only. The result body is not written."""

    if os.environ.get("TOWER_COPY_AUDIT") != "1":
        return


def copy_host_clipboard(text: str) -> CopyOutcome:
    """Write and verify one native clipboard destination, with no second store."""

    return copy_text(text, destination="local_host", allow_buffer_fallback=False)
    try:
        text = data.decode("utf-8")
    except Exception:
        text = ""
    extra = f" why={note}" if note else ""
    line = (
        f"dest={dest} chars={len(text)} utf8={len(data)} "
        f"sha256={hashlib.sha256(data).hexdigest()}{extra}\n"
    )
    try:
        audit_path = os.environ.get("TOWER_COPY_AUDIT_FILE", "/tmp/tower-copy-audit.txt")
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        return


def _environ() -> Dict[str, str]:
    release = ""
    try:
        release = os.uname().release
    except Exception:
        release = ""
    env = dict(os.environ)
    env["TOWER_UNAME"] = release
    if not env.get("WAYLAND_DISPLAY"):
        display = discover_wayland_display(env)
        if display:
            env["XDG_RUNTIME_DIR"], env["WAYLAND_DISPLAY"] = display
    return env
