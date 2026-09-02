#!/usr/bin/env python3
"""LifeOS CLI public entry point."""

from lifeos_work.cli import main


VERSION = "1.1.1"


def cli() -> None:
    """Run the installed ``lifeos`` console command."""
    main(VERSION)


if __name__ == "__main__":
    cli()
