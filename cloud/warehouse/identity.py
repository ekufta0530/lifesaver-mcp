"""Customer identity resolution.

The source has no customer id -- only a free-text ``customer`` string with typos
(``"Miike Woodland"``), trailing spaces, and commercial contacts
(``"Nestle Purina Petcare - Deion Taylor"``). Every KPI is a per-customer cohort,
so a stable id matters.

v1 does **exact + normalised** matching only. Fuzzy matching (with a human review
queue) is a later, assisted layer -- over-merging two real people corrupts every
KPI silently, so it is not done automatically here (DESIGN.md §8).

Everything in this module is pure. ``store`` persists the result.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_EDGE_PUNCT = re.compile(r"^[^\w&]+|[^\w&]+$")
_COMMERCIAL_RE = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|corp|corp\.|co|co\.|ltd|ltd\.|company|"
    r"associates|group|studio|studios|gallery|galleries|museum|foundation|"
    r"university|college|school|church|hospital|bank|realty|properties)\b",
    re.IGNORECASE,
)
# "Company Name - Contact Person": a hyphen with spaces on both sides.
_CONTACT_SPLIT = re.compile(r"\s+-\s+")


@dataclass(frozen=True, slots=True)
class NameParts:
    raw: str
    match_key: str  # casefolded identity key -- what two spellings must share to merge
    display: str  # tidied, original casing
    is_commercial: bool
    contact: str | None


@dataclass(frozen=True, slots=True)
class SeenName:
    customer_raw: str
    first_seen: str  # ISO date/datetime -- earliest we have seen this exact string


@dataclass(frozen=True, slots=True)
class CustomerRow:
    customer_id: str
    display_name: str
    normalized_name: str
    is_commercial: bool
    first_seen_at: str


@dataclass(frozen=True, slots=True)
class AliasRow:
    customer_raw: str
    normalized_name: str
    customer_id: str
    match_method: str  # exact | normalized | fuzzy | manual
    match_score: float | None
    needs_review: bool


@dataclass(slots=True)
class ResolutionResult:
    new_customers: list[CustomerRow]
    new_aliases: list[AliasRow]


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def normalize_name(raw: str, *, split_commercial_contact: bool = False) -> NameParts:
    collapsed = _collapse(raw)
    contact: str | None = None
    identity_part = collapsed

    parts = _CONTACT_SPLIT.split(collapsed, maxsplit=1)
    has_contact = len(parts) == 2 and all(p.strip() for p in parts)
    if has_contact:
        contact = parts[1].strip()
        if split_commercial_contact:
            identity_part = parts[0].strip()

    is_commercial = bool(_COMMERCIAL_RE.search(collapsed)) or has_contact

    key = _EDGE_PUNCT.sub("", identity_part.casefold())
    key = _collapse(key)
    return NameParts(
        raw=raw,
        match_key=key,
        display=collapsed,
        is_commercial=is_commercial,
        contact=contact,
    )


def customer_id_for(match_key: str) -> str:
    """Deterministic id from the match key: same key -> same id, no counter."""
    return "c" + hashlib.sha1(match_key.encode()).hexdigest()[:11]


def resolve(
    seen: Iterable[SeenName],
    known_aliases: dict[str, AliasRow],
    known_customers: dict[str, CustomerRow],
    *,
    split_commercial_contact: bool = False,
) -> ResolutionResult:
    """Map every not-yet-resolved raw name to a customer id.

    Already-resolved raw strings are left untouched (resolution is deterministic;
    re-running never churns ids and never overrides a ``manual`` alias).
    """
    # normalized_name -> customer_id, seeded from existing customers and grown as
    # this batch creates new ones. This is what "normalized match" checks against,
    # so it works even for customers whose id did not come from customer_id_for
    # (e.g. a manual merge).
    by_norm: dict[str, str] = {
        c.normalized_name: c.customer_id for c in known_customers.values()
    }
    display_by_id: dict[str, str] = {
        c.customer_id: c.display_name for c in known_customers.values()
    }

    new_customers: list[CustomerRow] = []
    new_aliases: list[AliasRow] = []

    for item in seen:
        raw = item.customer_raw
        if raw in known_aliases:
            continue

        parts = normalize_name(raw, split_commercial_contact=split_commercial_contact)
        # Guard against an all-punctuation / empty name collapsing to "".
        key = parts.match_key or parts.display.casefold() or raw.casefold()

        cid = by_norm.get(key)
        if cid is None:
            cid = customer_id_for(key)
            new_customers.append(
                CustomerRow(cid, parts.display, key, parts.is_commercial, item.first_seen)
            )
            by_norm[key] = cid
            display_by_id[cid] = parts.display
            method = "exact"
        else:
            method = "exact" if display_by_id.get(cid) == raw else "normalized"

        new_aliases.append(AliasRow(raw, key, cid, method, None, False))

    return ResolutionResult(new_customers=new_customers, new_aliases=new_aliases)
