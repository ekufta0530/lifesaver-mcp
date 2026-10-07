"""Phase 3 ingest for the main store: accumulate Work Order List history into
a local warehouse, then hand it to ``core`` for visits and KPIs.

See ``dashboard/DESIGN.md``. Layers added on top of ``core.store``:

  raw pulls        -- every fetch, stored verbatim (rebuild everything from here)
  line_items       -- deduped current truth, upserted on a stable natural key
  customers        -- resolved identities (exact + normalised match; fuzzy later)
"""
