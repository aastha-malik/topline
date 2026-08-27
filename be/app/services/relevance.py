"""Finance-relevance scoring for Gmail messages.

Topline reads a real inbox, so the first job is deciding what *not* to look at. Scoring runs
on message **metadata only** (headers, labels, snippet, attachment filenames). Only messages
that clear `relevance_threshold` earn a full-content fetch, which keeps the ingestion cost
bounded and means personal mail is never downloaded.

The score is a transparent sum of named rules. Every rule that fires is recorded on
`source_messages.relevance_reasons`, so "why was this skipped?" always has an answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Iterable

# --------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------

#: Strong: essentially only appear in invoicing/collections mail.
STRONG_KEYWORDS: frozenset[str] = frozenset(
    {
        "invoice", "tax invoice", "proforma", "pro forma", "credit note", "debit note",
        "remittance", "payment advice", "statement of account", "outstanding balance",
        "amount due", "balance due", "overdue", "past due", "payment reminder",
        "purchase order", "gst invoice", "e-invoice",
    }
)

#: Medium: finance-flavoured but common enough to need corroboration.
MEDIUM_KEYWORDS: frozenset[str] = frozenset(
    {
        "payment", "paid", "receipt", "bill", "billing", "due date", "net 30", "net 15",
        "utr", "neft", "rtgs", "imps", "upi", "cheque", "check number", "bank transfer",
        "transaction id", "reference number", "gst", "tds", "hsn", "sac code",
        "quotation", "advance payment", "settle", "settlement", "dues", "payable",
    }
)

#: Hinglish / transliterated Hindi. Small business mail in India mixes scripts freely.
HINGLISH_KEYWORDS: frozenset[str] = frozenset(
    {
        "bhugtan", "bakaya", "payment kiya", "paisa", "paise bhej", "bhej diya",
        "bill bhejo", "invoice bhejo", "payment kab", "kab tak", "baki hai",
        "transfer kar diya", "kar diya hai", "bhugtaan",
    }
)

#: Dispute / claim language - always worth ingesting, because it must pause follow-ups.
CLAIM_KEYWORDS: frozenset[str] = frozenset(
    {
        "already paid", "have paid", "we paid", "payment done", "payment made",
        "payment released", "cleared the invoice", "transferred the amount",
        "wrong invoice", "incorrect invoice", "invoice galat", "dispute", "disputed",
        "not as agreed", "overcharged", "double billed", "billed twice",
    }
)

#: Domains that confirm a payment/finance context regardless of wording.
PAYMENT_PROVIDER_DOMAINS: frozenset[str] = frozenset(
    {
        "razorpay.com", "stripe.com", "payu.in", "payubiz.in", "cashfree.com",
        "instamojo.com", "paytm.com", "phonepe.com", "billdesk.com", "ccavenue.com",
        "zohobooks.com", "zoho.com", "quickbooks.com", "intuit.com", "freshbooks.com",
        "xero.com", "tally.solutions", "clear.in", "cleartax.in",
    }
)

#: Bank senders - payment credit alerts are real evidence.
BANK_DOMAIN_HINTS: tuple[str, ...] = (
    "hdfcbank", "icicibank", "axisbank", "sbi.co.in", "kotak", "yesbank",
    "idfcfirstbank", "indusind", "pnb", "bankofbaroda", "canarabank", "unionbank",
)

#: Senders that are never receivables context, however many keywords they contain.
NOISE_DOMAIN_HINTS: tuple[str, ...] = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "medium.com", "substack.com", "quora.com", "pinterest.com",
    "meetup.com", "eventbrite.com", "spotify.com", "netflix.com", "swiggy.in",
    "zomato.com", "uber.com", "olacabs.com", "makemytrip.com", "goibibo.com",
    "amazon.in", "flipkart.com", "myntra.com", "nykaa.com",
)

#: Local-parts that mark bulk/automated mail rather than a person doing business.
NOISE_LOCALPARTS: tuple[str, ...] = (
    "newsletter", "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "digest", "updates", "marketing", "promo", "offers", "deals", "news", "alerts-news",
)

#: Gmail labels that disqualify a message outright.
EXCLUDED_LABELS: frozenset[str] = frozenset({"SPAM", "TRASH", "DRAFT", "CHAT"})

#: Gmail categories that are low-value; they lose points but are not auto-excluded,
#: because Gmail routinely files legitimate vendor invoices under Updates/Promotions.
DEMOTED_LABELS: dict[str, int] = {
    "CATEGORY_SOCIAL": -60,
    "CATEGORY_PROMOTIONS": -35,
    "CATEGORY_FORUMS": -40,
    "CATEGORY_UPDATES": -5,
}

#: Attachment filenames that look like an invoice document.
INVOICE_FILENAME_RE = re.compile(
    r"(invoice|inv[\-_ ]?\d|bill|receipt|statement|proforma|challan|quotation|po[\-_ ]?\d)",
    re.IGNORECASE,
)

#: A currency amount: Rs. 40,000 / INR 40000.00 / ₹40,000
AMOUNT_RE = re.compile(
    r"(?:₹|\bRs\.?|\bINR\b|\$)\s*[\d,]+(?:\.\d{1,2})?", re.IGNORECASE
)

#: An invoice-number-shaped token: INV-2024-001, TI/24-25/117
INVOICE_NUMBER_RE = re.compile(
    r"\b(?:INV|INVOICE|BILL|TI|PI|EST|PO)[\-/#\s]?[A-Z0-9][A-Z0-9\-/]{2,}\b", re.IGNORECASE
)


# --------------------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RelevanceReason:
    rule: str
    points: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "points": self.points, "detail": self.detail}


@dataclass(slots=True)
class RelevanceResult:
    score: int
    is_relevant: bool
    reasons: list[RelevanceReason] = field(default_factory=list)
    #: True when the message is excluded outright (spam/trash/known-noise sender).
    hard_excluded: bool = False

    @property
    def reason_dicts(self) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self.reasons]

    def summary(self) -> str:
        if not self.reasons:
            return "no finance signals found"
        top = sorted(self.reasons, key=lambda r: -abs(r.points))[:3]
        return "; ".join(f"{r.rule}({r.points:+d})" for r in top)


@dataclass(slots=True)
class MessageMetadata:
    """The metadata-only view of a Gmail message used for scoring."""

    gmail_message_id: str
    from_email: str = ""
    from_name: str = ""
    to_emails: list[str] = field(default_factory=list)
    cc_emails: list[str] = field(default_factory=list)
    subject: str = ""
    snippet: str = ""
    label_ids: list[str] = field(default_factory=list)
    attachment_filenames: list[str] = field(default_factory=list)
    attachment_mime_types: list[str] = field(default_factory=list)

    @property
    def from_domain(self) -> str:
        return email_domain(self.from_email)


def email_domain(address: str | None) -> str:
    if not address:
        return ""
    _, addr = parseaddr(address)
    _, _, domain = (addr or "").partition("@")
    return domain.strip().lower()


def local_part(address: str | None) -> str:
    if not address:
        return ""
    _, addr = parseaddr(address)
    local, _, _ = (addr or "").partition("@")
    return local.strip().lower()


def _contains_any(haystack: str, needles: Iterable[str]) -> list[str]:
    return [n for n in needles if n in haystack]


# --------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------


def score_message(
    meta: MessageMetadata,
    *,
    threshold: int,
    known_customer_domains: frozenset[str] | set[str] = frozenset(),
    known_customer_emails: frozenset[str] | set[str] = frozenset(),
    owner_domains: frozenset[str] | set[str] = frozenset(),
) -> RelevanceResult:
    """Score a message on metadata alone.

    `known_customer_domains` / `known_customer_emails` let an established ledger pull in
    follow-up correspondence that carries no finance keywords at all ("re: your note").
    """
    reasons: list[RelevanceReason] = []
    labels = {str(label).upper() for label in meta.label_ids}

    # --- hard exclusions ---------------------------------------------------------------
    if blocked := labels & EXCLUDED_LABELS:
        reasons.append(RelevanceReason("excluded_label", -100, ",".join(sorted(blocked))))
        return RelevanceResult(0, False, reasons, hard_excluded=True)

    domain = meta.from_domain
    sender_local = local_part(meta.from_email)
    is_known_counterparty = (
        domain in known_customer_domains
        or (meta.from_email or "").strip().lower() in known_customer_emails
    )

    if domain and any(hint in domain for hint in NOISE_DOMAIN_HINTS) and not is_known_counterparty:
        reasons.append(RelevanceReason("noise_sender_domain", -100, domain))
        return RelevanceResult(0, False, reasons, hard_excluded=True)

    haystack = f"{meta.subject}\n{meta.snippet}".lower()
    filenames = " ".join(meta.attachment_filenames).lower()
    score = 0

    # --- content signals ---------------------------------------------------------------
    if hits := _contains_any(haystack, STRONG_KEYWORDS):
        points = min(50, 30 + 10 * (len(hits) - 1))
        score += points
        reasons.append(RelevanceReason("strong_keyword", points, ",".join(hits[:4])))

    if hits := _contains_any(haystack, MEDIUM_KEYWORDS):
        points = min(24, 8 * len(hits))
        score += points
        reasons.append(RelevanceReason("medium_keyword", points, ",".join(hits[:4])))

    if hits := _contains_any(haystack, HINGLISH_KEYWORDS):
        score += 20
        reasons.append(RelevanceReason("hinglish_keyword", 20, ",".join(hits[:4])))

    if hits := _contains_any(haystack, CLAIM_KEYWORDS):
        # A payment claim or dispute must always reach the decision engine.
        score += 45
        reasons.append(RelevanceReason("payment_claim_or_dispute", 45, ",".join(hits[:3])))

    if AMOUNT_RE.search(meta.subject) or AMOUNT_RE.search(meta.snippet):
        score += 15
        reasons.append(RelevanceReason("currency_amount", 15))

    if INVOICE_NUMBER_RE.search(meta.subject):
        score += 20
        reasons.append(RelevanceReason("invoice_number_in_subject", 20))

    # --- attachment signals ------------------------------------------------------------
    has_pdf = any(
        (mt or "").lower() == "application/pdf" for mt in meta.attachment_mime_types
    ) or filenames.count(".pdf") > 0
    if has_pdf:
        score += 12
        reasons.append(RelevanceReason("pdf_attachment", 12))
        if INVOICE_FILENAME_RE.search(filenames):
            score += 28
            reasons.append(RelevanceReason("invoice_like_filename", 28, filenames[:80]))

    # --- sender signals ----------------------------------------------------------------
    if domain in PAYMENT_PROVIDER_DOMAINS:
        score += 35
        reasons.append(RelevanceReason("payment_provider_sender", 35, domain))
    elif domain and any(hint in domain for hint in BANK_DOMAIN_HINTS):
        score += 30
        reasons.append(RelevanceReason("bank_sender", 30, domain))

    if is_known_counterparty:
        score += 30
        reasons.append(RelevanceReason("known_customer", 30, domain or meta.from_email))

    if domain and domain in owner_domains:
        # Internal mail is usually chatter unless it also carries strong signals.
        score -= 10
        reasons.append(RelevanceReason("internal_sender", -10, domain))

    is_finance_sender = domain in PAYMENT_PROVIDER_DOMAINS or (
        bool(domain) and any(hint in domain for hint in BANK_DOMAIN_HINTS)
    )
    if sender_local and any(p in sender_local for p in NOISE_LOCALPARTS):
        # Payment providers and banks send genuine payment evidence from noreply@, so the
        # bulk-sender penalty applies only to senders that are not already trusted.
        if not is_known_counterparty and not is_finance_sender:
            score -= 30
            reasons.append(RelevanceReason("bulk_sender_localpart", -30, sender_local))

    for label, penalty in DEMOTED_LABELS.items():
        if label in labels:
            score += penalty
            reasons.append(RelevanceReason("demoted_label", penalty, label))

    score = max(0, min(100, score))
    return RelevanceResult(score, score >= threshold, reasons)


# --------------------------------------------------------------------------------------
# Server-side pre-filter
# --------------------------------------------------------------------------------------


def build_backfill_query(after: str, before: str | None = None) -> str:
    """Build the Gmail `q` for a backfill.

    This is a *pre*-filter: it stops Gmail from listing the whole mailbox. Local scoring
    still runs on everything it returns, so a loose query here cannot widen what is stored.
    """
    terms = [
        "invoice", "\"tax invoice\"", "proforma", "receipt", "payment", "bill",
        "overdue", "\"amount due\"", "\"balance due\"", "remittance",
        "\"purchase order\"", "gst", "utr", "neft", "rtgs", "upi",
    ]
    keyword_clause = " OR ".join(terms)
    parts = [
        f"({keyword_clause} OR (has:attachment filename:pdf))",
        f"after:{after}",
        "-in:spam",
        "-in:trash",
        "-in:chats",
        "-category:social",
    ]
    if before:
        parts.append(f"before:{before}")
    return " ".join(parts)
