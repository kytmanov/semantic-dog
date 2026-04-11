import re

import pytest

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHFABCDfJRSThl]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from *text*.

    Rich can render option names with per-character ANSI styling
    (e.g. ``\\x1b[1m-\\x1b[0m\\x1b[1m-\\x1b[0m\\x1b[1md\\x1b[0m...``) which
    breaks plain substring checks.  Strip codes before asserting.
    """
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def no_color(monkeypatch):
    """Disable Rich/Typer ANSI color output globally.

    CliRunner(env={"NO_COLOR": "1"}) does NOT work — Click stores that dict
    in its own context and never writes to os.environ. Rich reads os.environ
    directly when creating a Console, so the only reliable fix is monkeypatching
    the real environment before any CLI invocation.
    """
    monkeypatch.setenv("NO_COLOR", "1")
