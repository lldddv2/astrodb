import sys
import argparse
from importlib.metadata import version, PackageNotFoundError

from astrodb.ui.app import main as run_tui


def _get_version() -> str:
    try:
        return version("astrodb")
    except PackageNotFoundError:
        return "0.0.0+local"


def cli():
    """Entry point for the command line script."""
    parser = argparse.ArgumentParser(
        prog="astrodb",
        description="TUI for querying, cleaning, and persisting astronomical database records.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"astrodb {_get_version()}",
    )
    parser.parse_args()
    run_tui()


if __name__ == "__main__":
    sys.exit(cli())
