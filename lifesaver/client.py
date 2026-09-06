"""HTTP client for lsscloud.com's SSRS ReportViewer.

This is the class form of lifesaver_report_pull.py (kept as the standalone
reference script). Differences:

  - one long-lived requests.Session, reused across calls (cookie jar)
  - transparent re-login + one retry when the session has expired mid-flow
  - report parameters come from lifesaver.reports.ReportSpec, not module constants

The 3-step flow itself is unchanged: GET page -> POST date range + "View Report"
-> scrape ReportSession/ControlID from the embedded image src -> GET CSV export.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from .config import DEFAULT_RSPROXY, Settings
from .reports import ReportSpec

log = logging.getLogger(__name__)

_HIDDEN_FIELDS = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__EVENTVALIDATION",
    "__EVENTTARGET",
    "__EVENTARGUMENT",
    "__RequestVerificationToken",
)

_LOGIN_PATH = "/Account/Login"
_TERMINATE_PATH = "/Account/TerminateSession/"
_LOGOFF_PATH = "/Account/LogOff"
_EXPORT_PATH = "/Reserved.ReportViewerWebControl.axd"

# the login POST embeds: var loginModel = {...};
_LOGIN_MODEL_RE = re.compile(r"var loginModel = (\{.*?\});", re.S)


class LifesaverError(Exception):
    """Base class for all client errors."""


class AuthError(LifesaverError):
    """Login failed, or the session expired and could not be renewed."""


class ReportError(LifesaverError):
    """The report page loaded but did not produce a result (e.g. bad params)."""


def format_ssrs_date(d: date) -> str:
    """M/d/yyyy, no zero-padding -- confirmed against the live textbox."""
    return f"{d.month}/{d.day}/{d.year}"


class LifesaverClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self._s = settings
        self._session = session or requests.Session()
        self._authenticated = False

    # --- public API -------------------------------------------------------

    def fetch_csv(self, report: ReportSpec, start: date, end: date) -> bytes:
        """Run the full flow for a date range, returning raw CSV bytes.

        Re-logs-in and retries once if the session is found to be expired.
        """
        start_str, end_str = format_ssrs_date(start), format_ssrs_date(end)
        for attempt in (1, 2):
            self._ensure_authenticated()
            try:
                return self._run_flow(report, start_str, end_str)
            except AuthError:
                if attempt == 2:
                    raise
                log.info("session expired mid-flow; re-authenticating")
                self._authenticated = False
        raise AssertionError("unreachable")

    # --- flow steps ------------------------------------------------------

    def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            self.login()

    def login(self) -> None:
        resp = self._post_login()
        if _LOGIN_PATH not in resp.url:
            self._authenticated = True
            log.info("authenticated to %s", self._s.lifesaver_base_url)
            return

        # Still on the login page. LifeSaver enforces one session per user, so
        # the usual reason is our own prior session wasn't logged out.
        model = self._parse_login_model(resp.text)
        if model is None:
            raise AuthError(
                "still on the login page after POST -- check credentials "
                "(LIFESAVER_USERNAME / LIFESAVER_PASSWORD)"
            )

        errors = model.get("Errors") or []
        mine, others = self._split_sessions(model.get("ActiveSessions") or [])

        if not (set(errors) & {"UserAlreadyLoggedIn", "Too Many Current Sessions"}):
            raise AuthError(f"login rejected: {errors or 'unknown reason'}")
        if not mine:
            raise AuthError(
                "login blocked -- the license's active sessions all belong to "
                f"other users ({others} session(s)); not terminating them. "
                "Errors: " + str(errors)
            )
        if not self._s.lifesaver_terminate_own_session:
            raise AuthError(
                "login blocked by an existing session for this user; set "
                "LIFESAVER_TERMINATE_OWN_SESSION=true to auto-clear it "
                "(or run scripts/terminate_my_sessions.py)"
            )

        user_id = model.get("UserId")
        for sess in mine:
            self._terminate_session(sess["UniqueID"], user_id)
            log.warning(
                "terminated a stale session for %s (created %s)",
                self._s.lifesaver_username, sess.get("DateCreated"),
            )

        resp = self._post_login()
        if _LOGIN_PATH in resp.url:
            raise AuthError(
                "still blocked after terminating our own session(s) -- "
                + str(self._error_summary(resp.text))
            )
        self._authenticated = True
        log.info("authenticated to %s (after clearing a stale session)", self._s.lifesaver_base_url)

    def logout(self) -> None:
        """Best-effort: release the server-side session so the next run isn't blocked."""
        if not self._authenticated:
            return
        try:
            self._session.get(self._url(_LOGOFF_PATH), timeout=self._s.lifesaver_http_timeout)
        except requests.RequestException as e:  # pragma: no cover - best effort
            log.warning("logout call failed: %s", e)
        finally:
            self._authenticated = False

    def _post_login(self) -> requests.Response:
        resp = self._session.post(
            self._url(_LOGIN_PATH),
            data={
                "UserName": self._s.lifesaver_username,
                "Password": self._s.lifesaver_password,
            },
            timeout=self._s.lifesaver_http_timeout,
        )
        resp.raise_for_status()
        return resp

    def _terminate_session(self, unique_id: str, user_id) -> None:
        resp = self._session.post(
            self._url(_TERMINATE_PATH),
            data=json.dumps({"Session": unique_id, "UserId": user_id}),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self._s.lifesaver_http_timeout,
        )
        resp.raise_for_status()

    @staticmethod
    def _parse_login_model(html: str) -> dict | None:
        m = _LOGIN_MODEL_RE.search(html)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except ValueError:
            return None

    def _split_sessions(self, sessions: list[dict]) -> tuple[list[dict], int]:
        me = self._s.lifesaver_username.lower()
        mine = [
            s for s in sessions
            if (s.get("UserAccount") or {}).get("UserName", "").lower() == me
        ]
        return mine, len(sessions) - len(mine)

    def _error_summary(self, html: str):
        model = self._parse_login_model(html)
        return model.get("Errors") if model else "unknown"

    def _run_flow(self, report: ReportSpec, start: str, end: str) -> bytes:
        soup = self._get_report_page(report)
        html = self._submit_date_range(report, soup, start, end)
        report_session, control_id, rsproxy = self._extract_report_session(html)
        return self._export_csv(report_session, control_id, rsproxy)

    def _get_report_page(self, report: ReportSpec) -> BeautifulSoup:
        resp = self._session.get(
            self._url(report.page_path), timeout=self._s.lifesaver_http_timeout
        )
        resp.raise_for_status()
        if _LOGIN_PATH in resp.url:
            raise AuthError("redirected to login when fetching report page")
        return BeautifulSoup(resp.text, "html.parser")

    def _submit_date_range(
        self, report: ReportSpec, soup: BeautifulSoup, start: str, end: str
    ) -> str:
        payload = _extract_hidden_fields(soup)
        payload.update(_extract_ctl_fields(soup))
        payload[report.start_date_field] = start
        payload[report.end_date_field] = end
        # plain <input type=submit>: its own name=value is what fires it
        payload[report.view_button_field] = report.view_button_value

        resp = self._session.post(
            self._url(report.page_path),
            data=payload,
            timeout=self._s.lifesaver_http_timeout,
        )
        resp.raise_for_status()
        if _LOGIN_PATH in resp.url:
            raise AuthError("redirected to login on report postback")
        return resp.text

    def _extract_report_session(self, html: str) -> tuple[str, str, str]:
        session_match = re.search(r"ReportSession=([a-zA-Z0-9]+)", html)
        control_match = re.search(r"ControlID=([a-fA-F0-9]+)", html)
        rsproxy_match = re.search(r"RSProxy=([^&\"'\s]+)", html)

        if not session_match or not control_match:
            raise ReportError(
                "no ReportSession/ControlID in postback response -- the report "
                "likely did not render (e.g. an invalid date parameter)"
            )

        rsproxy = (
            requests.utils.unquote(rsproxy_match.group(1))
            if rsproxy_match
            else self._s.lifesaver_rsproxy or DEFAULT_RSPROXY
        )
        return session_match.group(1), control_match.group(1), rsproxy

    def _export_csv(self, report_session: str, control_id: str, rsproxy: str) -> bytes:
        params = {
            "ReportSession": report_session,
            "Culture": "1033",
            "CultureOverrides": "True",
            "UICulture": "1033",
            "UICultureOverrides": "True",
            "ReportStack": "1",
            "ControlID": control_id,
            "RSProxy": rsproxy,
            "OpType": "Export",
            "FileName": "LifeSaver Reports",
            "ContentDisposition": "OnlyHtmlInline",
            "Format": "CSV",
        }
        resp = self._session.get(
            self._url(_EXPORT_PATH), params=params, timeout=self._s.lifesaver_http_timeout
        )
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "text/html" in ctype and b"," not in resp.content[:200]:
            raise ReportError(
                f"export returned HTML, not CSV (Content-Type: {ctype!r}) -- "
                "session may need a SessionKeepAlive poll before export"
            )
        return resp.content

    # --- helpers --------------------------------------------------------

    def _url(self, path: str) -> str:
        return self._s.lifesaver_base_url.rstrip("/") + path


def _extract_hidden_fields(soup: BeautifulSoup) -> dict[str, str]:
    fields = {}
    for name in _HIDDEN_FIELDS:
        tag = soup.find("input", {"name": name})
        if tag and tag.get("value") is not None:
            fields[name] = tag["value"]
    return fields


def _extract_ctl_fields(soup: BeautifulSoup) -> dict[str, str]:
    """Echo back the page's ctl00$... form fields unchanged, like the browser."""
    fields = {}
    for tag in soup.find_all("input"):
        name = tag.get("name", "")
        if name.startswith("ctl00$") and tag.get("value") is not None:
            fields[name] = tag["value"]
    return fields
