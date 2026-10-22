import re
from pathlib import Path

from tmux_agent_tower import __version__, jtt_update


ROOT = Path(__file__).resolve().parents[1]
RC3_TAG = "jtt-v0.3.0-rc3"
RC3_PACKAGE_VERSION = "0.3.0rc3"


def test_rc3_distribution_version_matches_jtt_release_tag():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project)

    assert match is not None
    assert match.group(1) == __version__ == RC3_PACKAGE_VERSION

    parsed_tag = jtt_update._parse_jtt_tag(RC3_TAG)
    assert parsed_tag.package == RC3_PACKAGE_VERSION
    assert parsed_tag.prerelease is True


def test_rc3_candidate_stays_in_jtt_release_namespace():
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
