"""Error boundary shared by Work-domain modules."""

import sys


def fail(message, exit_code=2):
    """Print a user-facing CLI error and terminate with ``exit_code``.

    This is deliberately the same small boundary that the monolithic CLI used:
    callers retain the existing message prefix and ``SystemExit`` semantics,
    while lower-level Work modules no longer import the CLI entry point.
    """

    print(f"lifeos: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


__all__ = ["fail"]
