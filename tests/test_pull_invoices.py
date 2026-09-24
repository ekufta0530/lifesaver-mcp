"""Offline tests for scripts/pull_invoices.py. No network, no credentials."""

import pytest

import pull_invoices as pi


def test_load_invoice_ids_from_csv_dedupes_and_preserves_order(tmp_path):
    csv_path = tmp_path / "workorderlist.csv"
    csv_path.write_text(
        "invoiceNumber,customer\n"
        "584,Alice\n"
        "591,Bob\n"
        "584,Alice\n"  # duplicate line item on the same invoice
        ",Nobody\n"    # blank invoice number
        "602,Carol\n"
    )
    ids = pi.load_invoice_ids_from_csv(str(csv_path))
    assert ids == ["584", "591", "602"]


def test_load_invoice_ids_from_csv_missing_column_raises(tmp_path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("foo,bar\n1,2\n")
    with pytest.raises(ValueError, match="invoiceNumber"):
        pi.load_invoice_ids_from_csv(str(csv_path))


def test_bulk_pull_invoices_writes_each_pdf_and_survives_one_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pi.time, "sleep", lambda *_: None)

    def fake_pull_invoice(session, invoice_id, store_id=pi.lsp.DEFAULT_STORE_ID):
        if invoice_id == "bad":
            raise RuntimeError("boom")
        return f"pdf-for-{invoice_id}".encode()

    monkeypatch.setattr(pi.lsp, "pull_invoice", fake_pull_invoice)

    out_dir = tmp_path / "invoices"
    results = pi.bulk_pull_invoices(
        session=object(),
        invoice_ids=["584", "bad", "591"],
        out_dir=str(out_dir),
        delay=0,
    )

    assert results["ok"] == ["584", "591"]
    assert len(results["failed"]) == 1
    assert results["failed"][0][0] == "bad"

    assert (out_dir / "invoice_584.pdf").read_bytes() == b"pdf-for-584"
    assert (out_dir / "invoice_591.pdf").read_bytes() == b"pdf-for-591"
    assert not (out_dir / "invoice_bad.pdf").exists()
