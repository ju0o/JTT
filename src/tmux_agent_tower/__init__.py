"""JTT runtime source package and installed import namespace."""

LEGACY_TOWER_VERSION = "0.3.0rc3"
JTT_VERSION = "0.3.1rc0"
__version__ = JTT_VERSION if __name__ == "ju0_terminal_tower" else LEGACY_TOWER_VERSION
DISTRIBUTION_NAME = "ju0-terminal-tower"


if __name__ == "ju0_terminal_tower":
    # The wheel installs this code under a product-specific package directory.
    # Internal source imports still use the historical name, but only after a
    # fail-closed check prevents loading beside the Legacy Tower distribution.
    import importlib.metadata as _metadata
    import importlib.util as _import_util
    import sys as _sys
    from pathlib import Path as _Path

    try:
        _metadata.distribution("tmux-agent-tower")
    except _metadata.PackageNotFoundError:
        pass
    else:
        raise ImportError(
            "ju0-terminal-tower and the legacy tmux-agent-tower distribution "
            "must use separate Python environments; install JTT with pipx or uv"
        )

    _existing = _sys.modules.get("tmux_agent_tower")
    if _existing is not None:
        _existing_path = getattr(_existing, "__file__", None)
        if not _existing_path or _Path(_existing_path).resolve() != _Path(__file__).resolve():
            raise ImportError(
                "the tmux_agent_tower import name is already owned by another "
                "runtime; use a separate Python environment for JTT"
            )
    else:
        _legacy_spec = _import_util.find_spec("tmux_agent_tower")
        if _legacy_spec is not None:
            _own_file = _Path(__file__).resolve()
            _own_package_dir = _own_file.parent

            def _matches_path(candidate, expected):
                try:
                    return _Path(candidate).resolve() == expected
                except (OSError, RuntimeError, TypeError):
                    return False

            _foreign_origin = (
                _legacy_spec.origin is not None
                and not _matches_path(_legacy_spec.origin, _own_file)
            )
            _search_locations = _legacy_spec.submodule_search_locations
            _unresolved_spec = (
                _legacy_spec.origin is None and _search_locations is None
            )
            _foreign_search_path = _search_locations is not None and any(
                not _matches_path(location, _own_package_dir)
                for location in _search_locations
            )
            if _unresolved_spec or _foreign_origin or _foreign_search_path:
                raise ImportError(
                    "another tmux_agent_tower source is visible on this Python path; "
                    "use a separate Python environment for JTT"
                )
        _sys.modules["tmux_agent_tower"] = _sys.modules[__name__]
