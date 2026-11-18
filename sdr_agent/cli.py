"""Command-line entry point.

    python -m sdr_agent run --domain northwind-freight.example --first-name Dana --title "VP Sales" --demo
    python -m sdr_agent batch data/prospects_demo.csv --demo
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import pipeline
from .backends import Backend, ClaudeBackend, DemoBackend
from .integrations import hubspot, slack
from .models import Prospect, SDRResult
from .render import to_markdown

ROOT = Path(__file__).resolve().parent.parent
console = Console()


def _backend(args) -> Backend:
    if args.demo:
        return DemoBackend()
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        console.print("[yellow]No ANTHROPIC_API_KEY set: running in --demo mode.[/yellow]")
        return DemoBackend()
    return ClaudeBackend(model=args.model)


def _save(result: SDRResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.prospect.company_domain}__{result.prospect.first_name.lower()}"
    (out_dir / f"{stem}.json").write_text(result.model_dump_json(indent=2))
    md = out_dir / f"{stem}.md"
    md.write_text(to_markdown(result))
    return md


def _deliver(result: SDRResult, args) -> None:
    if args.slack:
        url = os.getenv("SLACK_WEBHOOK_URL")
        if not url:
            console.print("[red]--slack needs SLACK_WEBHOOK_URL[/red]")
        else:
            slack.post(result, url)
            console.print("  posted to Slack")
    if args.hubspot:
        token = os.getenv("HUBSPOT_TOKEN")
        if not token:
            console.print("[red]--hubspot needs HUBSPOT_TOKEN[/red]")
        else:
            contact_id, note_id = hubspot.HubSpot(token).sync(result)
            console.print(f"  HubSpot contact {contact_id}, note {note_id}")


def _process(prospect: Prospect, backend: Backend, seller: str, args) -> SDRResult:
    result = pipeline.run(
        prospect, backend, seller,
        max_revisions=args.max_revisions, pass_threshold=args.threshold,
        log=lambda m: console.print(f"[dim]{m}[/dim]"),
    )
    path = _save(result, Path(args.out))
    console.print(f"[green]Saved[/green] {path}")
    _deliver(result, args)
    return result


def _summary(results: list[SDRResult]) -> None:
    table = Table(title="AI SDR run")
    for col in ("Prospect", "Company", "ICP", "Attempts", "Grade", "Status"):
        table.add_column(col)
    for r in results:
        table.add_row(
            f"{r.prospect.first_name} {r.prospect.last_name}".strip(),
            r.brief.company_name, str(r.brief.icp_fit_score), str(len(r.attempts)),
            f"{r.final.grade.overall:.1f}", "approved" if r.approved else "needs edit",
        )
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="sdr_agent", description="AI SDR research + email agent")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--demo", action="store_true", help="Run offline on recorded fixtures")
    common.add_argument("--seller", default=str(ROOT / "config" / "seller.md"), help="Seller context file")
    common.add_argument("--out", default=str(ROOT / "outputs"))
    common.add_argument("--model", default=None, help="Override SDR_MODEL")
    common.add_argument("--max-revisions", type=int, default=2)
    common.add_argument("--threshold", type=float, default=4.0, help="Grader pass mark (1-5)")
    common.add_argument("--slack", action="store_true", help="Post result to SLACK_WEBHOOK_URL")
    common.add_argument("--hubspot", action="store_true", help="Sync contact + note to HubSpot")

    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("run", parents=[common], help="One prospect")
    one.add_argument("--domain", required=True)
    one.add_argument("--first-name", required=True)
    one.add_argument("--last-name", default="")
    one.add_argument("--title", required=True)
    one.add_argument("--email")
    one.add_argument("--linkedin")

    many = sub.add_parser("batch", parents=[common], help="Prospects from a CSV")
    many.add_argument("csv_path")

    args = parser.parse_args(argv)
    seller = Path(args.seller).read_text()
    backend = _backend(args)

    failures = 0
    if args.cmd == "run":
        prospects = [Prospect(
            company_domain=args.domain, first_name=args.first_name, last_name=args.last_name,
            title=args.title, email=args.email, linkedin_url=args.linkedin,
        )]
    else:
        prospects = []
        # utf-8-sig strips the BOM that Excel/Sheets exports often add
        with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
            for line_no, row in enumerate(csv.DictReader(f), start=2):
                # k is None for overflow cells in rows with too many commas; drop them
                row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
                for optional in ("email", "linkedin_url"):
                    row[optional] = row.get(optional) or None
                try:
                    prospects.append(Prospect(**row))
                except ValidationError as exc:
                    failures += 1
                    missing = ", ".join(str(e["loc"][0]) for e in exc.errors())
                    console.print(f"[red]Skipping CSV line {line_no}: bad or missing {missing}[/red]")

    results = []
    for p in prospects:
        console.rule(f"{p.first_name} @ {p.company_domain}")
        try:
            results.append(_process(p, backend, seller, args))
        except Exception as exc:  # keep a batch going if one row fails
            failures += 1
            console.print(f"[red]Failed {p.company_domain}: {exc}[/red]")

    if results:
        _summary(results)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
