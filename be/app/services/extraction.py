"""Deterministic invoice and payment-evidence extraction from email bodies and PDF text.

Everything here is rule-based and reproducible. The LLM extractor (`extract_invoice_facts`,
owned by the agent developer) is an *enrichment* layer that plugs in behind
:class:`InvoiceFactExtractor`; the ledger must stay usable, auditable and testable without
a model call.

Every fact carries an :class:`Evidence` record - the verbatim snippet plus its character
offsets in the source - so the dashboard can show the owner exactly where a number came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.relevance import CLAIM_KEYWORDS, email_domain

# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Evidence:
    """A verbatim snippet and where it was found."""

    snippet: str
    locator: str  # e.g. "email_body:chars=120-186" or "pdf:chars=44-70"
    field_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"snippet": self.snippet, "locator": self.locator, "field": self.field_name}


def _evidence(text: str, match: re.Match[str], source: str, field_name: str,
              pad: int = 45) -> Evidence:
    start = max(0, match.start() - pad)
    end = min(len(text), match.end() + pad)
    snippet = " ".join(text[start:end].split())
    return Evidence(snippet, f"{source}:chars={match.start()}-{match.end()}", field_name)


# --------------------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------------------

INVOICE_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:tax\s+)?invoice\s*(?:no\.?|number|num|#|id)?\s*[:\-#]?\s*"
        r"([A-Z0-9][A-Z0-9\-/_]{2,31})",
        re.IGNORECASE,
    ),
    re.compile(r"\binv(?:oice)?\s*[#:\-]\s*([A-Z0-9][A-Z0-9\-/_]{2,31})", re.IGNORECASE),
    re.compile(r"\b(INV[\-/][A-Z0-9\-/]{3,28})\b", re.IGNORECASE),
    re.compile(r"\b(TI/\d{2}-\d{2}/\d{2,8})\b", re.IGNORECASE),
    re.compile(r"\bbill\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/_]{2,31})",
               re.IGNORECASE),
)

#: Words that follow "invoice" but are never an invoice number.
_NUMBER_STOPWORDS = frozenset(
    {
        "for", "is", "of", "to", "and", "the", "attached", "date", "dated", "amount",
        "total", "due", "number", "no", "copy", "details", "value", "against", "from",
        "raised", "payment", "paid", "sent", "we", "you", "your", "our", "has", "have",
        "was", "will", "please", "find", "kindly", "regarding", "re", "id",
    }
)

CURRENCY_SYMBOLS = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "$": "USD",
                    "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP"}

AMOUNT_PATTERN = re.compile(
    r"(₹|\bRs\.?|\bINR\b|\bUSD\b|\$|€|£)\s*([\d][\d,\s]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

#: Labels that mark the *payable* figure, ranked. Higher rank wins over a bare amount.
TOTAL_LABELS: tuple[tuple[str, int], ...] = (
    ("total amount due", 100),
    ("amount due", 95),
    ("balance due", 95),
    ("total payable", 92),
    ("net payable", 92),
    ("grand total", 90),
    ("total amount", 85),
    ("invoice total", 85),
    ("total due", 85),
    ("total", 70),
    ("subtotal", 40),
)

_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d",
    "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%B-%Y",
    "%d/%m/%y", "%d-%m-%y", "%d %b %y",
)

_DATE_TOKEN = (
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"
    r"|\d{4}[/\-]\d{1,2}[/\-]\d{1,2}"
    r"|\d{1,2}(?:st|nd|rd|th)?[\s\-]+[A-Za-z]{3,9},?[\s\-]+\d{2,4}"
    r"|[A-Za-z]{3,9}[\s\-]+\d{1,2}(?:st|nd|rd|th)?,?[\s\-]+\d{2,4})"
)

ISSUE_DATE_PATTERN = re.compile(
    r"(?:invoice\s*date|bill\s*date|date\s*of\s*issue|issued?\s*(?:on|date)?|dated)\s*[:\-]?\s*"
    + _DATE_TOKEN,
    re.IGNORECASE,
)
DUE_DATE_PATTERN = re.compile(
    r"(?:due\s*date|payment\s*due|due\s*on|payable\s*(?:by|on)|pay\s*by)\s*[:\-]?\s*"
    + _DATE_TOKEN,
    re.IGNORECASE,
)
NET_TERMS_PATTERN = re.compile(
    r"(?:payment\s*terms?|terms)\s*[:\-]?\s*net\s*(\d{1,3})", re.IGNORECASE
)

PAYMENT_CLAIM_PATTERN = re.compile(
    r"("
    r"already\s+paid|have\s+(?:been\s+)?paid|we\s+(?:have\s+)?paid|payment\s+(?:has\s+been\s+)?"
    r"(?:made|done|released|processed|sent|initiated)|transferred\s+the\s+amount|"
    r"amount\s+(?:has\s+been\s+)?transferred|cleared\s+(?:the\s+)?(?:invoice|dues|payment)|"
    r"paid\s+(?:on|via|through|by)|payment\s+(?:done|kar\s+diya|kar\s+diya\s+hai)|"
    r"bhugtan\s+kar\s+diya|paisa\s+bhej\s+diya"
    r")",
    re.IGNORECASE,
)
DISPUTE_PATTERN = re.compile(
    r"("
    r"wrong\s+invoice|incorrect\s+(?:invoice|amount|billing)|invoice\s+galat|"
    r"(?:invoice|amount|bill|billing)\s+(?:is|was|seems|looks)\s+(?:wrong|incorrect|"
    r"inflated|not\s+right)|"
    r"dispute|disputed|disputing|overcharged|over\s*charged|double\s*billed|billed\s+twice|"
    r"not\s+as\s+(?:agreed|per\s+the\s+quote)|do\s+not\s+agree|discrepancy|"
    r"never\s+(?:received|ordered)|did\s+not\s+order"
    r")",
    re.IGNORECASE,
)
UTR_PATTERN = re.compile(
    r"(?:utr|rrn|transaction\s*(?:id|ref|reference)|txn\s*id|ref(?:erence)?\s*(?:no\.?|number))"
    r"\s*[:\-#]?\s*([A-Z0-9]{6,24})",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class InvoiceFacts:
    invoice_number: str | None = None
    amount_paise: int | None = None
    currency: str = "INR"
    issued_date: date | None = None
    due_date: date | None = None
    due_date_inferred: bool = False
    customer_name: str | None = None
    customer_email: str | None = None
    confidence: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def missing_fields(self) -> list[str]:
        missing = []
        if not self.invoice_number:
            missing.append("invoice_number")
        if self.amount_paise is None or self.amount_paise <= 0:
            missing.append("amount")
        if self.due_date is None:
            missing.append("due_date")
        if not self.customer_email:
            missing.append("customer_email")
        return missing

    @property
    def is_usable(self) -> bool:
        """Enough to create a ledger row at all: an amount plus someone to bill."""
        return bool(self.amount_paise) and bool(self.customer_email)


@dataclass(slots=True)
class PaymentSignals:
    """Payment-adjacent claims found in an email. Never a confirmation - only a claim."""

    has_payment_claim: bool = False
    has_dispute: bool = False
    claim_snippet: str | None = None
    dispute_snippet: str | None = None
    utr_reference: str | None = None
    paid_amount_paise: int | None = None
    evidence: list[Evidence] = field(default_factory=list)


class InvoiceFactExtractor(Protocol):
    """Extension point for the agent developer's Gemini extractor.

    Implement this and pass it to `app.services.ingest.IngestionPipeline` to enrich the
    deterministic result. The deterministic pass always runs first and always wins on
    fields the model is not more confident about.
    """

    def extract(self, text: str, *, source: str) -> InvoiceFacts: ...


# --------------------------------------------------------------------------------------
# Primitive parsers
# --------------------------------------------------------------------------------------


def parse_amount_to_paise(raw: str) -> int | None:
    """``"Rs. 40,000.00"`` -> ``4000000``. Integer paise; never a float."""
    cleaned = re.sub(r"[,\s]", "", raw)
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if value < 0:
        return None
    return int((value * 100).quantize(Decimal("1")))


def parse_date(raw: str) -> date | None:
    token = re.sub(r"(\d)(st|nd|rd|th)", r"\1", raw.strip(), flags=re.IGNORECASE)
    token = token.replace(",", " ")
    token = re.sub(r"\s+", " ", token).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).date()
        except ValueError:
            continue
    return None


def normalize_invoice_number(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().upper().rstrip(".,;:)")
    cleaned = re.sub(r"\s+", "", cleaned)
    if len(cleaned) < 3 or cleaned.lower() in _NUMBER_STOPWORDS:
        return None
    if not any(ch.isdigit() for ch in cleaned):
        return None  # an invoice number without a digit is a false positive
    return cleaned


def find_invoice_number(text: str, source: str) -> tuple[str | None, Evidence | None]:
    for pattern in INVOICE_NUMBER_PATTERNS:
        for match in pattern.finditer(text):
            candidate = normalize_invoice_number(match.group(1))
            if candidate:
                return candidate, _evidence(text, match, source, "invoice_number")
    return None, None


def find_amount(text: str, source: str) -> tuple[int | None, str, Evidence | None]:
    """Pick the payable total.

    Amounts are ranked by the label that precedes them, so "Total Amount Due" beats a line
    item and "Subtotal" loses to "Grand Total". With no labels at all, the largest amount
    wins - on an invoice that is almost always the total.
    """
    best: tuple[int, int, str, Evidence] | None = None  # (rank, paise, currency, evidence)
    lowered = text.lower()

    for match in AMOUNT_PATTERN.finditer(text):
        symbol = match.group(1).strip().lower()
        paise = parse_amount_to_paise(match.group(2))
        if paise is None or paise == 0:
            continue
        currency = CURRENCY_SYMBOLS.get(symbol, "INR")

        window = lowered[max(0, match.start() - 40) : match.start()]
        rank = 0
        for label, label_rank in TOTAL_LABELS:
            if label in window:
                rank = max(rank, label_rank)
                break
        evidence = _evidence(text, match, source, "amount")
        candidate = (rank, paise, currency, evidence)
        if best is None or (rank, paise) > (best[0], best[1]):
            best = candidate

    if best is None:
        return None, "INR", None
    return best[1], best[2], best[3]


def find_dates(
    text: str, source: str, *, default_terms_days: int
) -> tuple[date | None, date | None, bool, list[Evidence]]:
    """Return ``(issued_date, due_date, due_was_inferred, evidence)``."""
    evidence: list[Evidence] = []
    issued = due = None

    if match := ISSUE_DATE_PATTERN.search(text):
        issued = parse_date(match.group(1))
        if issued:
            evidence.append(_evidence(text, match, source, "issued_date"))

    if match := DUE_DATE_PATTERN.search(text):
        due = parse_date(match.group(1))
        if due:
            evidence.append(_evidence(text, match, source, "due_date"))

    inferred = False
    if due is None and issued is not None:
        # "Net 15" on the invoice beats the workspace default.
        terms = default_terms_days
        if match := NET_TERMS_PATTERN.search(text):
            terms = int(match.group(1))
            evidence.append(_evidence(text, match, source, "payment_terms"))
        due = issued + timedelta(days=terms)
        inferred = True

    return issued, due, inferred, evidence


# --------------------------------------------------------------------------------------
# Composite extraction
# --------------------------------------------------------------------------------------


def extract_invoice_facts(
    text: str,
    *,
    source: str = "pdf",
    default_terms_days: int = 15,
    customer_name: str | None = None,
    customer_email: str | None = None,
) -> InvoiceFacts:
    """Pull invoice facts out of PDF text or an email body.

    `customer_name` / `customer_email` come from the Gmail headers; the document itself
    rarely states the counterparty in a machine-readable way.
    """
    facts = InvoiceFacts(customer_name=customer_name, customer_email=customer_email)
    if not text or not text.strip():
        return facts

    number, number_ev = find_invoice_number(text, source)
    facts.invoice_number = number
    if number_ev:
        facts.evidence.append(number_ev)

    paise, currency, amount_ev = find_amount(text, source)
    facts.amount_paise = paise
    facts.currency = currency
    if amount_ev:
        facts.evidence.append(amount_ev)

    issued, due, inferred, date_ev = find_dates(
        text, source, default_terms_days=default_terms_days
    )
    facts.issued_date = issued
    facts.due_date = due
    facts.due_date_inferred = inferred
    facts.evidence.extend(date_ev)

    facts.confidence = _score_confidence(facts, source)
    return facts


def _score_confidence(facts: InvoiceFacts, source: str) -> float:
    """Confidence that this really is an invoice, from how many facts corroborate it."""
    score = 0.0
    if facts.invoice_number:
        score += 0.35
    if facts.amount_paise:
        score += 0.30
    if facts.issued_date:
        score += 0.10
    if facts.due_date:
        score += 0.15 if not facts.due_date_inferred else 0.05
    if facts.customer_email:
        score += 0.10
    if source.startswith("pdf"):
        # A structured PDF is stronger evidence than prose in a mail body.
        score += 0.05
    return round(min(1.0, score), 3)


def extract_payment_signals(text: str, *, source: str = "email_body") -> PaymentSignals:
    """Find payment claims and disputes in an email body.

    These pause follow-ups; they never mark an invoice paid. Confirmation requires a
    provider event (see :mod:`app.services.razorpay_sync`).
    """
    signals = PaymentSignals()
    if not text or not text.strip():
        return signals

    if match := PAYMENT_CLAIM_PATTERN.search(text):
        ev = _evidence(text, match, source, "payment_claim")
        signals.has_payment_claim = True
        signals.claim_snippet = ev.snippet
        signals.evidence.append(ev)

    if match := DISPUTE_PATTERN.search(text):
        ev = _evidence(text, match, source, "dispute")
        signals.has_dispute = True
        signals.dispute_snippet = ev.snippet
        signals.evidence.append(ev)

    if match := UTR_PATTERN.search(text):
        signals.utr_reference = match.group(1).upper()
        signals.evidence.append(_evidence(text, match, source, "utr_reference"))

    if signals.has_payment_claim:
        paise, _, amount_ev = find_amount(text, source)
        if paise:
            signals.paid_amount_paise = paise
            if amount_ev:
                signals.evidence.append(amount_ev)

    return signals


def strip_quoted_reply(body: str) -> str:
    """Drop quoted history so a reply is classified on its new text only."""
    if not body:
        return ""
    cut_markers = (
        re.compile(r"^On .{5,120}\bwrote:\s*$", re.MULTILINE),
        re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
        re.compile(r"^_{5,}\s*$", re.MULTILINE),
        re.compile(r"^From:\s.+$", re.MULTILINE),
        re.compile(r"^Sent from my \w+", re.MULTILINE),
    )
    earliest = len(body)
    for pattern in cut_markers:
        if match := pattern.search(body):
            earliest = min(earliest, match.start())
    trimmed = body[:earliest]
    lines = [ln for ln in trimmed.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def guess_customer_identity(
    from_email: str | None,
    from_name: str | None,
    owner_domains: frozenset[str] | set[str] = frozenset(),
) -> tuple[str, str] | None:
    """Return ``(name, email)`` for the counterparty, or None if it is the owner."""
    if not from_email:
        return None
    email = from_email.strip().lower()
    domain = email_domain(email)
    if not domain or domain in owner_domains:
        return None
    name = (from_name or "").strip()
    if not name or "@" in name:
        # Fall back to a title-cased domain: "acmetraders.in" -> "Acmetraders"
        name = domain.split(".")[0].replace("-", " ").title()
    return name, email


#: Re-exported so the ingestion pipeline can flag claim-like mail without importing relevance.
__all__ = [
    "CLAIM_KEYWORDS",
    "Evidence",
    "InvoiceFactExtractor",
    "InvoiceFacts",
    "PaymentSignals",
    "extract_invoice_facts",
    "extract_payment_signals",
    "find_amount",
    "find_invoice_number",
    "guess_customer_identity",
    "normalize_invoice_number",
    "parse_amount_to_paise",
    "parse_date",
    "strip_quoted_reply",
]
