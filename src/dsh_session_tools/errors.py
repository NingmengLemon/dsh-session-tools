"""The one exception type the CLI turns into a clean message and a non-zero exit."""

from __future__ import annotations


class ToolError(Exception):
    """An expected, user-facing failure.

    Anything raised as a ToolError is a condition we detected and refused on:
    a bad path, an ambiguous session, an artifact that fails validation. The CLI
    prints the message and exits non-zero. Unexpected exceptions are left alone
    so they surface with a traceback rather than being mistaken for a diagnosis.
    """
