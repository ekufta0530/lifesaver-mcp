"""Phase 3 backend: accumulate Work Order List history into a local warehouse
and compute the retention KPIs the dashboard reads.

See ``dashboard/DESIGN.md``. Layers:

  raw pulls        -- every fetch, stored verbatim (rebuild everything from here)
  line_items       -- deduped current truth, upserted on a stable natural key
  customers        -- resolved identities (exact + normalised match; fuzzy later)
  visits           -- the "a purchase == a visit" grain
  kpi_monthly      -- one row per (metric, month, calc_version), frozen when final
"""
