"""Per-report configuration.

Adding another Store Reporting page (Payments, Orders, ...) should be a new
entry here, not a code change: they share the
`ctl00$ContentPlaceHolder1$reportViewer` master-page naming. Confirm each
report's date-field control IDs in DevTools before adding it -- some reports
have more parameters than a plain date range.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportSpec:
    key: str  # URL-safe id used in the API path
    page_path: str  # e.g. "/Reports/WorkOrderList"
    start_date_field: str
    end_date_field: str
    view_button_field: str
    view_button_value: str = "View Report"


_WOL = "ctl00$ContentPlaceHolder1$reportViewer$ctl08"

REPORTS: dict[str, ReportSpec] = {
    "work-order-list": ReportSpec(
        key="work-order-list",
        page_path="/Reports/WorkOrderList",
        start_date_field=f"{_WOL}$ctl03$txtValue",
        end_date_field=f"{_WOL}$ctl05$txtValue",
        view_button_field=f"{_WOL}$ctl00",
    ),
}


def get_report(key: str) -> ReportSpec:
    try:
        return REPORTS[key]
    except KeyError:
        raise KeyError(f"unknown report {key!r}; known: {sorted(REPORTS)}") from None
