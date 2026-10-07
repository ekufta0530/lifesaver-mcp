"""Render the Frame Shop Performance dashboard to a self-contained HTML file.

    python -m dashboard.build                 # -> dashboard/index.html
    python -m dashboard.build --db warehouse.db --out dashboard/index.html --json dashboard/data.json
    python -m dashboard.build --store mason --db mason.db --out dashboard/mason.html

Reads the warehouse (read-only) and embeds the full KPI history into the page.
The page renders client-side: it opens on the *current* month/quarter (the "live"
view that a daily rebuild keeps moving) and a dropdown reaches every closed 2026
monthly and quarterly report. Re-run after each `cloud.warehouse.job sync` and
re-deploy the output.

One page per store (``STORES``); a tab bar links the pages. Every store's
warehouse has the same tables, so only the copy, outliers and chart ranges
differ between them.
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from core.models import CALC_VERSION
from sqlite_extract.importer import DATA_THROUGH, SOURCE_WORK_ORDERS

# --- one-off outliers ------------------------------------------------------
# The Polaris Mission: two 2024 commissions for a SpaceX program (the customer
# is entered as "Casey Phillips" / "The Polaris Mission"), ~$39.5k net combined
# -- an order of magnitude above any normal ticket. Left in, they make every
# prior-year and trailing-12 revenue / average-ticket comparison that reaches
# back to late 2024 read as a steep decline, while the order *count* barely
# moves. The dashboard's "exclude outlier" toggle drops exactly these visits
# from the business section; the retention KPIs are left untouched.
OUTLIER_VISIT_IDS = ("c781adfa3958:2024-10-05", "c781adfa3958:2024-11-06")
OUTLIER_LABEL = "Polaris Mission commission"

# --- KPI metadata -----------------------------------------------------------
# Shared with the client renderer verbatim (emitted as DASH.meta).
#   good      -- the healthy direction
#   ceiling   -- fixed top of the y-axis, so the baseline line has headroom
#   plot_from -- earliest month worth charting (before this the sample is noise)
#   short     -- the one-liner under the chart title
#   long      -- the "what does this actually mean" tooltip
META = [
    {
        "key": "first_to_second_rate",
        "label": "First → second purchase",
        "tile": "First → second",
        "unit": "pct", "good": "up", "baseline": 0.192, "ceiling": 0.30,
        "plot_from": "2025-09",
        "short": "Share of a first-visit cohort that comes back within 12 months.",
        "long": (
            "Of every customer whose first-ever visit fell in a given month, what "
            "fraction returned for a second visit within 365 days? Reported as the "
            "blend of the twelve monthly cohorts whose 12-month window closed a "
            "year before the report date — one month alone is too small to trust. "
            "A customer who bought once and never came back counts against this "
            "forever. It is the highest-leverage retention number: ~82% of "
            "first-time customers never return at all."
        ),
    },
    {
        "key": "repeat_revenue_share",
        "label": "Repeat share of revenue",
        "tile": "Repeat revenue",
        "unit": "pct", "good": "up", "baseline": 0.273, "ceiling": 0.42,
        "plot_from": "2024-08",
        "short": "Revenue from return visits, trailing 12 months.",
        "long": (
            "Of all revenue in the trailing 12 months, how much came from visits "
            "that were not the customer's first? Revenue is retail minus discount; "
            "a visit is one customer on one day (work orders dropped off together "
            "count once). This climbs when existing customers come back and "
            "spend — the clearest single sign retention is working."
        ),
    },
    {
        "key": "median_days_to_second",
        "label": "Median days to second",
        "tile": "Days to second",
        "unit": "days", "good": "down", "baseline": 97, "ceiling": 150,
        "plot_from": "2025-09",
        "short": "Typical gap between a customer's first and second visit.",
        "long": (
            "Among customers in the matured cohort who did return within a year, "
            "the median number of days between visit one and visit two — the "
            "median, so a few outliers don't distort it. Lower means people come "
            "back sooner, a sign that reminders and seasonal nudges are landing. "
            "It can drift up simply because slower returners keep being counted, "
            "so read it next to the first→second rate."
        ),
    },
    {
        "key": "active_customers_ttm",
        "label": "Active customers",
        "tile": "Active customers",
        "unit": "int", "good": "up", "baseline": 365, "ceiling": 400,
        "plot_from": "2024-08",
        "short": "Distinct customers with a visit in the last 12 months.",
        "long": (
            "How many different customers made at least one visit in the trailing "
            "12 months — the real size of the engaged base. Different from "
            "total-customers-ever: someone who hasn't visited in over a year has "
            "effectively lapsed and drops out of this count. It grew as the store "
            "built its base and has lately begun to soften."
        ),
    },
    {
        "key": "reactivation_rate",
        "label": "Reactivation rate",
        "tile": "Reactivation",
        "unit": "pct", "good": "up", "baseline": None, "ceiling": 0.09,
        "plot_from": "2025-09",
        "short": "Win-back rate among long-lapsed customers.",
        "long": (
            "At the month's start, take every customer who had bought before but "
            "not in the previous 365 days — the lapsed pool, roughly 300 people. "
            "What share of them bought something during the month? With no win-back "
            "campaign running this sits near zero; it is the pre-campaign "
            "baseline. When a push goes out to lapsed customers, this is the "
            "number that should move."
        ),
    },
]

# --- stores -------------------------------------------------------------
# One dashboard page per store. Keys:
#   tab / page    -- tab-bar label and the published file it links to
#   as_of         -- "today": the wall clock is "now" (daily-synced warehouse);
#                    "data": the latest order date is "now" (periodic snapshot)
#   chart_from    -- first month on the revenue + new-customer bar charts
#   meta          -- per-KPI overrides of META; a store without hand-tuned
#                    ceilings gets them derived from its own data
#   outliers      -- visit_ids the "exclude outlier" toggle can remove
#   companion     -- the card beside the reactivation chart; {pool} becomes the
#                    report month's lapsed-pool size
#   method        -- the "How these are built" bullets (HTML)

_M_VISIT = ("<b>A purchase is a visit</b> — one customer on one day. Several work orders "
            "dropped off together count once.")
_M_LIVE_YOY = ("<b>Year-over-year on the live month</b> compares only the same run of days a "
               "year earlier, so a part-month isn't measured against a whole one. \"On pace\" is "
               "the month-to-date rate carried to month end.")
_M_MONTHLY = ("<b>Monthly performance vs. last year</b> lines each month up against the same "
              "month a year earlier — revenue, order count, and average ticket. Completed months "
              "use the full month; the current month is month-to-date against the same run of "
              "days last year. Toggle between the calendar year so far and a rolling 12 months.")
_M_COHORT = ("<b>Cohort metrics</b> (first&nbsp;&rarr;&nbsp;second, median days) use the 12 "
             "monthly cohorts that matured a year before the report month, so the sample is stable.")

STORES: dict[str, dict] = {
    "main": {
        "tab": "Main store",
        "page": "index.html",
        "kicker": "Lifesaver &middot; Work Order History",
        "as_of": "today",
        "chart_from": "2024-08",
        "meta": {},
        "outliers": OUTLIER_VISIT_IDS,
        "outlier_label": OUTLIER_LABEL,
        "reactivation_delta": "pre-campaign baseline",
        "cohort_blurb": "The store opened on Lifesaver in May 2024.",
        "companion": (
            "<h2>The win-back dial</h2><p>No reactivation campaign has run, so this sits near "
            "zero — the pre-campaign baseline. When a win-back push goes out to 12-month-lapsed "
            "customers (email, postcard, a call), this is the number that moves if it worked. "
            "The lapsed pool is the addressable audience.</p>"
        ),
        "method": [
            _M_VISIT,
            "<b>Revenue is retail minus discount</b>, booked to the order date. Voided orders are "
            "excluded from every figure; a same-day re-do keeps only the corrected order.",
            _M_LIVE_YOY,
            _M_MONTHLY,
            "<b>The Polaris Mission toggle.</b> In Oct–Nov 2024 the store took two commissions for "
            "a SpaceX program (~$39.5k net combined, roughly ten normal tickets). Left in, they "
            "make any comparison reaching back to late 2024 look like a sharp drop even though "
            "the order <i>count</i> is flat. The toggle removes just those two orders from the "
            "last-year revenue and average-ticket figures — it only touches the Oct and Nov rows, "
            "so it shows its effect in the trailing-12-months view (checking it there switches "
            "you to that view). Retention figures are unaffected.",
            _M_COHORT,
            "<b>Live vs closed.</b> The current month and quarter update with each daily data "
            "pull. Once a period closes its figures are frozen — pick it from the dropdown to "
            "see the report as it stood.",
            "<b>Complete history.</b> Invoice&nbsp;#1 is a May&nbsp;2024 test order — there is no "
            "earlier data to be missing, so cohorts are unbiased.",
        ],
    },
    "mason": {
        "tab": "Mason",
        "page": "mason.html",
        "kicker": "Mason &middot; LifeSaver POS history since 2015",
        "as_of": "data",
        "chart_from": "2015-06",
        # LifeSaver starts June 2015: anyone who already shopped before then looks
        # "new" in 2015-16, so cohort metrics wait for cohorts formed a year in,
        # and trailing-12 metrics for the first full year.
        "meta": {
            "first_to_second_rate": {
                "baseline": None, "plot_from": "2017-06",
                "long": (
                    "Of every customer whose first-ever visit fell in a given month, what "
                    "fraction returned for a second visit within 365 days? Reported as the "
                    "blend of the twelve monthly cohorts whose 12-month window closed a year "
                    "before the report date — one month alone is too small to trust. A "
                    "customer who bought once and never came back counts against this forever."
                ),
            },
            "repeat_revenue_share": {"baseline": None, "plot_from": "2016-05"},
            "median_days_to_second": {"baseline": None, "plot_from": "2017-06"},
            "active_customers_ttm": {
                "baseline": None, "plot_from": "2016-05",
                "long": (
                    "How many different customers made at least one visit in the trailing 12 "
                    "months — the real size of the engaged base. Different from "
                    "total-customers-ever: someone who hasn't visited in over a year has "
                    "effectively lapsed and drops out of this count."
                ),
            },
            "reactivation_rate": {
                "baseline": None, "plot_from": "2017-06",
                "long": (
                    "At the month's start, take every customer who had bought before but not in "
                    "the previous 365 days — the lapsed pool. What share of them bought something "
                    "during the month? With over a decade of history the pool is large, so even "
                    "a fraction of a percent is real people coming back."
                ),
            },
        },
        "outliers": (),
        "outlier_label": "",
        "reactivation_delta": None,
        "cohort_blurb": "LifeSaver history starts June 2015, so the first year includes "
                        "existing customers seen for the first time.",
        "companion": (
            "<h2>The lapsed pool</h2><p>{pool} past customers hadn't bought in over a year "
            "going into this month — the audience for any win-back push (email, postcard, a "
            "call). Reactivation is the share of them who came back; it is the number that "
            "should move when a campaign goes out.</p>"
        ),
        "method": [
            _M_VISIT,
            "<b>Revenue is the ticket subtotal</b> — pre-tax and after discounts, the same "
            "\"Sales\" figure the store's LifeSaver reports — booked to the order date. Voided "
            "orders are excluded from every figure.",
            "<b>Customers are LifeSaver customer numbers</b>, so the same person is matched "
            "across years without relying on how their name was typed.",
            _M_LIVE_YOY,
            _M_MONTHLY,
            _M_COHORT,
            "<b>A periodic snapshot.</b> This store's figures come from a copy of its LifeSaver "
            "database, not a daily feed; the newest order date in that copy is treated as "
            "\"today\", so the month it falls in is the open (month-to-date) month.",
            "<b>History starts June 2015.</b> Customers who shopped before LifeSaver was set up "
            "look new in 2015–16, so cohort charts start mid-2017 and trailing-12 charts mid-2016, "
            "once that effect has washed out.",
        ],
    },
}

FONT_LINK = (
    "https://fonts.googleapis.com/css2?"
    "family=Spectral:wght@400;500;600&"
    "family=Archivo:wght@400;500;600&display=swap"
)


# --- data ----------------------------------------------------------------

def _nice_ceiling(top: float, unit: str) -> float:
    """A round y-axis top with ~10% headroom over the highest plotted value."""
    if unit == "pct":
        step = 0.01 if top < 0.1 else 0.05
    else:
        step = 25 if unit == "days" else 10 ** max(1, len(str(int(top))) - 1)
    return round(math.ceil(top * 1.1 / step) * step, 4) or step


def store_meta(store: str, series: dict[str, dict[str, dict]]) -> list[dict]:
    """META with the store's overrides; ceilings it doesn't pin are fitted to its data."""
    overrides = STORES[store]["meta"]
    out = []
    for m in META:
        o = overrides.get(m["key"])
        if o is None:
            out.append(m)
            continue
        m = {**m, **o}
        if "ceiling" not in o:
            vals = [r["v"] for mo, r in series[m["key"]].items()
                    if mo >= m["plot_from"] and r["v"] is not None]
            if vals:
                m["ceiling"] = _nice_ceiling(max(vals), m["unit"])
        out.append(m)
    return out


def load(db_path: str, store: str = "main", *, today: date | None = None) -> dict:
    cfg = STORES[store]
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row

    def checkpoint(name: str) -> str | None:
        r = c.execute("SELECT value FROM sync_checkpoints WHERE name = ?", (name,)).fetchone()
        return None if r is None else r["value"]

    if today is None:
        data_through = checkpoint(DATA_THROUGH)
        today = (date.fromisoformat(data_through) if cfg["as_of"] == "data" and data_through
                 else date.today())

    series: dict[str, dict[str, dict]] = {}
    for m in META:
        series[m["key"]] = {
            r["month"][:7]: {
                "v": r["value"], "n": r["numerator"], "d": r["denominator"],
                "final": bool(r["is_final"]),
            }
            for r in c.execute(
                "SELECT month, value, numerator, denominator, is_final "
                "FROM kpi_monthly WHERE metric = ? AND calc_version = ? ORDER BY month",
                (m["key"], CALC_VERSION),
            )
        }

    cohorts = [
        {"m": r["m"], "n": r["n"]}
        for r in c.execute(
            "SELECT substr(first_visit_date, 1, 7) m, COUNT(*) n "
            "FROM customer_lifecycle GROUP BY m ORDER BY m"
        )
    ]
    s = dict(c.execute(
        "SELECT (SELECT COUNT(*) FROM customer_lifecycle) customers, "
        "(SELECT COUNT(*) FROM customer_lifecycle WHERE lifetime_visits >= 2) repeat_customers, "
        "(SELECT COUNT(*) FROM visits) visits, "
        "(SELECT ROUND(SUM(revenue)) FROM visits) revenue, "
        "(SELECT MIN(visit_date) FROM visits) first_day, "
        "(SELECT MAX(visit_date) FROM visits) last_day"
    ).fetchone())
    # line items + pulls exist only in the cloud ingest's warehouse
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    has_pulls = "line_items" in tables
    s["line_items"] = c.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] if has_pulls else 0
    s["last_pull"] = (c.execute("SELECT MAX(pulled_at) FROM raw_pulls").fetchone()[0]
                      if has_pulls else None)

    # monthly revenue + order (visit) counts -- the "how's business" layer,
    # same revenue basis as the KPIs (visits = retail - discount, voids excluded)
    monthly = {
        r["m"]: {"rev": r["rev"], "v": r["v"]}
        for r in c.execute(
            "SELECT substr(visit_date, 1, 7) m, ROUND(SUM(revenue), 2) rev, COUNT(*) v "
            "FROM visits GROUP BY m ORDER BY m"
        )
    }

    s["work_orders"] = int(checkpoint(SOURCE_WORK_ORDERS) or 0)

    all_months = sorted({mo for k in series.values() for mo in k})
    today_month = today.strftime("%Y-%m")
    current_month = min(today_month, all_months[-1]) if all_months else today_month

    # partial-month YoY: compare the live month only against the *same span of
    # days* a year earlier, so "September so far" isn't measured against a whole
    # September.
    cy, cm = int(current_month[:4]), int(current_month[5:7])
    cutoff_day = today.day if current_month == today_month else calendar.monthrange(cy, cm)[1]
    ly_last = min(cutoff_day, calendar.monthrange(cy - 1, cm)[1])
    ly = c.execute(
        "SELECT COALESCE(ROUND(SUM(revenue), 2), 0) rev, COUNT(*) v FROM visits "
        "WHERE visit_date BETWEEN ? AND ?",
        (date(cy - 1, cm, 1).isoformat(), date(cy - 1, cm, ly_last).isoformat()),
    ).fetchone()

    # outlier isolation -- the "exclude the Polaris Mission" toggle subtracts
    # these client-side, so ship the per-month contribution + the same partial
    # prior-year window with the outlier removed.
    outliers = cfg["outliers"]
    qs = ",".join("?" * len(outliers))
    outlier_monthly = {
        r["m"]: {"rev": r["rev"] or 0.0, "v": r["v"]}
        for r in c.execute(
            f"SELECT substr(visit_date, 1, 7) m, ROUND(SUM(revenue), 2) rev, COUNT(*) v "
            f"FROM visits WHERE visit_id IN ({qs}) GROUP BY m ORDER BY m",
            outliers,
        )
    }
    otot = c.execute(
        f"SELECT COALESCE(ROUND(SUM(revenue), 2), 0) rev, COUNT(*) v "
        f"FROM visits WHERE visit_id IN ({qs})",
        outliers,
    ).fetchone()
    ly_ex = c.execute(
        f"SELECT COALESCE(ROUND(SUM(revenue), 2), 0) rev, COUNT(*) v FROM visits "
        f"WHERE visit_date BETWEEN ? AND ? AND visit_id NOT IN ({qs})",
        (date(cy - 1, cm, 1).isoformat(), date(cy - 1, cm, ly_last).isoformat(), *outliers),
    ).fetchone()

    c.close()

    business = {
        "monthly": monthly,
        "all_time_revenue": s["revenue"],
        "first_day": s["first_day"],
        "partial_month": current_month,
        "partial_day": cutoff_day,
        "days_in_partial": calendar.monthrange(cy, cm)[1],
        "partial_ly_rev": ly["rev"],
        "partial_ly_v": ly["v"],
        "partial_ly_rev_ex": ly_ex["rev"],
        "partial_ly_v_ex": ly_ex["v"],
        "outlier": {
            "label": cfg["outlier_label"],
            "monthly": outlier_monthly,
            "months": list(outlier_monthly),
            "total_rev": otot["rev"],
            "total_v": otot["v"],
        },
    }

    return {
        "store": {
            "key": store,
            "snapshot": cfg["as_of"] == "data",
            "data_through": today.isoformat(),
            "chart_from": cfg["chart_from"],
            "reactivation_delta": cfg["reactivation_delta"],
            "cohort_blurb": cfg["cohort_blurb"],
            "companion": cfg["companion"],
        },
        "tabs": [{"key": k, "label": v["tab"], "page": v["page"]} for k, v in STORES.items()],
        "meta": store_meta(store, series),
        "series": series,
        "cohorts": cohorts,
        "summary": s,
        "business": business,
        "current_month": current_month,
        "calc_version": CALC_VERSION,
        "generated": datetime.now(timezone.utc).strftime("%-d %b %Y, %H:%M UTC"),
        "last_sync": s["last_pull"],
    }


# --- page --------------------------------------------------------------

def render(data: dict) -> str:
    s = data["summary"]
    store = data["store"]["key"]
    cfg = STORES[store]
    first_day = date.fromisoformat(s["first_day"])
    last_day = date.fromisoformat(s["last_day"])
    window = f"{first_day:%b %Y} – {last_day:%b %Y}"
    title = "Frame Shop Performance" + ("" if store == "main" else f" · {cfg['tab']}")
    current = ' aria-current="page"'
    tabs = "".join(
        f'<a href="{t["page"]}"{current if t["key"] == store else ""}>{t["label"]}</a>'
        for t in data["tabs"]
    )
    method = "\n      ".join(f"<li>{b}</li>" for b in cfg["method"])
    revenue = (f"${s['revenue'] / 1e6:.2f}M" if s["revenue"] >= 1e6
               else f"${s['revenue'] / 1000:,.0f}k")
    volume = (f"{s['line_items']:,} line items" if s["line_items"]
              else f"{s['work_orders']:,} work orders")

    return f"""<!doctype html>
<!-- generated by dashboard/build.py -->
<meta charset="utf-8">
<title>{title}</title>
<meta name="description" content="Monthly revenue, year-over-year performance, and customer-retention KPIs for the framing store.">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONT_LINK}">
<style>{CSS}</style>

<div class="page">
  <nav class="storetabs" aria-label="Store">{tabs}</nav>
  <header class="masthead">
    <div class="brand">
      <p class="kicker">{cfg['kicker']}</p>
      <h1>Frame Shop Performance</h1>
    </div>
    <dl class="context">
      <div><dt>Coverage</dt><dd>{window}</dd></div>
      <div><dt>Customers</dt><dd>{s['customers']:,}</dd></div>
      <div><dt>Repeat</dt><dd>{s['repeat_customers']:,}</dd></div>
      <div><dt>Visits</dt><dd>{s['visits']:,}</dd></div>
      <div><dt>Revenue</dt><dd>{revenue}</dd></div>
    </dl>
  </header>

  <div class="controls">
    <label for="period">Report period</label>
    <select id="period" aria-describedby="period-tag"></select>
    <span id="period-tag" class="period-tag"></span>
  </div>

  <h2 class="section-label">The business</h2>
  <section id="biztiles" class="tiles biz" aria-label="Headline business numbers"></section>
  <section id="yoy" class="grid" aria-label="Monthly performance versus last year"></section>
  <section id="bizchart" class="grid bizchart" aria-label="Monthly revenue"></section>

  <h2 class="section-label">Customer retention</h2>
  <section id="tiles" class="tiles" aria-label="Headline values"></section>
  <section id="charts" class="grid" aria-label="Trends"></section>

  <section class="method">
    <h2>How these are built</h2>
    <ul>
      {method}
    </ul>
    <details>
      <summary>Full monthly data</summary>
      <div class="tablewrap">{_table(data)}</div>
    </details>
  </section>

  <footer class="colophon">
    <span>Generated {data['generated']} &middot; calc v{data['calc_version']} &middot; {volume}</span>
  </footer>
</div>

<script>window.DASH = {json.dumps(data, separators=(',', ':'))};</script>
<script>{APP_JS}</script>
"""


def _table(data: dict) -> str:
    months = sorted({m for k in data["series"].values() for m in k})
    head = "".join(f"<th>{m['label']}</th>" for m in data["meta"])
    body = []
    for mo in months:
        cells = [f'<th scope="row">{_ml(mo)}</th>']
        for meta in data["meta"]:
            row = data["series"][meta["key"]].get(mo)
            if row and row["v"] is not None:
                txt = _fmt(row["v"], meta["unit"])
                if not row["final"]:
                    txt += " *"
            else:
                txt = "—"
            cells.append(f"<td>{txt}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<table><thead><tr><th scope="col">Month</th>{head}</tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table>'
        '<p class="tablenote">* not yet final</p>'
    )


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "—"
    if unit == "pct":
        return f"{v * 100:.1f}%"
    if unit == "days":
        return f"{v:.0f}"
    return f"{v:,.0f}"


def _ml(m: str) -> str:
    return datetime.strptime(m, "%Y-%m").strftime("%b ’%y")


CSS = r"""
:root{
  --ground:#fcfbf8; --surface:#ffffff; --ink:#232019; --muted:#78756b;
  --hair:#e7e3d9; --grid:#efece3; --accent:#17588a; --wash:rgba(23,88,138,.09);
  --watch:#8a6d1f; --good:#17588a; --shadow:rgba(35,32,25,.12);
  --serif:"Spectral",Georgia,"Times New Roman",serif;
  --sans:"Archivo","Helvetica Neue",Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#161719; --surface:#1d1f21; --ink:#e7e4dc; --muted:#95928a;
  --hair:#2c2e30; --grid:#25272a; --accent:#4691c6; --wash:rgba(70,145,198,.14);
  --watch:#c6a24e; --good:#4691c6; --shadow:rgba(0,0,0,.4);
}}
:root[data-theme="dark"]{
  --ground:#161719; --surface:#1d1f21; --ink:#e7e4dc; --muted:#95928a;
  --hair:#2c2e30; --grid:#25272a; --accent:#4691c6; --wash:rgba(70,145,198,.14);
  --watch:#c6a24e; --good:#4691c6; --shadow:rgba(0,0,0,.4);
}
*{box-sizing:border-box}
[hidden]{display:none!important}

.storetabs{display:flex;gap:4px;margin:0 0 22px;border-bottom:1px solid var(--hair);
  overflow-x:auto;scrollbar-width:none}
.storetabs a{font:600 11px var(--sans);letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted);text-decoration:none;padding:9px 14px 10px;margin-bottom:-1px;
  border-bottom:2px solid transparent;white-space:nowrap}
.storetabs a:hover{color:var(--ink)}
.storetabs a[aria-current="page"]{color:var(--accent);border-bottom-color:var(--accent)}
.storetabs a:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
html{overflow-x:hidden}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;
  font-variant-numeric:tabular-nums;overflow-x:hidden}
.page{max-width:1120px;margin:0 auto;padding:clamp(20px,4vw,52px) clamp(16px,4vw,44px) 64px}

.masthead{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;
  gap:24px;padding-bottom:20px;border-bottom:2px solid var(--ink)}
.kicker{margin:0 0 4px;font-size:11px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);font-weight:500}
.masthead h1{margin:0;font-family:var(--serif);font-weight:600;font-size:clamp(30px,5vw,44px);
  letter-spacing:-.015em;line-height:1}
.context{display:flex;flex-wrap:wrap;gap:10px 26px;margin:0;max-width:100%}
.context div{display:flex;flex-direction:column}
.context dt{font-size:10px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted)}
.context dd{margin:2px 0 0;font-family:var(--serif);font-size:19px;font-weight:500}

.controls{display:flex;align-items:center;flex-wrap:wrap;gap:10px 14px;margin:22px 0 4px}
.controls label{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:600}
#period{font:500 14px var(--sans);color:var(--ink);background:var(--surface);
  border:1px solid var(--hair);border-radius:4px;padding:7px 30px 7px 11px;cursor:pointer;
  appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%2378756b' stroke-width='1.5'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 11px center}
#period:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.period-tag{font-size:12.5px;color:var(--muted)}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;
  letter-spacing:.06em;text-transform:uppercase;color:var(--accent)}
.badge .dot{width:7px;height:7px;border-radius:50%;background:var(--accent)}
@media (prefers-reduced-motion:no-preference){
  .badge .dot{animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
}

.section-label{margin:30px 0 0;font-family:var(--sans);font-size:11px;font-weight:600;
  letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.section-label:first-of-type{margin-top:24px}

.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;margin:10px 0 0;
  background:var(--hair);border:1px solid var(--hair)}
.tiles.biz{grid-template-columns:repeat(4,1fr)}
.tile{background:var(--surface);padding:16px 16px 14px;display:flex;flex-direction:column;min-height:150px}
.tiles.biz .tile{min-height:132px}
.tiles.biz .value{font-size:30px}
.delta.dual{display:flex;flex-direction:column;gap:3px}
.delta .d1{color:var(--muted);font-weight:400}
.delta .d2.good{color:var(--good)} .delta .d2.watch{color:var(--watch)}
.bizchart{margin-top:10px}
.eyebrow{margin:0;display:flex;align-items:center;font-size:11px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted);font-weight:600}
.tile .value{margin:9px 0 0;font-family:var(--serif);font-size:33px;font-weight:600;
  letter-spacing:-.02em;line-height:1}
.delta{margin:6px 0 0;font-size:12px;font-weight:500;color:var(--muted)}
.delta.good{color:var(--good)} .delta.watch{color:var(--watch)}
.delta .since{color:var(--muted);font-weight:400}
.spark{width:88px;height:26px;margin:auto 0 0;overflow:visible}
.spark path{fill:none;stroke:var(--accent);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.spark circle{fill:var(--accent)}
.asof{margin:8px 0 0;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:3px;
  padding:18px 18px 14px;margin:0;display:flex;flex-direction:column}
.card.wide{grid-column:1/-1}
.card h2{margin:0;font-family:var(--serif);font-size:19px;font-weight:600;letter-spacing:-.01em}
.blurb{margin:6px 0 12px;font-size:12.5px;color:var(--muted);line-height:1.45;max-width:52ch}
.card .foot{margin:10px 0 0;font-size:11px;color:var(--muted);letter-spacing:.01em}
.partial-note{color:var(--accent)}
.card.companion{background:var(--wash);border-color:transparent;justify-content:center}
.card.companion h2{font-size:17px}
.card.companion p{margin:8px 0 0;font-size:13px;color:var(--ink);line-height:1.5;max-width:46ch}

.infowrap{position:relative;display:inline-flex;vertical-align:middle}
.info{width:15px;height:15px;border-radius:50%;border:1px solid var(--muted);background:transparent;
  color:var(--muted);font:600 9px/1 var(--sans);cursor:help;padding:0;margin-left:7px;flex:none}
.info:hover,.info:focus-visible{border-color:var(--accent);color:var(--accent);outline:none}
.tip{position:absolute;left:0;top:calc(100% + 8px);width:min(300px,78vw);z-index:30;
  background:var(--surface);border:1px solid var(--hair);border-radius:5px;padding:11px 13px;
  font:400 12px/1.55 var(--sans);color:var(--ink);box-shadow:0 8px 28px var(--shadow);
  text-transform:none;letter-spacing:normal;text-align:left;
  opacity:0;visibility:hidden;transform:translateY(-4px);transition:opacity .12s,transform .12s}
.eyebrow .tip{width:min(240px,78vw)}
.tiles .tile:nth-child(n+4) .eyebrow .tip{left:auto;right:0}
.card:nth-child(even) h2 .tip{left:auto;right:0}
.infowrap:hover .tip,.infowrap:focus-within .tip{opacity:1;visibility:visible;transform:none}

svg.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .ax{fill:var(--muted);font-size:9.5px;font-family:var(--sans)}
.chart .baseline{stroke:var(--accent);stroke-width:1;stroke-dasharray:2 3;opacity:.7}
.chart .qband{fill:var(--accent);opacity:.07}
.chart .wash{fill:var(--wash);stroke:none}
.chart .ln{fill:none;stroke:var(--accent);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.chart .ln.tail{stroke-dasharray:3 3}
.chart .pt{fill:var(--surface);stroke:var(--accent);stroke-width:2}
.chart .pt.partial{stroke-dasharray:2 2}
.chart .bar{fill:var(--accent);opacity:.85}
.chart .bar.muted{opacity:.42}
.chart .bar.partial{opacity:.55}
.chart .ma{fill:none;stroke:var(--ink);stroke-width:1.5;opacity:.55;stroke-dasharray:3 3}
.chart .hover .cross{stroke:var(--ink);stroke-width:1;opacity:.35}
.chart .hover .halo{fill:none;stroke:var(--accent);stroke-width:2}
.readout{position:absolute;pointer-events:none;background:var(--ink);color:var(--ground);
  font-size:11px;font-weight:500;padding:3px 7px;border-radius:3px;white-space:nowrap;
  transform:translate(-50%,-140%);opacity:0;transition:opacity .1s}
.chartwrap{position:relative}

.method{margin-top:34px;border-top:1px solid var(--hair);padding-top:22px}
.method h2{margin:0 0 12px;font-family:var(--serif);font-size:17px;font-weight:600}
.method ul{margin:0;padding-left:18px;max-width:70ch}
.method li{margin:0 0 7px;font-size:13px;color:var(--muted)}
.method li b{color:var(--ink);font-weight:600}
.method details{margin-top:16px}
.method summary{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);
  cursor:pointer;font-weight:600}
.tablewrap{overflow-x:auto;margin-top:14px}
table{border-collapse:collapse;font-size:12px;width:100%}
th,td{padding:5px 10px;text-align:right;border-bottom:1px solid var(--hair);white-space:nowrap}
thead th{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
tbody th{text-align:left;font-weight:500;color:var(--muted)}
.tablenote{font-size:11px;color:var(--muted);margin:8px 0 0}

/* monthly year-over-year table */
.yoycard{gap:0}
.yoy-controls{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;margin:2px 0 14px}
.seg{display:inline-flex;border:1px solid var(--hair);border-radius:5px;overflow:hidden}
.seg button{font:600 10.5px var(--sans);letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);background:var(--surface);border:0;padding:6px 11px;cursor:pointer}
.seg button+button{border-left:1px solid var(--hair)}
.seg button[aria-pressed="true"]{background:var(--wash);color:var(--accent)}
.seg button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.chk{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--muted);cursor:pointer}
.chk input{accent-color:var(--accent);flex:none}
.yoytable{border-collapse:collapse;font-size:12.5px;width:100%;min-width:660px}
.yoytable th,.yoytable td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--hair);white-space:nowrap}
.yoytable thead tr:first-child th{border-bottom:0;text-align:center;padding-bottom:1px;
  font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:600}
.yoytable thead tr:last-child th{font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--muted);font-weight:600}
.yoytable tbody th{text-align:left;font-weight:500;color:var(--ink)}
.yoytable tbody td.good{color:var(--good)}
.yoytable tbody td.watch{color:var(--watch)}
.yoytable tr.live th{color:var(--accent)}
.yoytable .mtd{font-size:8.5px;font-weight:600;letter-spacing:.06em;color:var(--accent);
  border:1px solid currentColor;border-radius:3px;padding:0 3px;margin-left:6px;vertical-align:1px}
.yoytable tr.tot th,.yoytable tr.tot td{border-top:2px solid var(--ink);border-bottom:0;
  font-weight:600;font-family:var(--serif)}
.yoytable tr.aff td,.yoytable tr.aff th{background:var(--wash)}
.yoytable tr.ref td,.yoytable tr.ref th{color:var(--muted)}
.yoytable .pypart{font-size:9px;color:var(--muted);font-weight:400}
.yoytable .exmark{font-size:8.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  color:var(--watch);margin-left:6px;vertical-align:1px}

.colophon{margin-top:30px;padding-top:16px;border-top:1px solid var(--hair);
  font-size:11px;color:var(--muted);letter-spacing:.02em}

@media (max-width:820px){
  .tiles,.tiles.biz{grid-template-columns:repeat(2,1fr)}
  #tiles .tile:last-child{grid-column:1/-1}
  .grid{grid-template-columns:1fr}
}
@media (max-width:560px){
  .masthead{gap:16px}
  .brand,.context{width:100%}
  .context{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
}
@media (max-width:460px){
  .tiles,.tiles.biz{grid-template-columns:1fr}
  #tiles .tile:last-child{grid-column:auto}
}
@media (prefers-reduced-motion:no-preference){
  .draw .chart .ln:not(.tail){stroke-dasharray:1;stroke-dashoffset:1;animation:draw .9s ease .1s forwards}
  @keyframes draw{to{stroke-dashoffset:0}}
}
"""


APP_JS = r"""
(function(){
  var D = window.DASH, S = D.series, META = D.meta;
  var MN = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  var CW=484, CH=208, PL=42, PR=18, PT=16, PB=24, PW=CW-PL-PR, PH=CH-PT-PB;

  function ml(m){ var p=m.split('-'); return MN[+p[1]-1].slice(0,3)+" ’"+p[0].slice(2); }
  function mname(m){ return MN[+m.split('-')[1]-1]; }
  function addM(m,n){ var p=m.split('-').map(Number); var t=p[0]*12+p[1]-1+n; return Math.floor(t/12)+'-'+String(t%12+1).padStart(2,'0'); }
  function fmt(v,u){ if(v==null) return '—';
    if(u==='pct') return (v*100).toFixed(1)+'%';
    if(u==='days') return Math.round(v).toString();
    return Math.round(v).toLocaleString(); }
  function esc(s){ return s.replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
  function qOf(m){ return Math.ceil(+m.split('-')[1]/3); }
  // x-axis label for bar i of a monthly series: every `every`th month over a
  // short span, each January (as the year) once it covers several years. The
  // last two bars stay clear for the end label.
  function axisTick(ms,i,every){
    if(ms.length>40) return i<ms.length-9 && ms[i].slice(5)==='01' ? ms[i].slice(0,4) : null;
    if(i>=ms.length-2) return null;
    return i%every===0 ? ml(ms[i]) : null;
  }
  function qMonths(q){ var s=(q-1)*3+1; return [0,1,2].map(function(i){return '2026-'+String(s+i).padStart(2,'0');}); }

  // ---- period list: live current month + quarter, then closed history ----
  var CUR = D.current_month, CURQ = qOf(CUR);
  var periods = [];
  periods.push({id:'m'+CUR, kind:'month', asOf:CUR, live:true, label:mname(CUR)+' 2026'});
  periods.push({id:'q'+CURQ, kind:'quarter', q:CURQ, asOf:CUR, live:true, label:'Q'+CURQ+' 2026'});
  for(var q=CURQ-1;q>=1;q--) periods.push({id:'q'+q, kind:'quarter', q:q, asOf:qMonths(q)[2], label:'Q'+q+' 2026'});
  for(var mo=+CUR.split('-')[1]-1;mo>=1;mo--){ var m='2026-'+String(mo).padStart(2,'0');
    periods.push({id:'m'+m, kind:'month', asOf:m, label:mname(m)+' 2026'}); }

  // a snapshot store's "now" is its newest order, not the clock -- its open
  // month is the latest one on record rather than a live one
  var SNAP = D.store.snapshot, OPEN = SNAP ? 'latest' : 'live';
  var sel = document.getElementById('period');
  periods.forEach(function(p){
    var o=document.createElement('option'); o.value=p.id;
    o.textContent = p.label + (p.live ? '  —  '+OPEN : '');
    sel.appendChild(o);
  });

  // ---- chart ----
  function xOf(i,n){ return PL + (n>1 ? PW*i/(n-1) : PW/2); }
  function yOf(v,c){ return PT + PH*(1 - v/c); }
  function dstr(pts){ return 'M '+pts.map(function(p){return p[0].toFixed(1)+' '+p[1].toFixed(1);}).join(' L '); }

  function chartSVG(meta, pts, band){
    var n = pts.length;
    if(!n) return '<svg viewBox="0 0 '+CW+' '+CH+'"></svg>';
    var c = meta.ceiling, u = meta.unit;
    var coords = pts.map(function(p,i){ return [xOf(i,n), yOf(p.v,c)]; });
    var solidN = pts.filter(function(p){return p.final;}).length;
    var solid = coords.slice(0, solidN);
    var tail = (solidN>0 && solidN<n) ? coords.slice(solidN-1) : [];
    var g = [];

    if(band){
      var bi = band.map(function(bm){ return pts.findIndex(function(p){return p.m===bm;}); }).filter(function(x){return x>=0;});
      if(bi.length){
        var x0 = xOf(Math.min.apply(null,bi), n) - (n>1?PW/(n-1)/2:0);
        var x1 = xOf(Math.max.apply(null,bi), n) + (n>1?PW/(n-1)/2:0);
        x0 = Math.max(x0, PL); x1 = Math.min(x1, CW-PR);
        g.push('<rect class="qband" x="'+x0.toFixed(1)+'" y="'+PT+'" width="'+(x1-x0).toFixed(1)+'" height="'+PH+'"/>');
      }
    }
    [0,0.5,1].forEach(function(f){
      var gy = yOf(f*c, c);
      g.push('<line class="grid" x1="'+PL+'" y1="'+gy.toFixed(1)+'" x2="'+(CW-PR)+'" y2="'+gy.toFixed(1)+'"/>');
      g.push('<text class="ax" x="'+(PL-6)+'" y="'+(gy+3).toFixed(1)+'" text-anchor="end">'+esc(fmt(f*c,u))+'</text>');
    });
    var seen = {};
    [0, n-1, n>>2, (n>>1), (3*n)>>2].forEach(function(i){
      if(i<0||i>=n||seen[i]) return; seen[i]=1;
      var px = xOf(i,n), a = i===0?'start':(i===n-1?'end':'middle');
      g.push('<text class="ax" x="'+px.toFixed(1)+'" y="'+(CH-7)+'" text-anchor="'+a+'">'+esc(ml(pts[i].m))+'</text>');
    });
    if(meta.baseline!=null && meta.baseline<=c){
      var by = yOf(meta.baseline,c);
      g.push('<line class="baseline" x1="'+PL+'" y1="'+by.toFixed(1)+'" x2="'+(CW-PR)+'" y2="'+by.toFixed(1)+'"/>');
    }
    if(solid.length>1){
      var area = dstr(solid)+' L '+solid[solid.length-1][0].toFixed(1)+' '+(PT+PH)+' L '+solid[0][0].toFixed(1)+' '+(PT+PH)+' Z';
      g.push('<path class="wash" d="'+area+'"/>');
      g.push('<path class="ln" pathLength="1" d="'+dstr(solid)+'"/>');
    }
    if(tail.length>1) g.push('<path class="ln tail" d="'+dstr(tail)+'"/>');
    if(solid.length) g.push('<circle class="pt" cx="'+solid[solid.length-1][0].toFixed(1)+'" cy="'+solid[solid.length-1][1].toFixed(1)+'" r="3.5"/>');
    if(tail.length) g.push('<circle class="pt partial" cx="'+tail[tail.length-1][0].toFixed(1)+'" cy="'+tail[tail.length-1][1].toFixed(1)+'" r="3.5"/>');

    return '<svg viewBox="0 0 '+CW+' '+CH+'" role="img" class="chart" '+
      'aria-label="'+esc(meta.label)+' trend">'+
      '<g class="hover" hidden><line class="cross"/><circle class="halo" r="4.5"/></g>'+
      g.join('')+'</svg>';
  }

  function sparkSVG(pts, c){
    if(pts.length<2) return '';
    var w=88,h=26, xs=pts.map(function(_,i){return 4+(w-8)*i/(pts.length-1);}),
        ys=pts.map(function(p){return 3+(h-6)*(1-p.v/c);});
    var d='M '+xs.map(function(x,i){return x.toFixed(1)+' '+ys[i].toFixed(1);}).join(' L ');
    return '<svg class="spark" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none" aria-hidden="true">'+
      '<path d="'+d+'"/><circle cx="'+xs[xs.length-1].toFixed(1)+'" cy="'+ys[ys.length-1].toFixed(1)+'" r="2"/></svg>';
  }

  // ---- hover ----
  function wireHover(wrap, meta, pts){
    var svg = wrap.querySelector('svg.chart'); if(!svg) return;
    var n = pts.length, gg = svg.querySelector('.hover'),
        cross = svg.querySelector('.cross'), halo = svg.querySelector('.halo');
    var tip = document.createElement('div'); tip.className='readout'; wrap.appendChild(tip);
    function show(i){
      var p = pts[i]; if(!p) return;
      var x = xOf(i,n), y = yOf(p.v, meta.ceiling);
      cross.setAttribute('x1',x); cross.setAttribute('x2',x);
      cross.setAttribute('y1',PT); cross.setAttribute('y2',PT+PH);
      halo.setAttribute('cx',x); halo.setAttribute('cy',y);
      gg.hidden = false;
      var r = svg.getBoundingClientRect(), wr = wrap.getBoundingClientRect();
      tip.style.left = (r.left - wr.left + x/CW*r.width) + 'px';
      tip.style.top  = (r.top - wr.top + y/CH*r.height) + 'px';
      tip.textContent = ml(p.m) + (p.final?'':' · so far') + '  ' +
        (meta.unit==='days' ? Math.round(p.v)+'d' : fmt(p.v, meta.unit));
      tip.style.opacity = 1;
    }
    function hide(){ gg.hidden = true; tip.style.opacity = 0; }
    svg.addEventListener('pointermove', function(e){
      var r = svg.getBoundingClientRect();
      var i = Math.round(((e.clientX-r.left)/r.width*CW - PL)/PW*(n-1));
      show(Math.max(0, Math.min(n-1, i)));
    });
    svg.addEventListener('pointerleave', hide);
    svg.style.touchAction = 'pan-y';
  }

  // ---- business (headline "how's the store doing" numbers) ----
  function usd(v){
    if(v==null || isNaN(v)) return '—';
    var a=Math.abs(v), sg=v<0?'-':'';
    if(a>=1000000) return sg+'$'+(a/1000000).toFixed(2)+'M';
    if(a>=100000) return sg+'$'+Math.round(a/1000)+'k';
    if(a>=1000)   return sg+'$'+(a/1000).toFixed(1)+'k';
    return sg+'$'+Math.round(a);
  }
  function usdFull(v){
    if(v==null || isNaN(v)) return '—';
    return (v<0?'-$':'$')+Math.round(Math.abs(v)).toLocaleString();
  }
  function pctTxt(d){
    if(d==null) return '—';
    var p=Math.abs(d*100);
    return p<0.5 ? 'flat' : (d>0?'▲ ':'▼ ')+p.toFixed(0)+'%';
  }
  function relCls(d){ return (d==null || Math.abs(d)<0.005) ? '' : d>0 ? 'good' : 'watch'; }
  function rel(cur,base){ return base>0 ? cur/base-1 : null; }
  function shortMl(m){ return MN[+m.split('-')[1]-1].slice(0,3)+' '+m.slice(0,4); }
  function mDay(m,d){ return MN[+m.split('-')[1]-1].slice(0,3)+' '+d; }

  function renderBusiness(p){
    var host=document.getElementById('biztiles'), chost=document.getElementById('bizchart');
    host.innerHTML=''; chost.innerHTML='';
    var B=D.business, M=B.monthly, i;
    function rev(m){ return M[m]?M[m].rev:0; }
    function vis(m){ return M[m]?M[m].v:0; }
    var PARTIAL=B.partial_month;

    var months = p.kind==='month' ? [p.asOf] : qMonths(p.q).filter(function(m){return m<=CUR;});
    var hasPartial = months.indexOf(PARTIAL) >= 0;
    var k = p.kind==='month' ? 1 : 3;

    var pr = months.reduce(function(a,m){return a+rev(m);},0);
    var pv = months.reduce(function(a,m){return a+vis(m);},0);
    var lyR = months.reduce(function(a,m){return a+(m===PARTIAL?B.partial_ly_rev:rev(addM(m,-12)));},0);
    var lyV = months.reduce(function(a,m){return a+(m===PARTIAL?B.partial_ly_v:vis(addM(m,-12)));},0);
    var prevR = months.reduce(function(a,m){return a+rev(addM(m,-k));},0);

    var ttm=0, ttmPrev=0;
    for(i=0;i<12;i++)  ttm     += rev(addM(p.asOf,-i));
    for(i=12;i<24;i++) ttmPrev += rev(addM(p.asOf,-i));
    if(hasPartial) ttmPrev = ttmPrev - rev(addM(p.asOf,-12)) + B.partial_ly_rev;

    var proj=null;
    if(hasPartial && B.partial_day>0)
      proj = pr - rev(PARTIAL) + rev(PARTIAL)/B.partial_day*B.days_in_partial;

    var yr = +months[0].slice(0,4);
    var lab = p.kind==='month'
      ? {title:MN[+p.asOf.split('-')[1]-1]+' '+p.asOf.slice(0,4),
         mom:'vs '+shortMl(addM(p.asOf,-1)), yoy:'vs '+shortMl(addM(p.asOf,-12))}
      : {title:'Q'+p.q+' '+yr,
         mom:'vs Q'+(p.q>1?p.q-1:4)+' '+(p.q>1?yr:yr-1), yoy:'vs Q'+p.q+' '+(yr-1)};

    var momSpan = hasPartial
      ? '<span class="d1">through '+mDay(PARTIAL,B.partial_day)+
        (proj!=null ? ' &middot; on pace for ~'+usd(proj) : '')+'</span>'
      : '<span class="d2 '+relCls(rel(pr,prevR))+'">'+pctTxt(rel(pr,prevR))+' '+esc(lab.mom)+'</span>';

    var tracked = (+CUR.slice(0,4)-+B.first_day.slice(0,4))*12
                + (+CUR.slice(5,7)-+B.first_day.slice(5,7)) + 1;

    [
      {eyebrow: lab.title+(hasPartial?' so far':''), value: usd(pr),
       delta: '<p class="delta dual">'+momSpan+
         '<span class="d2 '+relCls(rel(pr,lyR))+'">'+pctTxt(rel(pr,lyR))+' '+esc(lab.yoy)+'</span></p>'},
      {eyebrow:'Trailing 12 months', value: usd(ttm),
       delta:'<p class="delta dual"><span class="d1">rolling year to '+esc(shortMl(p.asOf))+'</span>'+
         '<span class="d2 '+relCls(rel(ttm,ttmPrev))+'">'+pctTxt(rel(ttm,ttmPrev))+' vs prior 12&nbsp;mo</span></p>'},
      {eyebrow:'Orders '+(hasPartial?'so far':'in period'), value: pv.toLocaleString(),
       delta:'<p class="delta dual"><span class="d1">'+usd(pv?pr/pv:null)+' average ticket</span>'+
         '<span class="d2 '+relCls(rel(pv,lyV))+'">'+pctTxt(rel(pv,lyV))+' '+esc(lab.yoy)+'</span></p>'},
      {eyebrow:'Revenue to date', value: usd(B.all_time_revenue),
       delta:'<p class="delta dual"><span class="d1">since '+esc(shortMl(B.first_day.slice(0,7)))+
         '</span><span class="d1">'+tracked+' months on record</span></p>'},
    ].forEach(function(t){
      var el=document.createElement('article'); el.className='tile';
      el.innerHTML='<p class="eyebrow">'+esc(t.eyebrow)+'</p><p class="value">'+esc(t.value)+'</p>'+t.delta;
      host.appendChild(el);
    });

    var fig=document.createElement('figure'); fig.className='card wide';
    fig.innerHTML='<figcaption><h2>Monthly revenue</h2><p class="blurb">Booked per month — '+
      'retail minus discounts, voided orders removed. Dashed line is the 3-month average. '+
      'The newest bar is month-to-date.</p></figcaption>'+revBarSVG(months);
    chost.appendChild(fig);
  }

  function revBarSVG(sel){
    var B=D.business, M=B.monthly;
    var W=960, H=210, L=46, R=14, T=12, Bt=26, w=W-L-R, h=H-T-Bt;
    var ms=Object.keys(M).filter(function(m){return m>=D.store.chart_from && m<=CUR;}).sort();
    var n=ms.length; if(!n) return '<svg viewBox="0 0 '+W+' '+H+'"></svg>';
    var ceil=Math.ceil(Math.max.apply(null, ms.map(function(m){return M[m].rev;}))/10000)*10000 || 10000;
    var X=function(i){ return L + (n>1 ? w*i/(n-1) : w/2); };
    var Y=function(v){ return T + h*(1 - v/ceil); };
    var bw=Math.min(26, w/n*0.6), g=[];
    [0,0.5,1].forEach(function(f){
      var gy=Y(f*ceil);
      g.push('<line class="grid" x1="'+L+'" y1="'+gy.toFixed(1)+'" x2="'+(W-R)+'" y2="'+gy.toFixed(1)+'"/>');
      g.push('<text class="ax" x="'+(L-6)+'" y="'+(gy+3).toFixed(1)+'" text-anchor="end">$'+Math.round(f*ceil/1000)+'k</text>');
    });
    ms.forEach(function(m,i){
      var cx=X(i), bh=h*M[m].rev/ceil, by=T+h-bh;
      var cls='bar'+(m===B.partial_month?' partial'
        :(sel.length>1 && sel.indexOf(m)<0?' muted':''));
      var lbl=M[m].rev>=1000 ? '$'+(M[m].rev/1000).toFixed(1)+'k' : '$'+Math.round(M[m].rev);
      g.push('<rect class="'+cls+'" x="'+(cx-bw/2).toFixed(1)+'" y="'+by.toFixed(1)+'" width="'+bw.toFixed(1)+
        '" height="'+bh.toFixed(1)+'" rx="1"><title>'+esc(ml(m))+': '+lbl+
        (m===B.partial_month?' so far':'')+'</title></rect>');
      var tick=axisTick(ms,i,3);
      if(tick)
        g.push('<text class="ax" x="'+cx.toFixed(1)+'" y="'+(H-8)+'" text-anchor="middle">'+esc(tick)+'</text>');
    });
    g.push('<text class="ax" x="'+X(n-1).toFixed(1)+'" y="'+(H-8)+'" text-anchor="end">'+esc(ml(ms[n-1]))+'</text>');
    var pts=[];
    for(var j=2;j<n;j++){
      var avg=(M[ms[j]].rev+M[ms[j-1]].rev+M[ms[j-2]].rev)/3;
      pts.push(X(j).toFixed(1)+' '+Y(avg).toFixed(1));
    }
    if(pts.length>1) g.push('<path class="ma" d="M '+pts.join(' L ')+'"/>');
    return '<svg viewBox="0 0 '+W+' '+H+'" role="img" class="chart" preserveAspectRatio="none">'+
      g.join('')+'</svg>';
  }

  // ---- monthly performance vs last year (standalone, not period-scoped) ----
  var yoyMode = 'trailing', yoyEx = false;

  function renderYoY(){
    var host = document.getElementById('yoy');
    var B = D.business, M = B.monthly, OM = (B.outlier && B.outlier.monthly) || {};
    var PARTIAL = B.partial_month, CUR_YR = CUR.slice(0,4);
    function r(m){ var x = M[m]?M[m].rev:0; if(yoyEx && OM[m]) x -= OM[m].rev; return x; }
    function vv(m){ var x = M[m]?M[m].v:0; if(yoyEx && OM[m]) x -= OM[m].v; return x; }
    function avg(rev,n){ return n>0 ? rev/n : null; }

    // Trailing mode shows the current month PLUS a full 12 months back (13 rows,
    // Sep'25..Sep'26 style) so the oldest row is the same calendar month as the
    // current one, a year back. The "Trailing 12 mo" total stays a true rolling
    // 12-month window (totFrom..CUR) so it keeps matching the "Trailing 12
    // months" business tile above -- the extra oldest row is context, not
    // counted in that total.
    var start = yoyMode==='calendar' ? CUR_YR+'-01' : addM(CUR,-12);
    var totFrom = yoyMode==='calendar' ? start : addM(CUR,-11);
    var months = []; for(var m=start; m<=CUR; m=addM(m,1)) months.push(m);

    var ex = B.outlier || {}, hasEx = ex.total_v > 0;
    var exMonths = ex.months || [];
    function isOut(m){ return exMonths.indexOf(m) >= 0; }

    var tot = {cyR:0, cyV:0, pyR:0, pyV:0}, anyAff = false;
    var rows = months.map(function(cm){
      var pm = addM(cm,-12), part = cm===PARTIAL;
      var cyR = r(cm), cyV = vv(cm);
      var pyR = part ? (yoyEx ? B.partial_ly_rev_ex : B.partial_ly_rev) : r(pm);
      var pyV = part ? (yoyEx ? B.partial_ly_v_ex   : B.partial_ly_v)   : vv(pm);
      var aff = yoyEx && (isOut(cm) || isOut(pm));
      if(aff) anyAff = true;
      if(cm >= totFrom){ tot.cyR+=cyR; tot.cyV+=cyV; tot.pyR+=pyR; tot.pyV+=pyV; }
      return {cm:cm, part:part, cyR:cyR, cyV:cyV, pyR:pyR, pyV:pyV, aff:aff, ref:cm<totFrom};
    });

    function dcell(d){ return '<td class="'+relCls(d)+'">'+pctTxt(d)+'</td>'; }
    function line(lbl, x, cls){
      // x.part's "last yr" figures are a partial month too (same day-count as
      // this year's MTD, not the full month) -- mark them so they're never
      // mistaken for the full prior-year month shown on its own row above.
      var pn = x.part ? ' <span class="pypart">(1–'+B.partial_day+')</span>' : '';
      return '<tr'+(cls?' class="'+cls+'"':'')+'><th scope="row">'+lbl+'</th>'+
        '<td>'+usdFull(x.pyR)+pn+'</td><td>'+usdFull(x.cyR)+'</td>'+dcell(rel(x.cyR,x.pyR))+
        '<td>'+x.pyV.toLocaleString()+pn+'</td><td>'+x.cyV.toLocaleString()+'</td>'+dcell(rel(x.cyV,x.pyV))+
        '<td>'+usdFull(avg(x.cyR,x.cyV))+'</td>'+dcell(rel(avg(x.cyR,x.cyV),avg(x.pyR,x.pyV)))+'</tr>';
    }

    var body = rows.map(function(x){
      var lbl = MN[+x.cm.split('-')[1]-1].slice(0,3) +
        (yoyMode==='trailing' ? ' ’'+x.cm.slice(2,4) : '') +
        (x.part ? '<span class="mtd">MTD</span>' : '') +
        (x.aff ? '<span class="exmark" title="'+esc(ex.label)+' removed">outlier removed</span>' : '');
      return line(lbl, x, (x.part ? 'live' : '') + (x.aff ? ' aff' : '') + (x.ref ? ' ref' : ''));
    }).join('');
    body += line(yoyMode==='calendar' ? CUR_YR+' YTD' : 'Trailing 12 mo', tot, 'tot');

    var exNote = hasEx ? '<label class="chk"><input type="checkbox" id="yoyEx"'+(yoyEx?' checked':'')+'> '+
      'Exclude the '+esc(ex.label)+' <span class="since">('+usdFull(ex.total_rev)+' over '+
      ex.total_v+' orders, '+exMonths.map(ml).join(' &amp; ')+')</span></label>' : '';

    var notes = [];
    if(rows.some(function(x){return x.ref;}))
      notes.push(ml(rows[0].cm)+' is shown for reference (same month as the current one, '+
        'a year back); the total below covers the rolling 12 months, '+ml(totFrom)+'–'+ml(CUR)+'.');
    if(rows.some(function(x){return x.part;}))
      notes.push('MTD: through '+mDay(PARTIAL,B.partial_day)+', measured against '+
        mname(addM(PARTIAL,-12)).slice(0,3)+' 1–'+B.partial_day+', '+addM(PARTIAL,-12).slice(0,4)+'.');
    if(yoyEx && hasEx && anyAff)
      notes.push(exMonths.map(function(m){
        return '−'+usdFull((B.outlier.monthly[m]||{}).rev||0)+' ('+ml(m)+')'; }).join(', ')+
        ' removed from last-year revenue.');
    else if(yoyEx && hasEx && !anyAff)
      notes.push('The '+esc(ex.label)+' lands in '+exMonths.map(ml).join(' &amp; ')+
        ', outside this view — it only moves the Trailing 12 months comparison.');
    var footNote = notes.map(function(t){ return '<p class="foot">'+t+'</p>'; }).join('');

    host.innerHTML =
      '<figure class="card wide yoycard">'+
      '<figcaption><h2>Monthly performance vs. last year</h2>'+
      '<p class="blurb">Each month against the same month a year earlier — revenue, orders, and average '+
      'ticket. Completed months are the full month; the current month is month-to-date against the same '+
      'run of days last year.</p></figcaption>'+
      '<div class="yoy-controls">'+
        '<div class="seg" role="group" aria-label="Range">'+
          '<button type="button" data-m="calendar"'+(yoyMode==='calendar'?' aria-pressed="true"':'')+'>Calendar year</button>'+
          '<button type="button" data-m="trailing"'+(yoyMode==='trailing'?' aria-pressed="true"':'')+'>Trailing 12 months</button>'+
        '</div>'+exNote+
      '</div>'+
      '<div class="tablewrap"><table class="yoytable">'+
        '<thead><tr><th></th><th colspan="3">Revenue</th><th colspan="3">Orders</th><th colspan="2">Avg ticket</th></tr>'+
        '<tr><th scope="col">Month</th>'+
          '<th scope="col">Last yr</th><th scope="col">This yr</th><th scope="col">Δ</th>'+
          '<th scope="col">Last yr</th><th scope="col">This yr</th><th scope="col">Δ</th>'+
          '<th scope="col">This yr</th><th scope="col">Δ</th></tr></thead>'+
        '<tbody>'+body+'</tbody>'+
      '</table></div>'+footNote+
      '</figure>';

    host.querySelectorAll('.seg button').forEach(function(b){
      b.addEventListener('click', function(){ yoyMode = b.dataset.m; renderYoY(); });
    });
    var box = document.getElementById('yoyEx');
    if(box) box.addEventListener('change', function(){
      yoyEx = box.checked;
      // the outlier only falls in the trailing-12 window; if the user turns the
      // exclusion on from the calendar view (where it changes nothing), move
      // them to the view where it actually does something.
      if(yoyEx && yoyMode==='calendar' &&
         !months.some(function(m){ return isOut(m) || isOut(addM(m,-12)); }))
        yoyMode = 'trailing';
      renderYoY();
    });
  }

  // ---- render ----
  var first = true;
  function render(pid){
    var p = periods.filter(function(x){return x.id===pid;})[0] || periods[0];
    var tag = document.getElementById('period-tag');
    if(p.live && SNAP){
      var dt = D.store.data_through;
      tag.textContent = 'Latest snapshot · orders through ' + mDay(dt.slice(0,7), +dt.slice(8)) + ', ' + dt.slice(0,4);
    } else if(p.live){
      tag.innerHTML = '<span class="badge"><span class="dot"></span>Live</span> · updated ' + esc(D.generated);
    } else {
      var closed = p.kind==='quarter' ? qMonths(p.q)[2] : p.asOf;
      tag.textContent = 'Closed report · figures frozen at ' + mname(closed) + ' 2026';
    }

    renderBusiness(p);

    var band = p.kind==='quarter' ? qMonths(p.q) : null;
    var cmpMonth = p.kind==='quarter' ? addM(qMonths(p.q)[0], -1) : addM(p.asOf, -6);

    var tiles = document.getElementById('tiles');
    var charts = document.getElementById('charts');
    tiles.innerHTML = ''; charts.innerHTML = '';
    charts.classList.toggle('draw', first);

    META.forEach(function(meta){
      var ser = S[meta.key];
      var head = ser[p.asOf] || null;
      var cur = head ? head.v : null;

      // plotted points: plot_from .. asOf, non-null
      var pts = Object.keys(ser).filter(function(m){
        return m>=meta.plot_from && m<=p.asOf && ser[m] && ser[m].v!=null;
      }).sort().map(function(m){ return {m:m, v:ser[m].v, final:ser[m].final}; });

      // delta
      var dtxt='', dcls='flat';
      if(meta.key==='reactivation_rate' && D.store.reactivation_delta){
        dtxt = '<span class="since">'+esc(D.store.reactivation_delta)+'</span>';
      } else if(head && ser[cmpMonth] && ser[cmpMonth].v!=null){
        var dv = cur - ser[cmpMonth].v;
        var since = ' <span class="since">vs ' + ml(cmpMonth) + '</span>';
        if(Math.abs(dv) < (meta.unit==='pct' ? 0.0005 : meta.unit==='days' ? 0.5 : 0.5)){
          dtxt = 'no change' + since;
        } else {
          var up = dv>0, good = (up && meta.good==='up') || (!up && meta.good==='down');
          dcls = good ? 'good' : 'watch';
          var mag = meta.unit==='days' ? Math.abs(dv).toFixed(0) : fmt(Math.abs(dv), meta.unit);
          dtxt = (up?'▲':'▼') + ' ' + mag + since;
        }
      }

      var partialTag = head && !head.final
        ? ' · <span class="partial-note">' + mname(p.asOf) + ' still open</span>' : '';
      var asof = head ? (mname(p.asOf).slice(0,3) + ' ’' + p.asOf.slice(2,4) +
        (head.final ? ' · final' : ' · '+OPEN)) : '';

      var t = document.createElement('article'); t.className = 'tile';
      t.innerHTML =
        '<p class="eyebrow">' + esc(meta.tile) +
          '<span class="infowrap"><button class="info" type="button" aria-label="What ' +
          esc(meta.label) + ' means">i</button><span class="tip" role="tooltip">' +
          esc(meta.long) + '</span></span></p>' +
        '<p class="value">' + esc(fmt(cur, meta.unit)) + '</p>' +
        '<p class="delta ' + dcls + '">' + (dtxt || '&nbsp;') + '</p>' +
        sparkSVG(pts, meta.ceiling) +
        '<p class="asof">' + asof + '</p>';
      tiles.appendChild(t);

      var baseNote = meta.baseline!=null
        ? 'Dashed line: first-pass baseline, ' + fmt(meta.baseline, meta.unit) + '.'
        : 'Not previously tracked.';
      var fig = document.createElement('figure'); fig.className = 'card';
      fig.innerHTML =
        '<figcaption><h2>' + esc(meta.label) +
          '<span class="infowrap"><button class="info" type="button" aria-label="What ' +
          esc(meta.label) + ' means">i</button><span class="tip" role="tooltip">' +
          esc(meta.long) + '</span></span></h2>' +
          '<p class="blurb">' + esc(meta.short) + '</p></figcaption>' +
        '<div class="chartwrap">' + chartSVG(meta, pts, band) + '</div>' +
        '<p class="foot">' + baseNote + partialTag + '</p>';
      charts.appendChild(fig);
      wireHover(fig.querySelector('.chartwrap'), meta, pts);

      if(meta.key==='reactivation_rate'){
        var aside = document.createElement('aside'); aside.className = 'card companion';
        aside.innerHTML = D.store.companion.replace('{pool}',
          head && head.d!=null ? Math.round(head.d).toLocaleString() : 'The');
        charts.appendChild(aside);
      }
    });

    // bar chart: new customers per month, up to the report month
    var bar = document.createElement('figure'); bar.className = 'card wide';
    var cdata = D.cohorts.filter(function(c){ return c.m>=D.store.chart_from && c.m<=p.asOf && c.m<D.current_month; });
    bar.innerHTML = '<figcaption><h2>New customers per month</h2>' +
      '<p class="blurb">First-ever visits — cohort-size context for the rates above. ' +
      esc(D.store.cohort_blurb) + '</p></figcaption>' + barSVG(cdata);
    charts.appendChild(bar);

    first = false;
  }

  function barSVG(data){
    var n = data.length; if(!n) return '<svg viewBox="0 0 '+CW+' '+CH+'"></svg>';
    var ceil = 10*(Math.floor(Math.max.apply(null, data.map(function(c){return c.n;}))/10)+1);
    var bw = PW/n*0.64, g = [];
    [0,0.5,1].forEach(function(f){
      var gy = yOf(f*ceil, ceil);
      g.push('<line class="grid" x1="'+PL+'" y1="'+gy.toFixed(1)+'" x2="'+(CW-PR)+'" y2="'+gy.toFixed(1)+'"/>');
      g.push('<text class="ax" x="'+(PL-6)+'" y="'+(gy+3).toFixed(1)+'" text-anchor="end">'+Math.round(f*ceil)+'</text>');
    });
    data.forEach(function(c,i){
      var cx = xOf(i,n), bh = PH*c.n/ceil, by = PT+PH-bh;
      g.push('<rect class="bar" x="'+(cx-bw/2).toFixed(1)+'" y="'+by.toFixed(1)+'" width="'+bw.toFixed(1)+
        '" height="'+bh.toFixed(1)+'" rx="1"><title>'+esc(ml(c.m))+': '+c.n+' new</title></rect>');
      var tick=axisTick(data.map(function(d){return d.m;}),i,4);
      if(tick)
        g.push('<text class="ax" x="'+cx.toFixed(1)+'" y="'+(CH-7)+'" text-anchor="middle">'+esc(tick)+'</text>');
    });
    g.push('<text class="ax" x="'+xOf(n-1,n).toFixed(1)+'" y="'+(CH-7)+'" text-anchor="end">'+esc(ml(data[n-1].m))+'</text>');
    return '<svg viewBox="0 0 '+CW+' '+CH+'" role="img" class="chart">'+g.join('')+'</svg>';
  }

  // switching store tabs keeps the chosen report period
  function syncTabs(pid){
    document.querySelectorAll('.storetabs a').forEach(function(a){
      a.href = a.getAttribute('href').split('?')[0] + '?p=' + encodeURIComponent(pid);
    });
  }

  sel.addEventListener('change', function(){
    render(sel.value);
    var u = new URL(location.href); u.searchParams.set('p', sel.value);
    history.replaceState(null, '', u);
    syncTabs(sel.value);
  });

  var want = new URLSearchParams(location.search).get('p');
  var start = periods.some(function(p){return p.id===want;}) ? want : periods[0].id;
  sel.value = start;
  render(start);
  if(want) syncTabs(start);
  renderYoY();
})();
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", choices=list(STORES), default="main")
    ap.add_argument("--db", default="warehouse.db")
    ap.add_argument("--out", default="dashboard/index.html")
    ap.add_argument("--json", help="also write the embedded data to this path")
    args = ap.parse_args(argv)

    data = load(args.db, args.store)
    Path(args.out).write_text(render(data), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"wrote {args.out}  ({Path(args.out).stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
