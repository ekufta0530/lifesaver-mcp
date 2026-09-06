import datetime

import pytest

from conftest import load_fixture_bytes
from lifesaver.parser import ParseError, parse_work_order_csv


def test_parses_real_export_fixture():
    rows = parse_work_order_csv(load_fixture_bytes("export_sample.csv"))
    assert len(rows) == 51

    first = rows[0]
    assert first.invoice_number == 513
    assert first.work_order_number == "514.2"  # kept as string, not 514.2 float
    assert first.customer == "Nestle Purina Petcare - Deion Taylor"
    assert first.retail == 371.72  # "$371.72" -> float
    assert first.discount == 30.0
    assert first.order_date == datetime.date(2025, 8, 1)
    assert first.date_due == datetime.date(2025, 8, 22)


def test_handles_utf8_bom():
    raw = load_fixture_bytes("export_sample.csv")
    assert raw[:3] == b"\xef\xbb\xbf"  # fixture really has the BOM
    rows = parse_work_order_csv(raw)
    # if the BOM leaked into the header, invoice_number would be None for every row
    assert rows[0].invoice_number == 513


def test_money_parsing_edge_cases():
    csv = (
        "invoiceNumber,workOrderNumber,customer,lineItemNumber,description,"
        "currentStatus,retail,discount,orderDate,dateDue\r\n"
        "1,2.1,ACME,1,Thing,OnOrder,\"$1,234.50\",$0.00,8/1/2025,8/2/2025\r\n"
        "2,2.2,ACME,2,Other,Done,,,8/1/2025,\r\n"
    ).encode("utf-8-sig")
    rows = parse_work_order_csv(csv)
    assert rows[0].retail == 1234.50
    assert rows[1].retail is None
    assert rows[1].discount is None
    assert rows[1].date_due is None


def test_rejects_unexpected_header():
    with pytest.raises(ParseError):
        parse_work_order_csv(b"foo,bar\r\n1,2\r\n")
