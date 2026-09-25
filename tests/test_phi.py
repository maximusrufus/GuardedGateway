from guardedgateway import phi


def test_redacts_ssn():
    text = "patient ssn is 123-45-6789 for billing"
    redacted, count, counts = phi.redact_phi(text)
    assert "123-45-6789" not in redacted
    assert phi.PHI_PLACEHOLDER in redacted
    assert count == 1
    assert counts["ssn"] == 1


def test_redacts_mrn_and_dob():
    text = "MRN: A1234567, DOB: 01/02/1980"
    redacted, count, counts = phi.redact_phi(text)
    assert "A1234567" not in redacted
    assert "01/02/1980" not in redacted
    assert count >= 2


def test_redacts_email_and_phone():
    text = "reach me at jane.doe@example.com or (555) 123-4567"
    redacted, count, counts = phi.redact_phi(text)
    assert "jane.doe@example.com" not in redacted
    assert "555" not in redacted or "123-4567" not in redacted
    assert count >= 2


def test_clean_text_untouched():
    text = "This is a general billing question about denial codes."
    redacted, count, counts = phi.redact_phi(text)
    assert redacted == text
    assert count == 0
    assert counts == {}


def test_scrub_dict_redacts_sensitive_keys():
    d = {"ssn": "123-45-6789", "note": "call 555-123-4567 about it"}
    scrubbed = phi.scrub_dict(d)
    assert scrubbed["ssn"] == phi.PHI_PLACEHOLDER
    assert "555" not in scrubbed["note"] or "123-4567" not in scrubbed["note"]


def test_scrub_dict_recurses_nested():
    d = {"outer": {"ssn": "123-45-6789"}}
    scrubbed = phi.scrub_dict(d)
    assert scrubbed["outer"]["ssn"] == phi.PHI_PLACEHOLDER
