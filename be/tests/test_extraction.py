"""Deterministic extraction from invoice PDFs and email bodies."""

from __future__ import annotations

from datetime import date

import pytest

from app.enums import ExtractionMethod, ExtractionStatus
from app.services.extraction import (
    extract_invoice_facts,
    extract_payment_signals,
    find_amount,
    find_invoice_number,
    normalize_invoice_number,
    parse_amount_to_paise,
    parse_date,
    strip_quoted_reply,
)
from app.services.pdf import extract_pdf_text, looks_like_pdf, sha256_bytes


class TestAmountParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("40,000.00", 4_000_000),
            ("40000", 4_000_000),
            ("5,250.50", 525_050),
            ("0.99", 99),
            ("1,00,000", 10_000_000),  # Indian digit grouping
        ],
    )
    def test_paise_conversion_is_exact(self, raw, expected):
        assert parse_amount_to_paise(raw) == expected

    @pytest.mark.parametrize("raw", ["", "abc", "-500"])
    def test_rejects_unusable_amounts(self, raw):
        assert parse_amount_to_paise(raw) is None

    def test_labelled_total_beats_line_items(self):
        text = (
            "Design retainer  Rs. 10,000\n"
            "Hosting          Rs. 2,500\n"
            "Subtotal: Rs. 12,500\n"
            "Total Amount Due: Rs. 14,750\n"
        )
        paise, currency, evidence = find_amount(text, "pdf")
        assert paise == 1_475_000
        assert currency == "INR"
        assert "14,750" in evidence.snippet

    def test_currency_is_detected(self):
        paise, currency, _ = find_amount("Total: $1,200.00", "pdf")
        assert (paise, currency) == (120_000, "USD")


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("20/07/2026", date(2026, 7, 20)),
            ("2026-07-20", date(2026, 7, 20)),
            ("20 Jul 2026", date(2026, 7, 20)),
            ("20th July, 2026", date(2026, 7, 20)),
            ("July 20 2026", date(2026, 7, 20)),
        ],
    )
    def test_common_formats(self, raw, expected):
        assert parse_date(raw) == expected

    def test_rejects_nonsense(self):
        assert parse_date("sometime next week") is None


class TestInvoiceNumber:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Invoice Number: INV-2026-0114", "INV-2026-0114"),
            ("Tax Invoice No. TI/26-27/0042", "TI/26-27/0042"),
            ("inv #A-9912", "A-9912"),
        ],
    )
    def test_extracts_number(self, text, expected):
        number, evidence = find_invoice_number(text, "pdf")
        assert number == expected
        assert evidence is not None

    def test_prose_is_not_an_invoice_number(self):
        """"the invoice for June" must not yield "FOR" as an invoice number."""
        assert find_invoice_number("Please pay the invoice for June", "pdf")[0] is None

    def test_number_requires_a_digit(self):
        assert normalize_invoice_number("ATTACHED") is None
        assert normalize_invoice_number("inv-9") == "INV-9"


class TestPdfExtraction:
    def test_text_layer_is_preferred(self, acme_invoice_pdf):
        result = extract_pdf_text(acme_invoice_pdf, mime_type="application/pdf")
        assert result.status == ExtractionStatus.TEXT_EXTRACTED
        assert result.method == ExtractionMethod.PYPDF
        assert result.succeeded
        assert "INV-2026-0114" in result.text

    def test_scanned_pdf_falls_back_to_ocr(self, scanned_invoice_pdf):
        """No text layer -> OCR is attempted; without a backend it reports, not crashes."""
        result = extract_pdf_text(scanned_invoice_pdf, mime_type="application/pdf")
        assert result.status in (
            ExtractionStatus.OCR_EXTRACTED,
            ExtractionStatus.OCR_UNAVAILABLE,
        )
        if result.status == ExtractionStatus.OCR_UNAVAILABLE:
            assert "OCR backend not installed" in (result.error or "")

    def test_ocr_disabled_does_not_attempt_ocr(self, scanned_invoice_pdf):
        result = extract_pdf_text(
            scanned_invoice_pdf, mime_type="application/pdf", enable_ocr=False
        )
        assert result.status == ExtractionStatus.SKIPPED
        assert "OCR disabled" in (result.error or "")

    def test_non_pdf_is_skipped(self):
        result = extract_pdf_text(b"just text", mime_type="text/plain")
        assert result.status == ExtractionStatus.SKIPPED

    def test_corrupt_pdf_fails_cleanly(self):
        result = extract_pdf_text(b"%PDF-1.4 truncated garbage", mime_type="application/pdf")
        assert result.status in (ExtractionStatus.FAILED, ExtractionStatus.SKIPPED)
        assert not result.succeeded

    def test_content_hash_is_stable(self, acme_invoice_pdf):
        assert sha256_bytes(acme_invoice_pdf) == sha256_bytes(acme_invoice_pdf)
        assert looks_like_pdf(acme_invoice_pdf)


class TestInvoiceFacts:
    def test_full_facts_from_a_real_pdf(self, acme_invoice_pdf):
        text = extract_pdf_text(acme_invoice_pdf, mime_type="application/pdf").text
        facts = extract_invoice_facts(
            text, source="pdf", customer_email="ap@acmetraders.in", customer_name="Acme"
        )
        assert facts.invoice_number == "INV-2026-0114"
        assert facts.amount_paise == 4_000_000
        assert facts.issued_date == date(2026, 7, 5)
        assert facts.due_date == date(2026, 7, 20)
        assert not facts.due_date_inferred
        assert facts.missing_fields == []
        assert facts.is_usable
        assert facts.confidence >= 0.9

    def test_evidence_locators_point_back_into_the_source(self, nova_invoice_pdf):
        text = extract_pdf_text(nova_invoice_pdf, mime_type="application/pdf").text
        facts = extract_invoice_facts(text, source="pdf", customer_email="a@nova.in")
        assert facts.evidence
        for item in facts.evidence:
            assert item.locator.startswith("pdf:chars=")
            start, end = item.locator.split("=")[1].split("-")
            assert text[int(start) : int(end)]  # the offsets address real text

    def test_due_date_inferred_from_payment_terms(self):
        facts = extract_invoice_facts(
            "Invoice No: INV-77\nInvoice Date: 01 Jun 2026\nTotal: Rs. 1,000\n"
            "Payment Terms: Net 30",
            source="pdf",
            customer_email="a@b.in",
        )
        assert facts.due_date == date(2026, 7, 1)
        assert facts.due_date_inferred

    def test_missing_facts_are_reported(self):
        facts = extract_invoice_facts("Thanks for your business.", source="email_body")
        assert not facts.is_usable
        assert "amount" in facts.missing_fields

    def test_invoice_in_an_email_body(self):
        facts = extract_invoice_facts(
            "Hi, sharing invoice INV-2026-0200 for Rs. 12,000. Due date: 15 Aug 2026.",
            source="email_body",
            customer_email="ap@acmetraders.in",
        )
        assert facts.invoice_number == "INV-2026-0200"
        assert facts.amount_paise == 1_200_000
        assert facts.due_date == date(2026, 8, 15)


class TestPaymentSignals:
    @pytest.mark.parametrize(
        "body",
        [
            "We have already paid this invoice.",
            "Payment has been released yesterday.",
            "We transferred the amount on the 3rd.",
            "Bhugtan kar diya hai.",
        ],
    )
    def test_detects_payment_claims(self, body):
        assert extract_payment_signals(body).has_payment_claim

    @pytest.mark.parametrize(
        "body",
        [
            "This invoice is wrong.",
            "We were overcharged on this bill.",
            "Invoice galat hai.",
            "We never ordered this service.",
        ],
    )
    def test_detects_disputes(self, body):
        assert extract_payment_signals(body).has_dispute

    def test_extracts_utr_and_amount_from_a_claim(self):
        signals = extract_payment_signals(
            "We paid Rs. 40,000 on 3 Aug. UTR: HDFC2026X8817."
        )
        assert signals.has_payment_claim
        assert signals.utr_reference == "HDFC2026X8817"
        assert signals.paid_amount_paise == 4_000_000

    def test_neutral_mail_yields_no_signals(self):
        signals = extract_payment_signals("Thanks, received the files. Will review Monday.")
        assert not signals.has_payment_claim
        assert not signals.has_dispute


class TestQuotedReplyStripping:
    def test_removes_quoted_history(self):
        body = (
            "Already paid, please check.\n\n"
            "On Mon, 3 Aug 2026 at 10:00, Nina <owner@northwind.in> wrote:\n"
            "> Gentle reminder about invoice INV-1 for Rs. 40,000\n"
        )
        assert strip_quoted_reply(body) == "Already paid, please check."

    def test_prevents_stale_context_from_leaking_into_classification(self):
        """The old reminder's dispute wording must not be read as a new dispute."""
        body = (
            "Sure, we'll pay next week.\n\n"
            "--- Original Message ---\n"
            "You said the invoice was wrong and overcharged.\n"
        )
        assert not extract_payment_signals(strip_quoted_reply(body)).has_dispute
