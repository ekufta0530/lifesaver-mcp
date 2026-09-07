"""CLI entrypoint for the warehouse pipeline.

    python -m warehouse.job status
    python -m warehouse.job backfill                 # full history, resumable
    python -m warehouse.job sync                      # routine trailing-window pull
    python -m warehouse.job resolve                   # re-run identity resolution
    python -m warehouse.job rebuild                   # rebuild visits + lifecycle
    python -m warehouse.job snapshot --all            # (re)compute every KPI month
    python -m warehouse.job kpis --metric first_to_second_rate
    python -m warehouse.job review                    # aliases flagged for a human

The steps that hit lsscloud.com (``backfill``, ``sync``) hold the single shared
LifeSaver session; do not run them while the MCP server might also be pulling.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from datetime import date

from lifesaver.client import LifesaverClient, LifesaverError
from lifesaver.config import get_settings as get_lifesaver_settings

from . import pipeline
from .config import get_warehouse_settings
from .kpis import METRICS
from .months import month_start


def _parse_month(value: str) -> date:
    try:
        y, m = value.split("-")
        return date(int(y), int(m), 1)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}") from None


def _client() -> LifesaverClient:
    return LifesaverClient(get_lifesaver_settings())


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_status(args, wh, settings) -> int:
    _print(pipeline.status(wh))
    return 0


def cmd_backfill(args, wh, settings) -> int:
    client = _client()
    try:
        results = pipeline.backfill(
            wh, client, settings,
            from_month=args.from_month,
            to_month=args.to_month,
            resume=not args.restart,
            on_month=lambda m, r: print(
                f"  {m:%Y-%m}: {r.counts.inserted:+d} new, {r.counts.changed:+d} changed",
                file=sys.stderr,
            ),
        )
    finally:
        client.logout()
    print(f"backfilled {len(results)} month(s)")
    _print(pipeline.status(wh))
    return 0


def cmd_sync(args, wh, settings) -> int:
    client = _client()
    try:
        result, written = pipeline.refresh(wh, client, settings)
    finally:
        client.logout()
    print(
        f"sync: {result.counts.inserted:+d} new, {result.counts.changed:+d} changed, "
        f"{result.counts.unchanged} unchanged; {written} KPI rows updated"
    )
    return 0


def cmd_resolve(args, wh, settings) -> int:
    result, linked = pipeline.resolve_identities(wh, settings)
    print(
        f"resolve: +{len(result.new_customers)} customers, "
        f"+{len(result.new_aliases)} aliases, {linked} line_items linked"
    )
    return 0


def cmd_rebuild(args, wh, settings) -> int:
    n_visits, n_customers = pipeline.rebuild_visits(wh, settings)
    print(f"rebuild: {n_visits} visits across {n_customers} customers")
    return 0


def cmd_snapshot(args, wh, settings) -> int:
    today = date.today()
    if args.all:
        written = pipeline.snapshot_history(wh, settings, today=today)
        print(f"snapshot: {written} KPI rows written/updated")
    else:
        month = args.month or month_start(today)
        snaps = pipeline.snapshot_month(wh, settings, month, today=today)
        _print([dataclasses.asdict(s) for s in snaps])
    return 0


def cmd_kpis(args, wh, settings) -> int:
    _print(
        wh.read_kpi_series(
            metric=args.metric, from_month=args.from_month, to_month=args.to_month
        )
    )
    return 0


def cmd_review(args, wh, settings) -> int:
    _print(wh.customers_needing_review())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="warehouse.job", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    bf = sub.add_parser("backfill", help="pull full history, month by month, resumable")
    bf.add_argument("--from", dest="from_month", type=_parse_month, metavar="YYYY-MM")
    bf.add_argument("--to", dest="to_month", type=_parse_month, metavar="YYYY-MM")
    bf.add_argument("--restart", action="store_true", help="ignore the resume checkpoint")
    bf.set_defaults(func=cmd_backfill)

    sub.add_parser("sync", help="routine trailing-window pull + refresh").set_defaults(
        func=cmd_sync
    )
    sub.add_parser("resolve").set_defaults(func=cmd_resolve)
    sub.add_parser("rebuild").set_defaults(func=cmd_rebuild)

    sn = sub.add_parser("snapshot", help="(re)compute KPI snapshots")
    sn.add_argument("--month", type=_parse_month, metavar="YYYY-MM")
    sn.add_argument("--all", action="store_true", help="every month with data")
    sn.set_defaults(func=cmd_snapshot)

    kp = sub.add_parser("kpis", help="print stored KPI series")
    kp.add_argument("--metric", choices=METRICS)
    kp.add_argument("--from", dest="from_month", type=_parse_month, metavar="YYYY-MM")
    kp.add_argument("--to", dest="to_month", type=_parse_month, metavar="YYYY-MM")
    kp.set_defaults(func=cmd_kpis)

    sub.add_parser("review", help="list aliases flagged for human review").set_defaults(
        func=cmd_review
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = get_warehouse_settings()
    try:
        with pipeline.open_warehouse(settings) as wh:
            return args.func(args, wh, settings)
    except LifesaverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
