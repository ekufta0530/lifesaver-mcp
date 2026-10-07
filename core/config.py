"""KPI settings shared by every store, sourced from environment variables.

Reuses the ``.env`` convention of ``cloud.lifesaver.config``. Nothing here is
required -- every value has a working default.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Statuses that are NOT a real sale and must be dropped before a line item can
# count toward revenue or a visit. Lower-cased compare. The full currentStatus
# enum is still unconfirmed (DESIGN.md §12 #2) -- this is the conservative set:
# exclude only what we know is not a sale, keep everything else.
DEFAULT_NON_SALE_STATUSES = ("void", "cancelled", "canceled", "quote", "estimate")


class KpiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Comma-separated override for DEFAULT_NON_SALE_STATUSES.
    warehouse_non_sale_statuses: str = ",".join(DEFAULT_NON_SALE_STATUSES)

    # Cohort width for the first-to-second-purchase and median-days KPIs. 12 =>
    # blend the 12 monthly cohorts that matured a year before the report month
    # (bigger sample, less noise). 1 => a single maturing month.
    warehouse_cohort_window_months: int = 12

    @property
    def non_sale_statuses(self) -> frozenset[str]:
        return frozenset(
            s.strip().lower() for s in self.warehouse_non_sale_statuses.split(",") if s.strip()
        )


@lru_cache
def get_kpi_settings() -> KpiSettings:
    return KpiSettings()
