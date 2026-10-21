import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIXTURES = ROOT / "fixtures"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import pytest

from tmux_test_safety import create_isolated_tmux_test_root


def load_fixture(name):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text.split("\n")


@pytest.fixture
def fixture_lines():
    return load_fixture


@pytest.fixture(autouse=True)
def tmux_test_safety(monkeypatch):
    """Route test tmux processes to an explicit private socket or clear them."""
    guard = create_isolated_tmux_test_root(monkeypatch)
    try:
        yield guard
    finally:
        if guard is not None:
            guard.close()
