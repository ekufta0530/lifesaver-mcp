"""Configuration, sourced from environment variables (never hardcoded).

Reads a local `.env` file if present (handy for dev); real deployments set the
vars directly. Required: LIFESAVER_USERNAME, LIFESAVER_PASSWORD.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Confirmed-live default for this org's ReportViewer -> SSRS proxy. Tied to the
# customer's on-prem reporting box, not to a session, so it is safe as a
# constant; the client still prefers a value scraped fresh from the postback.
DEFAULT_RSPROXY = "http://lifesaver-sql1.corp.lifesaversoft.com/reportserver"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    lifesaver_username: str
    lifesaver_password: str
    lifesaver_base_url: str = "https://lsscloud.com"
    lifesaver_rsproxy: str = DEFAULT_RSPROXY

    # seconds; SSRS export on a wide date range can be slow
    lifesaver_http_timeout: float = 120.0

    # LifeSaver allows one active session per user. If a prior run didn't log
    # out, login is blocked with "UserAlreadyLoggedIn". When true, the client
    # terminates its OWN stale session (never another user's) and retries.
    lifesaver_terminate_own_session: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
