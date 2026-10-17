from tmux_agent_tower.tmux import discovery


def test_own_pane_is_excluded_by_id_not_window_name(monkeypatch):
    # Regression: exclusion must key off pane_id, not window name -- a
    # window can be renamed or reused for something unrelated while
    # keeping an old name, and must never be excluded/matched by that name
    # alone (real bug: a window that used to host Tower, renamed away from
    # "CONTROL" or reused for something else, either wrongly hid its real
    # content or wrongly kept swallowing navigation).
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "SomeRenamedWindow", "0", "%1", "title", "bash", "/tmp", "123", "0", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")
    rows = discovery.list_panes("sess", exclude_pane_id="%1")
    assert rows == []


def test_a_window_named_control_is_not_specially_excluded(monkeypatch):
    # The old design excluded anything in a window literally named
    # "CONTROL". That must be gone: only the exact registered pane_id is
    # excluded, regardless of what any window is named.
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "CONTROL", "0", "%1", "title", "bash", "/tmp", "123", "0", "0"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")
    rows = discovery.list_panes("sess", exclude_pane_id="%999")  # Tower is actually pane %999
    assert len(rows) == 1
    assert rows[0]["pane_id"] == "%1"


def test_no_exclude_pane_id_excludes_nothing(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "workstation-a", "0", "%1", "title", "bash", "/tmp", "123", "0", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")
    rows = discovery.list_panes("sess")
    assert len(rows) == 1


def test_malformed_line_is_skipped(monkeypatch):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: "not-enough-fields")
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert rows == []


def test_empty_tmux_output_returns_empty_list(monkeypatch):
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: "")
    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert rows == []


def test_dead_pane_skips_capture(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@1", "0", "workstation-a", "0", "%1", "title", "bash", "/tmp", "123", "1", "0"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery, "discover_project", lambda path: "proj")

    called = {"capture_pane": False}

    def fake_capture_pane(pane_id, lines=30):
        called["capture_pane"] = True
        return ["should not be called"]

    monkeypatch.setattr(discovery.capture, "capture_pane", fake_capture_pane)

    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert len(rows) == 1
    assert rows[0]["dead"] is True
    assert rows[0]["lines"] == []
    assert called["capture_pane"] is False


def test_valid_pane_parsed_correctly(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@9", "0", "workstation-a", "2", "%9", "My Title", "codex", "/home/user", "555", "0", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {"555": "/usr/bin/codex"})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: ["hello"])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "x")

    rows = discovery.list_panes("sess", exclude_pane_id="%999")
    assert len(rows) == 1
    row = rows[0]
    assert row["pane_id"] == "%9"
    assert row["command"] == "codex"
    assert row["cmdline"] == "/usr/bin/codex"
    assert row["lines"] == ["hello"]
    assert row["auto_project"] == "x"
    assert row["session"] == "sess"
    assert row["window_id"] == "@9"
    assert row["window_index"] == "0"
    assert row["window_name"] == "workstation-a"
    assert row["pane_index"] == "2"
    assert row["pane_active"] is True


def test_tower_runtime_is_detected_in_pane_process_tree(monkeypatch):
    fs = discovery.FIELD_SEP
    line = fs.join(["sess", "@9", "0", "Tower", "2", "%9", "workstation-a", "python3", "/work/sample", "100", "0", "1"])
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {
        "100": "bash", "101": "/usr/bin/python3 -m tmux_agent_tower.main",
    })
    monkeypatch.setattr(discovery.process_detection, "ppid_by_pid", lambda: {"101": "100"})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "project")
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    row = discovery.list_panes("sess")[0]

    assert row["tower_runtime"] is True


def test_tmux_34_octal_separator_is_parsed(monkeypatch):
    line = r"sess\037@9\0370\037workstation-a\0372\037%9\037My Title\037codex\037/home/user\037555\0370\0371"
    monkeypatch.setattr(discovery.capture, "run_tmux", lambda args: line)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.process_detection, "ppid_by_pid", lambda: {})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: ["hello"])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "x")

    rows = discovery.list_panes("sess")

    assert len(rows) == 1
    assert rows[0]["pane_id"] == "%9"
    assert rows[0]["pane_pid"] == "555"


def test_isolated_discovery_lists_panes_server_wide_without_losing_session_identity(monkeypatch):
    fs = discovery.FIELD_SEP
    output = "\n".join(
        [
            fs.join(["main", "@9", "0", "Work", "0", "%9", "Main", "bash", "/work", "555", "0", "1"]),
            fs.join(["sidecar", "@10", "0", "Tower UX", "0", "%10", "Tower", "python", "/work", "556", "0", "1"]),
        ]
    )
    calls = []

    def run_tmux(args):
        calls.append(args)
        return output

    monkeypatch.setattr(discovery.capture, "run_tmux", run_tmux)
    monkeypatch.setattr(discovery.process_detection, "cmdline_by_pid", lambda: {})
    monkeypatch.setattr(discovery.process_detection, "ppid_by_pid", lambda: {})
    monkeypatch.setattr(discovery.capture, "capture_pane", lambda pane_id, lines=30: [pane_id])
    monkeypatch.setattr(discovery, "discover_project", lambda path: "project")
    monkeypatch.setattr(discovery, "git_project_name", lambda path: None)

    rows = discovery.list_panes("sidecar", exclude_pane_id="%10", all_sessions=True)

    assert calls == [["list-panes", "-a", "-F", discovery._PANE_FORMAT]]
    assert [(row["session"], row["pane_id"]) for row in rows] == [("main", "%9")]


def test_isolated_discovery_lists_windows_server_wide_and_default_remains_scoped(monkeypatch):
    fs = discovery.FIELD_SEP
    output = "\n".join(
        [
            fs.join(["main", "$1", "@9", "0", "Work", "100"]),
            fs.join(["sidecar", "$2", "@10", "0", "Tower UX", "200"]),
        ]
    )
    calls = []
    def run_tmux(args):
        calls.append(args)
        if "-t" in args:
            return fs.join(["sidecar", "$2", "@10", "0", "Tower UX", "200"])
        return output

    monkeypatch.setattr(discovery.capture, "run_tmux", run_tmux)

    all_windows = discovery.list_windows("sidecar", all_sessions=True)
    scoped_windows = discovery.list_windows("sidecar")

    assert calls == [
        ["list-windows", "-a", "-F", discovery._WINDOW_FORMAT],
        ["list-windows", "-t", "sidecar", "-F", discovery._WINDOW_FORMAT],
    ]
    assert [item["session"] for item in all_windows] == ["main", "sidecar"]
    assert [item["session"] for item in scoped_windows] == ["sidecar"]
