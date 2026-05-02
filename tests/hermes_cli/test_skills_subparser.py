"""Test that skills subparser doesn't conflict (regression test for #898)."""

import argparse


def test_no_duplicate_skills_subparser():
    """Ensure 'skills' subparser is only registered once to avoid Python 3.11+ crash.

    Python 3.11 changed argparse to raise an exception on duplicate subparser
    names instead of silently overwriting (see CPython #94331).

    This test will fail with:
        argparse.ArgumentError: argument command: conflicting subparser: skills

    if the duplicate 'skills' registration is reintroduced.
    """
    # Force fresh import of the module where parser is constructed
    # If there are duplicate 'skills' subparsers, this import will raise
    # argparse.ArgumentError at module load time
    import importlib
    import sys
    import hermes_cli as _hermes_cli_pkg

    # Remove cached module if present, and restore on exit to avoid leaving
    # hermes_cli.main (the package attribute) pointing to a freshly-created
    # module object.  Leaving the attribute stale causes subsequent tests that
    # patch "hermes_cli.main.X" via unittest.mock to hit the new object while
    # functions imported at collection time still reference the old one.
    _original_main = sys.modules.get('hermes_cli.main')
    _original_main_attr = getattr(_hermes_cli_pkg, 'main', None)
    if 'hermes_cli.main' in sys.modules:
        del sys.modules['hermes_cli.main']

    try:
        import hermes_cli.main  # noqa: F401
    except argparse.ArgumentError as e:
        if "conflicting subparser" in str(e):
            raise AssertionError(
                f"Duplicate subparser detected: {e}. "
                "See issue #898 for details."
            ) from e
        raise
    finally:
        # Restore sys.modules and package attribute so subsequent tests that
        # patch hermes_cli.main.* see the same module object that was active
        # when those tests' module-level imports were resolved.
        if _original_main is not None:
            sys.modules['hermes_cli.main'] = _original_main
        elif 'hermes_cli.main' in sys.modules:
            del sys.modules['hermes_cli.main']
        if _original_main_attr is not None:
            _hermes_cli_pkg.main = _original_main_attr
        elif hasattr(_hermes_cli_pkg, 'main'):
            del _hermes_cli_pkg.main
