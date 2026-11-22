import pytest

from qra.redact import CARD_PLACEHOLDER, IBAN_PLACEHOLDER, contains_payment_data, redact


@pytest.mark.parametrize("iban", [
    "NL91ABNA0417164300",
    "NL91 ABNA 0417 1643 00",
    "nl91 abna 0417 1643 00",
    "DE89 3704 0044 0532 0130 00",
    "GB29-NWBK-6016-1331-9268-19",
])
def test_ibans_are_removed_in_every_common_spelling(iban):
    r = redact(f"Graag terugbetalen op {iban}, bedankt")
    assert r.kinds == ["iban"] and iban not in r.text and IBAN_PLACEHOLDER in r.text


@pytest.mark.parametrize("card", [
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "4111111111111111",
    "5500 0000 0000 0004",
    "3782 822463 10005",
])
def test_card_numbers_are_removed(card):
    r = redact(f"Mijn kaart {card} is geweigerd")
    assert "card" in r.kinds and CARD_PLACEHOLDER in r.text
    assert "".join(ch for ch in card if ch.isdigit()) not in "".join(ch for ch in r.text if ch.isdigit())


@pytest.mark.parametrize("harmless", [
    "Bel mij op 06 12345678",
    "Het kost 25 x 6 euro = 150 euro",
    "Ticket nummer 20261005",
    "Mijn BTW-nummer is NL123456789B01",
    "Rooster van 22:00 tot 02:00 in week 41",
])
def test_ordinary_numbers_survive(harmless):
    r = redact(harmless)
    assert r.text == harmless and not r.found


def test_text_without_payment_data_is_unchanged_and_reports_nothing():
    assert not contains_payment_data("Hoe voeg ik een nieuwe medewerker toe?")


def test_redacting_twice_is_stable():
    once = redact("IBAN NL91 ABNA 0417 1643 00 en kaart 4111 1111 1111 1111")
    assert redact(once.text).text == once.text
