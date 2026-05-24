"""CLI for export commands. Wired as ``roguerglike-export`` in pyproject."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .fit import build_fit


@click.group()
def main() -> None:
    """Export a sidecar JSONL recording to a fitness-platform format."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@main.command("fit")
@click.argument(
    "jsonl_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.argument(
    "fit_path",
    type=click.Path(dir_okay=False, path_type=Path),
)
def export_fit(jsonl_path: Path, fit_path: Path) -> None:
    """Convert a JSONL recording into a FIT activity file.

    The output is marked as sport=CYCLING, sub_sport=INDOOR_CYCLING so
    fitness platforms classify it as a trainer ride rather than an
    outdoor route. Suitable for direct upload to Strava and manual
    upload to TrainingPeaks.

    JSONL_PATH is a recording produced by ``roguerglike-sidecar --record <path>``.
    FIT_PATH is the output file to write (overwrites if it exists).
    """
    try:
        build_fit(jsonl_path, fit_path)
    except ValueError as e:
        click.echo(f"export failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"wrote {fit_path}")


if __name__ == "__main__":
    main()
