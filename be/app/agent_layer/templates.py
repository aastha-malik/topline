from __future__ import annotations

import html
import re
from collections.abc import Sequence
from urllib.parse import urlparse

from .domain import BrandProfile, DigestItemRecord, DraftRecord

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _safe_color(value: str) -> str:
    return value if _HEX_COLOR.fullmatch(value) else "#155EEF"


def _safe_logo(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc else None


def _paragraphs(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    return "".join(
        f'<p style="margin:0 0 16px;line-height:1.6">{html.escape(block).replace(chr(10), "<br>")}</p>'
        for block in blocks
    )


def render_branded_email(brand: BrandProfile, text_body: str) -> str:
    """Render model text inside a fixed, escaped HTML template.

    Model output is always treated as plain text. It cannot add scripts, links,
    tracking pixels, styles, or arbitrary markup.
    """

    business = html.escape(brand.business_name)
    sender = html.escape(brand.sender_name)
    color = _safe_color(brand.primary_color)
    logo = _safe_logo(brand.logo_url)
    logo_html = (
        f'<img src="{html.escape(logo, quote=True)}" alt="{business}" '
        'style="display:block;max-height:44px;max-width:180px;margin-bottom:18px">'
        if logo
        else ""
    )
    return (
        '<!doctype html><html><body style="margin:0;background:#f6f8fb">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        '<tr><td align="center" style="padding:28px 12px">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:620px;background:#fff;border:1px solid #e4e7ec;border-radius:12px">'
        f'<tr><td style="height:6px;background:{color};border-radius:12px 12px 0 0"></td></tr>'
        '<tr><td style="padding:30px;font:15px Arial,sans-serif;color:#1d2939">'
        f"{logo_html}{_paragraphs(text_body)}"
        f'<p style="margin:24px 0 0;color:#475467">Regards,<br>{sender}<br>{business}</p>'
        '</td></tr></table></td></tr></table></body></html>'
    )


def render_digest(items: Sequence[DigestItemRecord], run_date: str) -> tuple[str, str]:
    lines = [f"Topline daily receivables review — {run_date}", ""]
    rows: list[str] = []
    for item in items:
        amount = item.amount_paise / 100
        lines.append(
            f"{item.item_number}. {item.customer_name} — {amount:,.2f} — "
            f"oldest due {item.oldest_due_date.isoformat()}"
        )
        lines.append(f"   Why: {item.recommendation_reason}")
        rows.append(
            "<tr>"
            f'<td style="padding:12px;border-bottom:1px solid #e4e7ec">{item.item_number}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e4e7ec">{html.escape(item.customer_name)}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e4e7ec">{amount:,.2f}</td>'
            f'<td style="padding:12px;border-bottom:1px solid #e4e7ec">{html.escape(item.recommendation_reason)}</td>'
            "</tr>"
        )
    lines.extend(
        [
            "",
            "Reply naturally, for example: Acme ko firm bhejo, Bharat ko abhi chhod do.",
            "Topline will return drafts for review. Nothing is sent to customers without your explicit approval.",
        ]
    )
    html_body = (
        '<div style="font:14px Arial,sans-serif;color:#1d2939">'
        f"<h2>Daily receivables review — {html.escape(run_date)}</h2>"
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        '<tr><th align="left">#</th><th align="left">Customer</th>'
        '<th align="left">Outstanding</th><th align="left">Why now</th></tr>'
        f'{"".join(rows)}</table>'
        '<p>Reply with instructions. Topline will propose drafts for review; it will not email a customer without explicit approval.</p>'
        "</div>"
    )
    return "\n".join(lines), html_body


def render_draft_review(drafts: Sequence[DraftRecord]) -> tuple[str, str]:
    text_parts = ["Topline prepared these drafts for your approval:", ""]
    html_parts = ["<h2>Drafts for approval</h2>"]
    for draft in drafts:
        text_parts.extend(
            [
                f"DRAFT {draft.draft_number} — {draft.customer_email}",
                f"Subject: {draft.subject}",
                draft.text_body,
                f"Why this draft: {draft.rationale}",
                "",
            ]
        )
        html_parts.append(
            '<section style="margin:20px 0;padding:16px;border:1px solid #d0d5dd;border-radius:10px">'
            f"<h3>Draft {draft.draft_number} — {html.escape(draft.customer_email)}</h3>"
            f"<p><strong>Subject:</strong> {html.escape(draft.subject)}</p>"
            f"{_paragraphs(draft.text_body)}"
            f"<p><strong>Why:</strong> {html.escape(draft.rationale)}</p>"
            "</section>"
        )
    text_parts.append("Reply `send 1`, `send 1,2`, or `send all` to approve and send exactly those drafts.")
    html_parts.append(
        "<p>Reply <strong>send 1</strong>, <strong>send 1,2</strong>, or "
        "<strong>send all</strong> to approve and send exactly those drafts.</p>"
    )
    return "\n".join(text_parts), "".join(html_parts)


def normalize_subject(subject: str) -> str:
    normalized = " ".join(subject.replace("\r", " ").replace("\n", " ").split())
    if not normalized:
        raise ValueError("Email subject cannot be empty")
    return normalized[:200]
