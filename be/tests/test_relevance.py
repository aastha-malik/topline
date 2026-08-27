"""Relevance scoring: what Topline reads, and - just as importantly - what it does not."""

from __future__ import annotations

import pytest

from app.services.relevance import (
    MessageMetadata,
    build_backfill_query,
    score_message,
)

THRESHOLD = 40


def meta(**kwargs) -> MessageMetadata:
    kwargs.setdefault("gmail_message_id", "m1")
    return MessageMetadata(**kwargs)


class TestFinanceRelevant:
    def test_invoice_email_with_pdf_scores_high(self):
        result = score_message(
            meta(
                from_email="ap@acmetraders.in",
                subject="Tax Invoice INV-2026-0114 - Rs. 40,000",
                snippet="Please find the attached invoice. Amount due 20 Jul.",
                attachment_filenames=["invoice_INV-2026-0114.pdf"],
                attachment_mime_types=["application/pdf"],
            ),
            threshold=THRESHOLD,
        )
        assert result.is_relevant
        assert result.score >= 80
        assert any(r.rule == "invoice_like_filename" for r in result.reasons)

    def test_payment_claim_is_always_relevant(self):
        """A claim must reach the decision engine so follow-ups can be paused."""
        result = score_message(
            meta(
                from_email="ap@acmetraders.in",
                subject="Re: your note",
                snippet="We have already paid this on the 3rd, please check.",
            ),
            threshold=THRESHOLD,
        )
        assert result.is_relevant
        assert any(r.rule == "payment_claim_or_dispute" for r in result.reasons)

    def test_dispute_is_relevant(self):
        result = score_message(
            meta(
                from_email="ap@acmetraders.in",
                subject="Re: invoice",
                snippet="This invoice is wrong, we were overcharged.",
            ),
            threshold=THRESHOLD,
        )
        assert result.is_relevant

    def test_hinglish_payment_talk_is_relevant(self):
        result = score_message(
            meta(
                from_email="raj@novafoods.co.in",
                subject="Bill ka payment",
                snippet="Payment kar diya hai, bhugtan ho gaya. Rs. 5,250 bhej diya.",
            ),
            threshold=THRESHOLD,
        )
        assert result.is_relevant

    def test_known_customer_reply_without_keywords(self):
        """An established customer's follow-up counts even with no finance vocabulary."""
        result = score_message(
            meta(
                from_email="ap@acmetraders.in",
                subject="Re: our conversation",
                snippet="Sounds good, will confirm on Monday.",
            ),
            threshold=THRESHOLD,
            known_customer_emails={"ap@acmetraders.in"},
            known_customer_domains={"acmetraders.in"},
        )
        assert any(r.rule == "known_customer" for r in result.reasons)

    def test_payment_provider_sender(self):
        result = score_message(
            meta(
                from_email="noreply@razorpay.com",
                subject="Payment captured",
                snippet="A payment of Rs. 40,000 was captured.",
            ),
            threshold=THRESHOLD,
        )
        assert result.is_relevant


class TestIgnored:
    def test_unrelated_personal_email_is_ignored(self):
        """The headline negative case: ordinary mail must never enter the ledger."""
        result = score_message(
            meta(
                from_email="rahul@friendsgroup.com",
                from_name="Rahul",
                subject="Team lunch on Friday?",
                snippet="Thinking of that new place near the office. Are you in?",
            ),
            threshold=THRESHOLD,
        )
        assert not result.is_relevant
        assert result.score == 0
        assert result.summary() == "no finance signals found"

    @pytest.mark.parametrize(
        "subject,snippet",
        [
            ("Standup notes", "Here are yesterday's notes from the team standup."),
            ("Happy Diwali!", "Wishing you and your family a wonderful festive season."),
            ("Interview scheduled", "Your interview is confirmed for Tuesday at 3pm."),
            ("Server maintenance window", "We will restart the staging cluster tonight."),
        ],
    )
    def test_ordinary_business_chatter_is_ignored(self, subject, snippet):
        result = score_message(
            meta(from_email="colleague@partner.co", subject=subject, snippet=snippet),
            threshold=THRESHOLD,
        )
        assert not result.is_relevant, f"{subject!r} should not be finance-relevant"

    def test_newsletter_with_finance_words_is_hard_excluded(self):
        """Marketing mail is excluded by sender even when it is full of the right words."""
        result = score_message(
            meta(
                from_email="newsletter@linkedin.com",
                subject="Your invoice and payment receipt for Premium",
                snippet="Rs. 1,999 paid. View your billing statement.",
                label_ids=["INBOX", "CATEGORY_PROMOTIONS"],
            ),
            threshold=THRESHOLD,
        )
        assert not result.is_relevant
        assert result.hard_excluded

    @pytest.mark.parametrize("label", ["SPAM", "TRASH", "DRAFT"])
    def test_excluded_labels_short_circuit(self, label):
        result = score_message(
            meta(
                from_email="ap@acmetraders.in",
                subject="Tax Invoice INV-9 Rs. 40,000 overdue",
                label_ids=["INBOX", label],
                attachment_filenames=["invoice.pdf"],
                attachment_mime_types=["application/pdf"],
            ),
            threshold=THRESHOLD,
        )
        assert not result.is_relevant
        assert result.hard_excluded

    def test_social_category_is_demoted(self):
        result = score_message(
            meta(
                from_email="someone@example.org",
                subject="Payment for the bill",
                label_ids=["INBOX", "CATEGORY_SOCIAL"],
            ),
            threshold=THRESHOLD,
        )
        assert not result.is_relevant


class TestReasons:
    def test_every_decision_is_explainable(self):
        result = score_message(
            meta(
                from_email="ap@acmetraders.in",
                subject="Invoice INV-1 for Rs. 100",
                attachment_filenames=["invoice.pdf"],
                attachment_mime_types=["application/pdf"],
            ),
            threshold=THRESHOLD,
        )
        assert result.reasons
        for reason in result.reason_dicts:
            assert set(reason) == {"rule", "points", "detail"}


class TestBackfillQuery:
    def test_query_scopes_and_excludes(self):
        query = build_backfill_query("2025/08/27")
        assert "after:2025/08/27" in query
        assert "-in:spam" in query and "-in:trash" in query
        assert "invoice" in query and "has:attachment filename:pdf" in query

    def test_before_bound_is_optional(self):
        assert "before:2026/01/01" in build_backfill_query("2025/01/01", "2026/01/01")
        assert "before:" not in build_backfill_query("2025/01/01")
