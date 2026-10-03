"""atlas-pipeline CLI:  ingest | extract | reconcile | analytics | views | load | export-fixtures | all"""
from __future__ import annotations

import logging
from typing import Optional

import typer

from .config import ensure_dirs

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, no_args_is_help=True, help=__doc__)


@app.callback()
def _main(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ensure_dirs()


@app.command()
def ingest(sources: Optional[list[str]] = typer.Argument(None, help="subset of sources (default: all)"),
           skip: list[str] = typer.Option([], "--skip", help="sources to skip")) -> None:
    """Download (cached) + parse sources into data/interim/<source>.*.jsonl"""
    from . import ingest as ing
    ing.run(sources or None, skip)


@app.command()
def extract(limit: int = typer.Option(0, help="max abstracts (0 = all)"),
            concurrency: int = typer.Option(4)) -> None:
    """LLM claim extraction over PubMed abstracts (OpenAI Structured Outputs)."""
    from . import extract as ex
    ex.run(limit=limit or None, concurrency=concurrency)


@app.command()
def reconcile() -> None:
    """Map extracted names to stable ids; emit literature edges from claims."""
    from . import reconcile as rc
    rc.run()


@app.command()
def analytics() -> None:
    """IC, similarity, counterexamples, clustering, network overlap, paths."""
    from .analytics import run
    run()


@app.command()
def views() -> None:
    """Build ActionView / MechanismView payloads."""
    from . import views as v
    v.run()


@app.command()
def load(dry_run: bool = typer.Option(False, help="validate only, do not write"),
         database_url: Optional[str] = typer.Option(None, envvar="DATABASE_URL")) -> None:
    """Validate + load everything into Postgres in one transaction."""
    from . import load as ld
    ld.run(database_url, dry_run=dry_run)


@app.command("export-fixtures")
def export_fixtures(api_base: Optional[str] = typer.Option(None, envvar="ATLAS_API_BASE"),
                    database_url: Optional[str] = typer.Option(None, envvar="DATABASE_URL")) -> None:
    """Write real API responses for demo ids into data/snapshot/ (fixtures layout)."""
    from . import export_fixtures as exf
    exf.run(api_base=api_base, database_url=database_url)


@app.command("all")
def all_(skip: list[str] = typer.Option([], "--skip")) -> None:
    """ingest -> extract -> reconcile -> analytics -> views -> load"""
    from . import ingest as ing, extract as ex, reconcile as rc, views as v, load as ld
    from .analytics import run as an
    ing.run(None, skip)
    ex.run()
    rc.run()
    an()
    v.run()
    ld.run(None)


if __name__ == "__main__":
    app()
