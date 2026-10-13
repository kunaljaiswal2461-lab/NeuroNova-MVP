"""SPEC-07 L1 items 1 and 2: the personal-data heuristics."""
from __future__ import annotations

import pytest

from app.services.sample_values.pii import (
    any_value_is_pii,
    column_name_is_pii,
    name_tokens,
    table_is_person_like,
    value_is_pii,
)


@pytest.mark.parametrize(
    "name,tokens",
    [
        ("customerEmail", ["customer", "email"]),
        ("customer_email", ["customer", "email"]),
        ("CUSTOMER-EMAIL", ["customer", "email"]),
        ("E-mail", ["e", "mail"]),
        ("HTTPHost", ["http", "host"]),
        ("zip2", ["zip2"]),
        ("Event Type", ["event", "type"]),
        ("phone.number", ["phone", "number"]),
        ("", []),
        ("電話", []),
    ],
)
def test_tokenizer(name, tokens):
    assert name_tokens(name) == tokens


@pytest.mark.parametrize(
    "column",
    [
        "email", "Email", "customerEmail", "user_email", "mail", "phone", "mobile_no", "tel", "fax", "address",
        "street", "billing_addr", "zip", "zipcode", "postcode", "postal_code", "pincode", "pin", "ssn", "aadhaar",
        "aadhar_no", "pan", "passport", "dob", "birth_date", "birthday", "password", "passwd", "pwd", "api_token",
        "client_secret", "salt", "hash", "ip", "ip_address", "ipaddr", "iban", "ifsc", "card_no", "cvv", "account",
        "acct_no", "username", "login", "loginName",
    ],
)
def test_pii_names_are_skipped_in_any_table(column):
    assert column_name_is_pii("orders", column)


@pytest.mark.parametrize(
    "table,column",
    [
        ("regions", "name"), ("products", "name"), ("orders", "status"), ("orders", "channel"),
        ("products", "category"), ("regions", "zone"), ("Order Events", "Event Type"), ("refunds", "reason"),
        ("customers", "segment"), ("customers", "city"), ("page_views", "path"), ("orders", "customer_name"),
        ("products", "full_name"), ("customers", "display_name"), ("orders", "pipeline"), ("orders", "spinning"),
        ("shipments", "carrier"), ("orders", "capital"), ("t", "emailer_version"), ("t", "mailbox_count_total"),
    ],
)
def test_ordinary_names_are_not_skipped(table, column):
    # ``pipeline`` contains "pin"? No: tokens, not substrings.  ``emailer`` is one token that is not "email".
    assert not column_name_is_pii(table, column)


@pytest.mark.parametrize(
    "table,column",
    [
        ("customers", "name"), ("customers", "full_name"), ("customers", "first_name"), ("customers", "lastName"),
        ("customers", "surname"), ("users", "given_name"), ("employees", "family_name"), ("people", "middle_name"),
        ("patient", "name"), ("student_records", "name"), ("contacts", "name"), ("staff", "Name"),
        ("member_profiles", "full_name"), ("clients", "name"),
    ],
)
def test_name_columns_of_person_tables_are_skipped(table, column):
    assert column_name_is_pii(table, column)


def test_person_table_detection():
    assert table_is_person_like("customers")
    assert table_is_person_like("app_user")
    assert not table_is_person_like("regions")
    assert not table_is_person_like("products")
    assert not table_is_person_like("order_items")


@pytest.mark.parametrize(
    "value,kind",
    [
        ("a@b.co", "email"),
        ("first.last+tag@example.com", "email"),
        ("contact: bob@corp.example", "email"),
        ("＠".join(["a", "b.co"]), "email"),                              # full-width @ folds to @ under NFKC
        ("123e4567-e89b-12d3-a456-426614174000", "uuid"),
        ("ID 123E4567-E89B-12D3-A456-426614174000", "uuid"),
        ("192.168.0.1", "ipv4"),
        ("host 10.0.0.255 down", "ipv4"),
        ("+91 98765-43210", "phone"),
        ("(555) 123-4567", "phone"),
        ("555.123.4567", "phone"),
        ("१२३४५६७८९०", "phone"),                                            # Devanagari digits
        ("+९१ ९८७६५ ४३२१०", "phone"),
        ("９８７６５４３２１０", "phone"),                                   # full-width digits
        ("order 1234567890", "digit_run"),
        ("4111 1111 1111 1111", "phone"),
        ("ref4111111111111111", "digit_run"),
        ("999.999.999.999", "phone"),            # not an address, but 12 digits once the dots go: skipped on purpose
        ("v1.2.3.4.5", "ipv4"),                  # a version string looks like an address: a known false positive
    ],
)
def test_pii_value_patterns_match(value, kind):
    assert value_is_pii(value) == kind


@pytest.mark.parametrize(
    "value",
    [
        "PAID", "pending", "Cancelled", "enterprise ", "Retail", "SMB", "", " ", "@", "a@b", "a @ b.c", "a@ b.co",
        "1.2.3", "256.1.1.1", "order 12345678", "12345678a", "1234567", "+1 234",
        "2024", "Q3", "abc-def-ghi", "123e4567-e89b-12d3-a456", "Duplicate charge.",
    ],
)
def test_ordinary_values_do_not_match(value):
    assert value_is_pii(value) is None


def test_one_matching_value_is_enough():
    assert any_value_is_pii(["PAID", "pending", "x@y.example"])
    assert not any_value_is_pii(["PAID", "pending"])
    assert not any_value_is_pii([])


@pytest.mark.parametrize(
    "value",
    [
        "name​@​example.com",     # zero-width characters around @: still an e-mail shape
        "x@y.com‮",                   # bidi override after a real address
    ],
)
def test_format_characters_do_not_hide_an_email(value):
    # The pattern is [^@\s]+@[^@\s]+\.[^@\s]+ : zero-width and bidi characters are not \s, so they cannot split it.
    assert value_is_pii(value) == "email"


def test_look_alike_at_signs_are_not_silently_accepted():
    # U+FE6B SMALL COMMERCIAL AT and U+FF20 FULLWIDTH COMMERCIAL AT fold to "@" under NFKC.
    assert value_is_pii("a﹫b.co") == "email"
    assert value_is_pii("a＠b.co") == "email"
