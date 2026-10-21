import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "src/tmux_agent_tower/remote/opencode_session_binding.js"
INSTALL_DOC = ROOT / "docs/OPENCODE_SESSION_BINDING.md"


def test_opencode_binding_is_a_versioned_tui_plugin_with_stable_id():
    source = PLUGIN.read_text(encoding="utf-8")

    assert re.search(
        r'export default\s*\{\s*id:\s*"tower-opencode-session-binding"\s*,\s*tui:\s*async\s*\(api\)\s*=>',
        source,
    )
    assert "server:" not in source
    assert 'new Set(["1.19.0", "1.19.1"])' in source


def test_install_guide_keeps_tui_file_outside_server_plugin_discovery():
    guide = INSTALL_DOC.read_text(encoding="utf-8")
    expected_url = (
        "file:///home/REPLACE_WITH_USER/.config/opencode/"
        "tower-tui/opencode_session_binding.js"
    )

    assert expected_url in guide
    assert "`{plugin,plugins}/*.{ts,js}`" in guide
    assert "Migrating an existing `plugins/` installation" in guide
    assert re.search(r"move only the Tower\s+file", guide)
    assert "Do not move the old TUI module back into `plugin/` or `plugins/`" in guide
