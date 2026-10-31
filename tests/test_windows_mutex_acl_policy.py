"""Focused mutex ACL policy tests; the native probe is test-owned only."""
from __future__ import annotations

from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
import sys
import uuid

import pytest

import ju0_terminal_tower  # noqa: F401 - installs the guarded namespace
from tmux_agent_tower.terminal import TerminalError
from tmux_agent_tower.terminal import windows_manager as runtime


_CURRENT_SID = "synthetic-current-sid"
_OTHER_SID = "synthetic-other-sid"
_ALLOW_ACE = 0
_DENY_ACE = 1
_GENERIC_ALL = 0x10000000
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x80
_WAIT_TIMEOUT = 0x102


class _FakeAcl:
    def __init__(self, aces=()):
        self.aces = list(aces)

    def GetAceCount(self):
        return len(self.aces)

    def GetAce(self, index):
        return self.aces[index]


class _FakeSecurityDescriptor:
    def __init__(self, owner, acl):
        self.owner = owner
        self.acl = acl

    def GetSecurityDescriptorOwner(self):
        return self.owner

    def GetSecurityDescriptorDacl(self):
        return self.acl


class _FakeHandle:
    def __init__(self, state):
        self.state = state

    def Close(self):
        self.state.closed = True


def _ace(*, kind=_ALLOW_ACE, flags=0, mask=runtime.MUTEX_ALL_ACCESS,
         sid=_CURRENT_SID):
    return ((kind, flags), mask, sid)


def _install_fake_win32(monkeypatch, *, owner=_CURRENT_SID, aces=(),
                        wait_result=_WAIT_OBJECT_0):
    state = SimpleNamespace(
        owner=owner,
        aces=list(aces),
        wait_result=wait_result,
        handle=None,
        closed=False,
        released=False,
        added_ace=None,
        descriptor_owner=None,
    )

    class _AclBuilder:
        def AddAccessAllowedAceEx(self, revision, flags, mask, sid):
            state.added_ace = (revision, flags, mask, sid)

    class _DescriptorBuilder:
        def SetSecurityDescriptorOwner(self, sid, defaulted):
            state.descriptor_owner = (sid, defaulted)

        def SetSecurityDescriptorDacl(self, present, acl, defaulted):
            state.descriptor_dacl = (present, acl, defaulted)

    class _SecurityAttributes:
        pass

    fake_security = ModuleType("win32security")
    fake_security.ACL_REVISION = 2
    fake_security.ACCESS_ALLOWED_ACE_TYPE = _ALLOW_ACE
    fake_security.OWNER_SECURITY_INFORMATION = 1
    fake_security.DACL_SECURITY_INFORMATION = 4
    fake_security.ACL = _AclBuilder
    fake_security.SECURITY_DESCRIPTOR = _DescriptorBuilder
    fake_security.GetKernelObjectSecurity = (
        lambda _handle, _information: _FakeSecurityDescriptor(
            state.owner, _FakeAcl(state.aces)
        )
    )

    fake_event = ModuleType("win32event")
    fake_event.WAIT_ABANDONED = _WAIT_ABANDONED

    def create_mutex(_attributes, _initial_owner, name):
        state.mutex_name = name
        state.handle = _FakeHandle(state)
        return state.handle

    fake_event.CreateMutex = create_mutex
    fake_event.WaitForSingleObject = lambda _handle, _timeout: state.wait_result
    fake_event.ReleaseMutex = lambda _handle: setattr(state, "released", True)

    fake_con = ModuleType("win32con")
    fake_con.GENERIC_ALL = _GENERIC_ALL
    fake_con.WAIT_OBJECT_0 = _WAIT_OBJECT_0
    fake_con.WAIT_TIMEOUT = _WAIT_TIMEOUT

    fake_types = ModuleType("pywintypes")
    fake_types.SECURITY_ATTRIBUTES = _SecurityAttributes

    for name, module in (
        ("pywintypes", fake_types),
        ("win32con", fake_con),
        ("win32event", fake_event),
        ("win32security", fake_security),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setattr(runtime, "_require_windows", lambda: None)
    monkeypatch.setattr(runtime, "_private_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_current_sid", lambda: _CURRENT_SID)
    return state


def _session_dir(tmp_path):
    session_id = uuid.uuid4().hex
    path = tmp_path / session_id
    path.mkdir()
    return session_id, path


def test_mock_current_user_only_mutex_uses_mutex_access_mask(tmp_path, monkeypatch):
    state = _install_fake_win32(monkeypatch, aces=[_ace()])
    session_id, session_dir = _session_dir(tmp_path)

    with runtime._session_metadata_lock(session_id, session_dir):
        assert state.handle is not None

    assert runtime.MUTEX_ALL_ACCESS == 0x001F0001
    assert state.added_ace == (2, 0, runtime.MUTEX_ALL_ACCESS, _CURRENT_SID)
    assert state.released is True
    assert state.closed is True


@pytest.mark.parametrize(
    ("case", "owner", "aces", "message"),
    (
        ("other SID", _CURRENT_SID, [_ace(sid=_OTHER_SID)], "unsafe access list"),
        ("extra ACE", _CURRENT_SID, [_ace(), _ace()], "not private"),
        ("denied ACE", _CURRENT_SID, [_ace(kind=_DENY_ACE)], "unsafe access list"),
        ("nonzero ACE flags", _CURRENT_SID,
         [_ace(flags=1)], "unsafe access list"),
        ("bad mask", _CURRENT_SID, [_ace(mask=_GENERIC_ALL)], "unsafe access list"),
        ("owner mismatch", _OTHER_SID, [_ace()], "not private"),
    ),
)
def test_mock_mutex_acl_rejects_unapproved_security(
        tmp_path, monkeypatch, case, owner, aces, message):
    _install_fake_win32(monkeypatch, owner=owner, aces=aces)
    session_id, session_dir = _session_dir(tmp_path)

    with pytest.raises(TerminalError, match=message):
        with runtime._session_metadata_lock(session_id, session_dir):
            pytest.fail(f"{case} unexpectedly acquired the metadata lock")


@pytest.mark.parametrize(
    ("wait_result", "message", "released"),
    (
        (_WAIT_ABANDONED, "abandoned", True),
        (_WAIT_TIMEOUT, "timed out", False),
    ),
)
def test_mock_mutex_abandoned_and_timeout_fail_closed(
        tmp_path, monkeypatch, wait_result, message, released):
    state = _install_fake_win32(
        monkeypatch, aces=[_ace()], wait_result=wait_result
    )
    session_id, session_dir = _session_dir(tmp_path)

    with pytest.raises(TerminalError, match=message):
        with runtime._session_metadata_lock(session_id, session_dir):
            pytest.fail("an unavailable metadata lock was yielded")

    assert state.released is released
    assert state.closed is True


def test_mock_metadata_generation_mismatch_prevents_write(tmp_path, monkeypatch):
    session_id = uuid.uuid4().hex
    expected_generation = uuid.uuid4().hex
    replacement_generation = uuid.uuid4().hex
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    metadata_path = session_dir / runtime.SESSION_META
    metadata_path.write_text("existing metadata", encoding="utf-8")
    value = {
        "session_id": session_id,
        "generation": expected_generation,
        "state": "stopping",
    }
    writes = []
    monkeypatch.setattr(
        runtime, "_session_metadata_lock", lambda *_args: nullcontext()
    )
    monkeypatch.setattr(
        runtime,
        "_read_metadata",
        lambda _path: {
            "session_id": session_id,
            "generation": replacement_generation,
        },
    )
    monkeypatch.setattr(
        runtime, "_write_metadata_unlocked", lambda *_args: writes.append(True)
    )

    with pytest.raises(TerminalError, match="generation changed"):
        runtime._atomic_metadata(metadata_path, value)

    assert writes == []


def _safe_acl_summary(security, sid, win32security, generic_all):
    owner = security.GetSecurityDescriptorOwner()
    object_acl = security.GetSecurityDescriptorDacl()
    ace_count = 0 if object_acl is None else int(object_acl.GetAceCount())
    summary = {
        "owner_matches_current": bool(owner == sid),
        "ace_count": ace_count,
    }
    if ace_count:
        ace = object_acl.GetAce(0)
        access_mask = int(ace[1]) & 0xFFFFFFFF
        summary.update(
            ace_type=int(ace[0][0]),
            access_mask_hex=f"0x{access_mask:08X}",
            sid_matches_current=bool(ace[2] == sid),
            generic_all_match=bool(
                access_mask == (int(generic_all) & 0xFFFFFFFF)
            ),
            mutex_all_access_match=bool(
                access_mask == runtime.MUTEX_ALL_ACCESS
            ),
        )
    return summary


def _record_acl_summary(record_property, prefix, summary):
    for name, value in summary.items():
        record_property(f"{prefix}_{name}", value)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows mutex probe")
def test_native_mutex_acl_probe_records_only_safe_fields(
        tmp_path, monkeypatch, record_property):
    import win32api
    import pywintypes
    import win32con
    import win32event
    import win32security
    import winerror

    sid = runtime._current_sid()
    security_information = (
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
    )
    generic_acl = win32security.ACL()
    generic_acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION, 0, win32con.GENERIC_ALL, sid
    )
    generic_descriptor = win32security.SECURITY_DESCRIPTOR()
    generic_descriptor.SetSecurityDescriptorOwner(sid, False)
    generic_descriptor.SetSecurityDescriptorDacl(True, generic_acl, False)
    generic_attributes = pywintypes.SECURITY_ATTRIBUTES()
    generic_attributes.SECURITY_DESCRIPTOR = generic_descriptor
    generic_attributes.bInheritHandle = False

    control_name = "Local\\JTT-Mutex-Control-" + str(uuid.uuid4())
    if not callable(getattr(win32api, "SetLastError", None)) or not callable(
            getattr(win32api, "GetLastError", None)):
        pytest.fail("cannot verify that the probe control mutex name is new")
    win32api.SetLastError(0)
    control = win32event.CreateMutex(generic_attributes, False, control_name)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        control.Close()
        pytest.fail("probe control mutex name already exists; refusing to inspect it")
    try:
        generic_security = win32security.GetKernelObjectSecurity(
            control, security_information
        )
        generic_summary = _safe_acl_summary(
            generic_security, sid, win32security, win32con.GENERIC_ALL
        )
    finally:
        control.Close()

    session_id, session_dir = _session_dir(tmp_path)
    runtime._private_acl(session_dir, directory=True, apply=True)
    original_get_security = win32security.GetKernelObjectSecurity
    original_create_mutex = win32event.CreateMutex
    observed = []

    production_name = runtime.SESSION_METADATA_MUTEX_PREFIX + session_id

    def create_new_production_mutex(attributes, initial_owner, name):
        if name != production_name:
            pytest.fail("probe attempted to create an unexpected mutex")
        win32api.SetLastError(0)
        handle = original_create_mutex(attributes, initial_owner, name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            handle.Close()
            pytest.fail(
                "probe production mutex name already exists; refusing to inspect it"
            )
        return handle

    def capture_production_mutex(handle, information):
        security = original_get_security(handle, information)
        observed.append(
            _safe_acl_summary(
                security, sid, win32security, win32con.GENERIC_ALL
            )
        )
        return security

    monkeypatch.setattr(
        win32security, "GetKernelObjectSecurity", capture_production_mutex
    )
    monkeypatch.setattr(win32event, "CreateMutex", create_new_production_mutex)
    try:
        with runtime._session_metadata_lock(session_id, session_dir):
            pass
    except Exception:
        if observed:
            _record_acl_summary(record_property, "production", observed[0])
        pytest.fail(
            "production mutex setup or ACL failed; sanitized ACE observation="
            f"{observed[0] if observed else 'unavailable'}"
        )

    assert len(observed) == 1
    production_summary = observed[0]
    _record_acl_summary(record_property, "control", generic_summary)
    _record_acl_summary(record_property, "production", production_summary)

    assert generic_summary["owner_matches_current"] is True
    assert generic_summary["ace_count"] == 1
    assert generic_summary["ace_type"] == win32security.ACCESS_ALLOWED_ACE_TYPE
    assert generic_summary["sid_matches_current"] is True
    assert (
        generic_summary["generic_all_match"]
        or generic_summary["mutex_all_access_match"]
    )
    assert production_summary["owner_matches_current"] is True
    assert production_summary["ace_count"] == 1
    assert production_summary["ace_type"] == win32security.ACCESS_ALLOWED_ACE_TYPE
    assert production_summary["sid_matches_current"] is True
    assert production_summary["mutex_all_access_match"] is True
