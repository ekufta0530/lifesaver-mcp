"""Store-independent KPI core: visits, the five retention KPIs, and the SQLite
tables the dashboard reads.

Every data source -- the cloud report (``cloud.warehouse``) and the desktop
SQLite extract (``sqlite_extract``) -- turns its rows into ``LineForVisit`` and
feeds the same pure transforms here, so the KPI math is identical per store.
See ``dashboard/DESIGN.md``. Layers:

  visits           -- the "a purchase == a visit" grain
  kpi_monthly      -- one row per (metric, month, calc_version), frozen when final
"""
