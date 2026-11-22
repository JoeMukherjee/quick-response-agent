from qra.safety.checks import contact_ok, numbers_ok, paths_ok, payment_ok, run_checks, urls_ok
from qra.core.faq import load_faq

FAQ = load_faq()
F02, F05, F06, F11 = FAQ["F02"], FAQ["F05"], FAQ["F06"], FAQ["F11"]


def test_a_product_of_a_ticket_number_and_a_price_is_allowed():
    assert numbers_ok("Dat is 25 x EUR 6 = EUR 150 per maand, exclusief btw.", [F05], "Wat kost Plus voor 25 medewerkers?") == []


def test_a_sum_or_product_of_two_ticket_numbers_is_allowed_too():
    assert numbers_ok("12 x 4 = 48", [F05], "Basis for 12 employees") == []


def test_an_invented_price_is_rejected():
    fails = numbers_ok("Plus kost EUR 9 per medewerker.", [F05], "Wat kost Plus?")
    assert fails and "9" in fails[0]


def test_numbers_that_are_in_the_entry_are_allowed():
    assert numbers_ok("De link is 60 minuten geldig.", [FAQ["F01"]], "wachtwoord vergeten") == []


def test_an_invented_link_is_rejected_and_a_real_one_is_not():
    assert urls_ok("Kijk op status.roosterly.nl", [F11]) == []
    assert urls_ok("Kijk op status.example.com voor meer", [F11])


def test_invented_menu_steps_are_rejected():
    ok = "Ga naar Team → Medewerker toevoegen en klik op Uitnodigen."
    assert paths_ok(ok, [F02], "nieuwe medewerker") == []
    assert paths_ok("Ga naar Team → Personeel → Medewerker importeren.", [F02], "nieuwe medewerker")


def test_quoted_labels_must_come_from_the_entries():
    assert paths_ok('Klik op "Uitnodigen".', [F02], "") == []
    assert paths_ok('Klik op "Direct versturen".', [F02], "")


def test_contact_details_not_in_the_help_centre_are_rejected():
    assert contact_ok("Mail ons via support@roosterly.nl", [F02], "") != []
    assert contact_ok("Bel 020 123 4567", [F02], "") != []
    assert contact_ok("Uw eigen adres is jan@cafe.nl", [F02], "mijn adres jan@cafe.nl") == []


def test_payment_data_in_a_reply_is_rejected():
    assert payment_ok("Wij storten terug op NL91 ABNA 0417 1643 00") != []
    assert payment_ok("Fijne dag!") == []


def test_a_clean_dutch_reply_passes_every_check():
    reply = ("Hoi Anouk, ga naar Team → Medewerker toevoegen, vul naam, e-mailadres en contracturen in en klik op "
             "Uitnodigen. De medewerker krijgt een e-mail om de app te installeren.")
    assert run_checks(reply, [F02], "Hoe voeg ik een nieuwe medewerker toe aan ons rooster?") == []
