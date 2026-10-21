"""Minimal i18n layer.

Business logic and internal state constants (``STATUS_WORKING``,
``VISIT_NEW``, etc. in ``detection/status.py`` and ``state/visits.py``)
stay in English identifiers always -- only this module's ``t()`` function
turns a key into user-facing text. This keeps status/visit comparisons
(``if status == STATUS_WORKING``) stable regardless of display language.

The default is Korean when nothing is configured yet, but the first run
of the TUI (see ``ui/tower.py``'s language picker) asks explicitly and
persists the answer via ``save_language()`` -- ``has_language_configured()``
is how that first-run check tells "never asked" apart from "asked, and
the answer happened to be ko".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import ko, en
from ..paths import CONFIG_DIR

_CATALOGS = {"ko": ko.STRINGS, "en": en.STRINGS}
_DEFAULT_LANGUAGE = "ko"

CONFIG_FILE = CONFIG_DIR / "config.toml"

_LANGUAGE_LINE_RE = re.compile(r'^\s*language\s*=\s*"?([A-Za-z]{2})"?\s*$')


def _detect_language() -> str:
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            match = _LANGUAGE_LINE_RE.match(line)
            if match:
                candidate = match.group(1).lower()
                if candidate in _CATALOGS:
                    return candidate
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return _DEFAULT_LANGUAGE


_current_language = _detect_language()


def set_language(language: Optional[str]) -> None:
    global _current_language
    if language in _CATALOGS:
        _current_language = language


def current_language() -> str:
    return _current_language


def has_language_configured() -> bool:
    """True once a ``language = "..."`` line has ever been written, even
    if that write happened to pick the same value as the default -- used
    to show the first-run picker exactly once.
    """

    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            if _LANGUAGE_LINE_RE.match(line):
                return True
    except Exception:
        pass
    return False


def save_language(language: str) -> None:
    """Persist ``language`` to config.toml without disturbing any other
    key already in the file (``project_roots``, ``[agents]``, ...) --
    replaces an existing top-level ``language = ...`` line in place, or
    prepends one if there wasn't one, rather than re-serializing the
    whole file from a parsed structure.
    """

    if language not in _CATALOGS:
        return

    new_line = f'language = "{language}"'

    try:
        existing_lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing_lines = []
    except Exception:
        existing_lines = []

    output_lines = []
    replaced = False
    for line in existing_lines:
        if not replaced and _LANGUAGE_LINE_RE.match(line):
            output_lines.append(new_line)
            replaced = True
        else:
            output_lines.append(line)

    if not replaced:
        output_lines.insert(0, new_line)

    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    except Exception:
        pass

    set_language(language)


def t(key: str, **kwargs) -> str:
    catalog = _CATALOGS.get(_current_language, _CATALOGS[_DEFAULT_LANGUAGE])
    text = catalog.get(key)
    if text is None:
        text = _CATALOGS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text
