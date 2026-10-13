"""The one exception type the CLI turns into a clean message and a non-zero exit."""

from __future__ import annotations


def quote(value: object) -> str:
    """Wrap ``value`` in single quotes for a message, without escaping it.

    ``repr`` is the obvious choice and the wrong one here. Every path this tool
    talks about is a Windows path on this platform, and ``repr`` doubles each
    backslash, so a refusal that names a path appears to name one containing two
    of them.
    """
    return f"'{value}'"


class ToolError(Exception):
    """An expected, user-facing failure.

    Anything raised as a ToolError is a condition we detected and refused on:
    a bad path, an ambiguous session, an artifact that fails validation. The CLI
    prints the message and exits non-zero. Unexpected exceptions are left alone
    so they surface with a traceback rather than being mistaken for a diagnosis.
    """
