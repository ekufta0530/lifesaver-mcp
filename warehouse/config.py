"""Warehouse configuration, sourced from environment variables.

Reuses the same ``.env`` convention as ``lifesaver.config``. Nothing here is
required -- every value has a working default -- so the pipeline runs locally with
no setup beyond the LifeSaver credentials the sync step needs.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Statuses that are NOT a real sale and must be dropped before a line item can
# count toward revenue or a visit. Lower-cased compare. The full currentStatus
# enum is still unconfirmed (DESIGN.md §12 #2) -- this is the conservative set:
# exclude only what we know is not a sale, keep everything else.
DEFAULT_NON_SALE_STATUSES = ("void", "cancelled", "canceled", "quote", "estimate")


class WarehouseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite file that holds every layer except the raw-pull blobs' optional
    # on-disk mirror. Tiny (tens of thousands of rows, ever).
    warehouse_db_path: str = "warehouse.db"

    # The immutable raw layer: every pull is written here as
    # <start>_<end>.<pull_id>.csv.gz before anything is parsed. Sync this
    # directory to GCS (versioned bucket) for the off-box backup -- everything
    # else is rebuildable from it.
    warehouse_raw_dir: str = "warehouse_raw"

    # Trailing window the routine `sync` re-pulls, to catch status/price changes
    # on still-open orders. 120 days is a guess at "how long an order stays
    # mutable" -- tune once real churn has been observed (DESIGN.md §9).
    warehouse_sync_window_days: int = 120

    # Politeness delay between month pulls during a backfill. The SSRS backend is
    # slow and there is one shared session.
    warehouse_backfill_delay_seconds: float = 3.0

    # How far back the source retains data. Backfill's default floor.
    warehouse_retention_months: int = 36

    # Comma-separated override for DEFAULT_NON_SALE_STATUSES.
    warehouse_non_sale_statuses: str = ",".join(DEFAULT_NON_SALE_STATUSES)

    # Treat "Company - Contact" as the company (True) or as distinct customers
    # per contact (False). Unconfirmed with the user (DESIGN.md §12 #4); the
    # conservative default is not to merge.
    warehouse_split_commercial_contact: bool = False

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
def get_warehouse_settings() -> WarehouseSettings:
    return WarehouseSettings()
