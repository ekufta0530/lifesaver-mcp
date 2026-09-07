from warehouse.identity import (
    AliasRow,
    CustomerRow,
    SeenName,
    customer_id_for,
    normalize_name,
    resolve,
)


def test_normalize_collapses_whitespace_and_case():
    a = normalize_name("  John   Smith ")
    b = normalize_name("john smith")
    assert a.match_key == b.match_key == "john smith"
    assert a.display == "John Smith"  # display keeps original casing, trimmed


def test_normalize_strips_edge_punctuation_keeps_internal():
    assert normalize_name("Monet's Garden .").match_key == "monet's garden"
    assert normalize_name("Head&Heart").match_key == "head&heart"


def test_commercial_contact_detected_not_split_by_default():
    p = normalize_name("Nestle Purina Petcare - Deion Taylor")
    assert p.is_commercial is True
    assert p.contact == "Deion Taylor"
    assert p.match_key == "nestle purina petcare - deion taylor"


def test_commercial_contact_split_when_configured():
    p = normalize_name(
        "Nestle Purina Petcare - Deion Taylor", split_commercial_contact=True
    )
    assert p.match_key == "nestle purina petcare"


def test_company_keyword_marks_commercial():
    assert normalize_name("Acme Framing LLC").is_commercial is True
    assert normalize_name("Jane Doe").is_commercial is False


def test_customer_id_is_deterministic():
    assert customer_id_for("john smith") == customer_id_for("john smith")
    assert customer_id_for("john smith") != customer_id_for("jane smith")


def test_resolve_merges_spelling_variants_via_normalized_key():
    seen = [
        SeenName("John Smith", "2024-01-01"),
        SeenName("  john  smith ", "2024-02-01"),
        SeenName("Jane Doe", "2024-03-01"),
    ]
    result = resolve(seen, {}, {})

    assert len(result.new_customers) == 2  # John (2 spellings) + Jane
    ids = {a.customer_raw: a.customer_id for a in result.new_aliases}
    assert ids["John Smith"] == ids["  john  smith "]
    assert ids["Jane Doe"] != ids["John Smith"]

    methods = {a.customer_raw: a.match_method for a in result.new_aliases}
    assert methods["John Smith"] == "exact"
    assert methods["  john  smith "] == "normalized"


def test_resolve_skips_already_known_and_manual_aliases():
    known_alias = AliasRow("Bob", "bob", "cbob", "manual", None, False)
    known_cust = CustomerRow("cbob", "Bob", "bob", False, "2024-01-01")
    result = resolve(
        [SeenName("Bob", "2024-05-01"), SeenName("bob", "2024-05-02")],
        {"Bob": known_alias},
        {"cbob": known_cust},
    )
    # "Bob" already resolved; "bob" normalizes onto the existing customer
    assert [a.customer_raw for a in result.new_aliases] == ["bob"]
    assert result.new_aliases[0].customer_id == "cbob"
    assert result.new_aliases[0].match_method == "normalized"
    assert result.new_customers == []
