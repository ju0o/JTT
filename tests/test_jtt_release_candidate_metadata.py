import re
from pathlib import Path

from tmux_agent_tower import (
    DISTRIBUTION_NAME,
    JTT_VERSION,
    LEGACY_TOWER_VERSION,
    __version__ as legacy_tower_version,
    jtt,
    jtt_update,
)


ROOT = Path(__file__).resolve().parents[1]
RC3_TAG = "jtt-v0.3.0-rc3"
RC3_PACKAGE_VERSION = "0.3.0rc3"


def test_new_distribution_candidate_has_distinct_version_and_jtt_entrypoint():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project)
    name = re.search(r'(?m)^name\s*=\s*"([^"]+)"\s*$', project)

    assert match is not None
    assert name is not None
    assert match.group(1) == JTT_VERSION == "0.3.1rc0"
    assert name.group(1) == DISTRIBUTION_NAME == "ju0-terminal-tower"
    assert match.group(1) != RC3_PACKAGE_VERSION
    assert 'jtt = "ju0_terminal_tower.bootstrap:main"' in project
    assert '\ntower = ' not in project

    parsed_tag = jtt_update._parse_jtt_tag(RC3_TAG)
    assert parsed_tag.package == RC3_PACKAGE_VERSION
    assert parsed_tag.prerelease is True

    candidate_tag = jtt_update._parse_jtt_tag("jtt-v0.3.1-rc0")
    assert candidate_tag.package == match.group(1)


def test_jtt_source_version_is_separate_from_preserved_legacy_tower_version(capsys):
    assert legacy_tower_version == LEGACY_TOWER_VERSION == "0.3.0rc3"
    assert jtt.cli(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"jtt {JTT_VERSION} [DEV]"


def test_official_rc3_identity_stays_distinct_and_legacy_tower_release_is_rejected():
    parsed_release = jtt_update._parse_jtt_release_item({
        "tag_name": RC3_TAG,
        "draft": False,
        "prerelease": True,
        "published_at": "2026-10-22T00:00:00Z",
    })
    draft_candidate = jtt_update._parse_jtt_release_item({
        "tag_name": RC3_TAG,
        "draft": True,
        "prerelease": True,
    })
    legacy_tower_release = jtt_update._parse_jtt_release_item({"tag_name": "v0.3.0-rc2"})

    assert parsed_release is not None
    assert parsed_release.tag == RC3_TAG
    assert parsed_release.package_version == RC3_PACKAGE_VERSION
    assert draft_candidate is None
    assert legacy_tower_release is None
