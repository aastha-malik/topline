"""The Gmail ingestion pipeline: backfill, incremental sync, and evidence extraction.

Shape of a run
--------------
1. **List** message ids with a server-side finance pre-filter (bounded by a date window).
2. **Metadata fetch** for each id - headers, labels and attachment names only.
3. **Score** with :mod:`app.services.relevance`. Below threshold, the message is recorded
   as `ignored` with its reasons and *its body is never downloaded*.
4. **Full fetch** for candidates only, then PDF attachments (text first, OCR fallback).
5. **Extract** invoice facts and payment signals deterministically, and write them to the
   ledger with an evidence link back to the Gmail message or PDF.

Idempotency
-----------
Every step keys off a natural identifier, so re-running a sync converges instead of
duplicating: messages by `(gmail_account_id, gmail_message_id)`, invoices by dedupe key,
evidence by content hash, payment events by provider event id, audit rows by dedupe key.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Sequence, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import (
    ActorType,
    ExtractionStatus,
    LinkType,
    MessageDirection,
    MessageProcessingState,
    PaymentEventType,
    PaymentProvider,
    SyncMode,
    SyncStatus,
)
from app.logging_config import get_logger
from app.models import Customer, GmailAccount, SourceAttachment, SourceMessage, SyncRun
from app.services import ledger
from app.services.audit import audit_key, record_event
from app.services.extraction import (
    extract_invoice_facts,
    extract_payment_signals,
    guess_customer_identity,
    strip_quoted_reply,
)
from app.services.gmail import (
    AttachmentRef,
    GmailClient,
    GmailHistoryExpired,
    ParsedMessage,
)
from app.services.pdf import extract_pdf_text, sha256_bytes
from app.services.relevance import (
    RelevanceResult,
    build_backfill_query,
    email_domain,
    score_message,
)

logger = get_logger(__name__)

T = TypeVar("T")


async def _blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a synchronous Gmail transport call off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


@dataclass(slots=True)
class SyncOutcome:
    sync_run_id: uuid.UUID
    mode: str
    status: str
    messages_listed: int = 0
    messages_metadata_fetched: int = 0
    messages_content_fetched: int = 0
    messages_ignored: int = 0
    attachments_processed: int = 0
    invoices_created: int = 0
    invoices_updated: int = 0
    payment_events_recorded: int = 0
    history_fallback_used: bool = False
    start_history_id: str | None = None
    end_history_id: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sync_run_id": str(self.sync_run_id),
            "mode": self.mode,
            "status": self.status,
            "messages_listed": self.messages_listed,
            "messages_metadata_fetched": self.messages_metadata_fetched,
            "messages_content_fetched": self.messages_content_fetched,
            "messages_ignored": self.messages_ignored,
            "attachments_processed": self.attachments_processed,
            "invoices_created": self.invoices_created,
            "invoices_updated": self.invoices_updated,
            "payment_events_recorded": self.payment_events_recorded,
            "history_fallback_used": self.history_fallback_used,
            "start_history_id": self.start_history_id,
            "end_history_id": self.end_history_id,
            "error": self.error,
            "notes": self.notes,
        }


class IngestionPipeline:
    """Runs Gmail ingestion for one connected account."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        account: GmailAccount,
        client: GmailClient,
        settings: Settings,
        today: date | None = None,
    ) -> None:
        self.session = session
        self.account = account
        self.client = client
        self.settings = settings
        self.today = today or datetime.now(timezone.utc).date()
        self.owner_domains = {email_domain(account.email_address)} - {""}
        self._known_domains: set[str] = set()
        self._known_emails: set[str] = set()

    # ----------------------------------------------------------------------------------
    # Entry points
    # ----------------------------------------------------------------------------------

    async def run_backfill(self, *, months: int | None = None) -> SyncOutcome:
        """Ingest the owner-selected history window (default 12 months)."""
        months = months or self.settings.backfill_months
        window_start = self.today - timedelta(days=30 * months)
        query = build_backfill_query(after=window_start.strftime("%Y/%m/%d"))

        run = await self._start_run(SyncMode.BACKFILL)
        outcome = SyncOutcome(run.id, str(SyncMode.BACKFILL), str(SyncStatus.RUNNING))
        self.account.backfill_status = str(SyncStatus.RUNNING)
        self.account.backfill_window_start = window_start
        self.account.backfill_window_end = self.today
        await self.session.flush()

        try:
            await self._load_known_counterparties()
            message_ids = await _blocking(
                lambda: list(
                    self.client.iter_message_ids(
                        query=query,
                        page_size=self.settings.gmail_list_page_size,
                        max_messages=self.settings.gmail_max_messages_per_run,
                    )
                )
            )
            outcome.messages_listed = len(message_ids)
            await self._process_message_ids(message_ids, outcome)

            profile = await _blocking(self.client.get_profile)
            outcome.end_history_id = _as_str(profile.get("historyId"))
            self.account.backfill_status = str(SyncStatus.COMPLETED)
            self.account.last_backfill_at = datetime.now(timezone.utc)
            outcome.status = str(SyncStatus.COMPLETED)
        except Exception as exc:
            logger.exception("gmail backfill failed", extra={"account": str(self.account.id)})
            self.account.backfill_status = str(SyncStatus.FAILED)
            outcome.status = str(SyncStatus.FAILED)
            outcome.error = str(exc)

        await self._finish_run(run, outcome)
        return outcome

    async def run_incremental(self) -> SyncOutcome:
        """Sync new messages using the stored Gmail `historyId`.

        When Gmail rejects the cursor - it only retains history for a limited window - this
        falls back to a *scoped* resync of the recent past rather than a full re-read.
        """
        run = await self._start_run(SyncMode.INCREMENTAL)
        start_history_id = self.account.last_history_id
        outcome = SyncOutcome(
            run.id,
            str(SyncMode.INCREMENTAL),
            str(SyncStatus.RUNNING),
            start_history_id=start_history_id,
        )

        try:
            await self._load_known_counterparties()

            if not start_history_id:
                outcome.notes.append("no stored history id; running scoped resync instead")
                await self._fallback_resync(outcome, run)
            else:
                try:
                    message_ids, latest = await _blocking(
                        self.client.iter_history_message_ids, start_history_id
                    )
                    outcome.messages_listed = len(message_ids)
                    await self._process_message_ids(message_ids, outcome)
                    outcome.end_history_id = _as_str(latest)
                except GmailHistoryExpired as exc:
                    outcome.notes.append(f"history id rejected by Gmail ({exc}); scoped resync")
                    outcome.history_fallback_used = True
                    run.mode = str(SyncMode.FALLBACK_RESYNC)
                    outcome.mode = str(SyncMode.FALLBACK_RESYNC)
                    await self._fallback_resync(outcome, run)

            if not outcome.end_history_id:
                profile = await _blocking(self.client.get_profile)
                outcome.end_history_id = _as_str(profile.get("historyId"))

            self.account.last_incremental_sync_at = datetime.now(timezone.utc)
            outcome.status = str(SyncStatus.COMPLETED)
        except Exception as exc:
            logger.exception("gmail incremental sync failed")
            outcome.status = str(SyncStatus.FAILED)
            outcome.error = str(exc)

        await self._finish_run(run, outcome)
        return outcome

    async def _fallback_resync(self, outcome: SyncOutcome, run: SyncRun) -> None:
        """Re-scan a bounded recent window when the history cursor is unusable.

        Scoped on purpose: an expired cursor means "we may have missed a few days", not
        "re-read the mailbox". Already-seen messages are skipped by their unique key, so
        this is cheap and cannot duplicate ledger rows.
        """
        outcome.history_fallback_used = True
        window_start = self.today - timedelta(days=self.settings.fallback_resync_days)
        query = build_backfill_query(after=window_start.strftime("%Y/%m/%d"))
        message_ids = await _blocking(
            lambda: list(
                self.client.iter_message_ids(
                    query=query,
                    page_size=self.settings.gmail_list_page_size,
                    max_messages=self.settings.gmail_max_messages_per_run,
                )
            )
        )
        outcome.messages_listed += len(message_ids)
        outcome.notes.append(f"scoped resync from {window_start.isoformat()}")
        await self._process_message_ids(message_ids, outcome)

    # ----------------------------------------------------------------------------------
    # Per-message processing
    # ----------------------------------------------------------------------------------

    async def _process_message_ids(
        self, message_ids: Sequence[str], outcome: SyncOutcome
    ) -> None:
        for message_id in message_ids:
            try:
                await self._process_one(message_id, outcome)
            except Exception as exc:
                # One malformed message must not abort an entire backfill.
                logger.warning(
                    "message ingestion failed",
                    exc_info=True,
                    extra={"gmail_message_id": message_id, "error": str(exc)},
                )
                outcome.notes.append(f"{message_id}: {exc}")

    async def _process_one(self, message_id: str, outcome: SyncOutcome) -> None:
        existing = await self.session.scalar(
            select(SourceMessage).where(
                SourceMessage.gmail_account_id == self.account.id,
                SourceMessage.gmail_message_id == message_id,
            )
        )
        if existing is not None and existing.processing_state in (
            MessageProcessingState.EXTRACTED,
            MessageProcessingState.IGNORED,
        ):
            # Already decided on this message; re-running the sync is a no-op.
            return

        # --- step 1: metadata only ------------------------------------------------------
        metadata_message = await _blocking(self.client.get_metadata, message_id)
        outcome.messages_metadata_fetched += 1

        result = score_message(
            metadata_message.to_metadata(),
            threshold=self.settings.relevance_threshold,
            known_customer_domains=frozenset(self._known_domains),
            known_customer_emails=frozenset(self._known_emails),
            owner_domains=frozenset(self.owner_domains),
        )

        if not result.is_relevant:
            await self._record_ignored(metadata_message, result, existing)
            outcome.messages_ignored += 1
            return

        # --- step 2: full content, candidates only --------------------------------------
        full_message = await _blocking(self.client.get_full, message_id)
        outcome.messages_content_fetched += 1

        record = await self._upsert_source_message(
            full_message, result, existing, store_body=True
        )
        await self._extract_and_link(full_message, record, outcome)

        record.processing_state = str(MessageProcessingState.EXTRACTED)
        await self.session.flush()

    async def _record_ignored(
        self,
        message: ParsedMessage,
        result: RelevanceResult,
        existing: SourceMessage | None,
    ) -> None:
        """Persist the decision to skip a message - without persisting the message.

        `body_text` stays null. The owner can see *that* a message was considered and why,
        which is what "unrelated mail is ignored and logged" has to mean if the product is
        not to quietly hoard personal email.
        """
        record = await self._upsert_source_message(message, result, existing, store_body=False)
        record.processing_state = str(MessageProcessingState.IGNORED)
        await self.session.flush()

        await record_event(
            self.session,
            workspace_id=self.account.workspace_id,
            owner_id=self.account.user_id,
            event_type="gmail.message_ignored",
            summary=f"Ignored non-finance message: {result.summary()}",
            entity_type="source_message",
            entity_id=str(record.id),
            decision={
                "score": result.score,
                "threshold": self.settings.relevance_threshold,
                "hard_excluded": result.hard_excluded,
                "reasons": result.reason_dicts,
            },
            dedupe_key=audit_key("gmail.message_ignored", self.account.id, message.gmail_message_id),
        )

    async def _upsert_source_message(
        self,
        message: ParsedMessage,
        result: RelevanceResult,
        existing: SourceMessage | None,
        *,
        store_body: bool,
    ) -> SourceMessage:
        direction = (
            MessageDirection.OUTBOUND
            if message.from_email == (self.account.email_address or "").lower()
            else MessageDirection.INBOUND
        )
        body = message.body_text[: self.settings.max_stored_body_chars] if store_body else None

        record = existing
        if record is None:
            record = SourceMessage(
                workspace_id=self.account.workspace_id,
                gmail_account_id=self.account.id,
                gmail_message_id=message.gmail_message_id,
            )
            self.session.add(record)

        record.gmail_thread_id = message.gmail_thread_id
        record.gmail_history_id = message.history_id or record.gmail_history_id
        record.internal_date = message.internal_date
        record.from_email = message.from_email or None
        record.from_name = message.from_name or None
        record.from_domain = email_domain(message.from_email) or None
        record.to_emails = message.to_emails
        record.cc_emails = message.cc_emails
        record.subject = message.subject
        record.snippet = message.snippet
        record.label_ids = message.label_ids
        record.has_attachments = bool(message.attachments)
        record.direction = str(direction)
        record.is_finance_relevant = result.is_relevant
        record.relevance_score = result.score
        record.relevance_reasons = result.reason_dicts
        if store_body:
            record.body_text = body
            record.body_hash = (
                hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None
            )
            record.processing_state = str(MessageProcessingState.FETCHED)
        await self.session.flush()
        return record

    # ----------------------------------------------------------------------------------
    # Evidence -> ledger
    # ----------------------------------------------------------------------------------

    async def _extract_and_link(
        self, message: ParsedMessage, record: SourceMessage, outcome: SyncOutcome
    ) -> None:
        counterparty = self._resolve_counterparty(message, record)
        if counterparty is None:
            outcome.notes.append(f"{message.gmail_message_id}: no counterparty resolved")
            return
        name, email = counterparty

        customer = await ledger.upsert_customer(
            self.session,
            workspace_id=self.account.workspace_id,
            owner_id=self.account.user_id,
            name=name,
            email=email,
            seen_at=message.internal_date,
        )
        self._known_emails.add(customer.primary_email)
        if customer.domain:
            self._known_domains.add(customer.domain)

        attachments = await self._process_attachments(message, record, outcome)

        # --- invoice facts: PDFs first, then the email body ------------------------------
        found_invoice = False
        for attachment, text in attachments:
            if not text:
                continue
            facts = extract_invoice_facts(
                text,
                source="pdf",
                default_terms_days=self.settings.default_payment_terms_days,
                customer_name=customer.name,
                customer_email=customer.primary_email,
            )
            if not facts.is_usable:
                continue
            await self._persist_invoice(
                facts, customer, record, attachment, outcome, LinkType.INVOICE_DOCUMENT
            )
            found_invoice = True

        if not found_invoice and message.body_text:
            facts = extract_invoice_facts(
                message.body_text,
                source="email_body",
                default_terms_days=self.settings.default_payment_terms_days,
                customer_name=customer.name,
                customer_email=customer.primary_email,
            )
            if facts.is_usable and facts.confidence >= 0.5:
                await self._persist_invoice(
                    facts, customer, record, None, outcome, LinkType.INVOICE_MENTION
                )

        # --- payment claims and disputes -------------------------------------------------
        await self._persist_payment_signals(message, record, customer, outcome)

    def _resolve_counterparty(
        self, message: ParsedMessage, record: SourceMessage
    ) -> tuple[str, str] | None:
        """Work out who the customer is on this message.

        On mail the owner sent, the customer is the recipient; on mail the owner received,
        it is the sender. `guess_customer_identity` decides what counts as "the owner" -
        wholesale for a company domain, exact-address-only for a personal email provider a
        customer might share with the owner - so both directions defer to it rather than
        pre-filtering by domain here.
        """
        if record.direction == MessageDirection.OUTBOUND:
            for address in message.to_emails:
                identity = guess_customer_identity(
                    address, "", self.owner_domains, owner_email=self.account.email_address
                )
                if identity is not None:
                    return identity
            return None
        return guess_customer_identity(
            message.from_email,
            message.from_name,
            self.owner_domains,
            owner_email=self.account.email_address,
        )

    async def _process_attachments(
        self, message: ParsedMessage, record: SourceMessage, outcome: SyncOutcome
    ) -> list[tuple[SourceAttachment, str]]:
        """Download and extract PDF attachments. Returns `(row, extracted_text)` pairs."""
        results: list[tuple[SourceAttachment, str]] = []

        for ref in message.attachments:
            if not _is_pdf(ref):
                continue
            if ref.size_bytes > self.settings.gmail_max_attachment_bytes:
                outcome.notes.append(f"{ref.filename}: skipped, exceeds size limit")
                continue

            row = await self.session.scalar(
                select(SourceAttachment).where(
                    SourceAttachment.source_message_id == record.id,
                    SourceAttachment.gmail_attachment_id == ref.attachment_id,
                )
            )
            if row is not None and row.extraction_status in (
                ExtractionStatus.TEXT_EXTRACTED,
                ExtractionStatus.OCR_EXTRACTED,
            ):
                results.append((row, row.extracted_text or ""))
                continue

            data = ref.inline_data
            if data is None and ref.attachment_id:
                data = await _blocking(
                    self.client.get_attachment_bytes, message.gmail_message_id, ref.attachment_id
                )
            data = data or b""

            extraction = extract_pdf_text(
                data,
                mime_type=ref.mime_type,
                max_chars=self.settings.max_stored_pdf_chars,
                enable_ocr=self.settings.enable_ocr_fallback,
                ocr_trigger_chars_per_page=self.settings.ocr_trigger_chars_per_page,
            )

            if row is None:
                row = SourceAttachment(
                    workspace_id=self.account.workspace_id,
                    source_message_id=record.id,
                    gmail_attachment_id=ref.attachment_id,
                )
                self.session.add(row)

            row.filename = ref.filename
            row.mime_type = ref.mime_type
            row.size_bytes = ref.size_bytes or len(data)
            row.content_sha256 = sha256_bytes(data) if data else None
            row.extraction_status = str(extraction.status)
            row.extraction_method = str(extraction.method)
            row.extracted_text = extraction.text or None
            row.page_count = extraction.page_count
            row.extraction_error = extraction.error
            await self.session.flush()

            outcome.attachments_processed += 1
            if extraction.status == ExtractionStatus.OCR_UNAVAILABLE:
                outcome.notes.append(f"{ref.filename}: scanned PDF, OCR backend unavailable")
            results.append((row, extraction.text))

        return results

    async def _persist_invoice(
        self,
        facts,
        customer: Customer,
        record: SourceMessage,
        attachment: SourceAttachment | None,
        outcome: SyncOutcome,
        link_type: str,
    ) -> None:
        result = await ledger.upsert_invoice_from_facts(
            self.session,
            workspace_id=self.account.workspace_id,
            owner_id=self.account.user_id,
            customer=customer,
            facts=facts,
            source_message_id=record.id,
            source_attachment_id=attachment.id if attachment else None,
            today=self.today,
            grace_days=self.settings.reminder_grace_days,
            reminder_cooldown_days=self.settings.reminder_cooldown_days,
        )
        if result.created:
            outcome.invoices_created += 1
        else:
            outcome.invoices_updated += 1

        await ledger.link_extraction_evidence(
            self.session,
            workspace_id=self.account.workspace_id,
            invoice_id=result.invoice.id,
            evidence=facts.evidence,
            link_type=link_type,
            source_message_id=record.id,
            source_attachment_id=attachment.id if attachment else None,
            confidence=facts.confidence,
        )

        await record_event(
            self.session,
            workspace_id=self.account.workspace_id,
            owner_id=self.account.user_id,
            event_type="ledger.invoice_extracted" if result.created else "ledger.invoice_updated",
            summary=(
                f"{'Created' if result.created else 'Updated'} invoice "
                f"{facts.invoice_number or '(no number)'} for {customer.name}: "
                f"{result.decision.effective_state}"
            ),
            entity_type="invoice",
            entity_id=str(result.invoice.id),
            decision=result.decision.as_dict(),
            source_evidence=[e.as_dict() for e in facts.evidence],
            dedupe_key=audit_key(
                "ledger.invoice",
                result.invoice.id,
                record.gmail_message_id,
                attachment.id if attachment else "",
                result.decision.effective_state,
            ),
        )

    async def _persist_payment_signals(
        self,
        message: ParsedMessage,
        record: SourceMessage,
        customer: Customer,
        outcome: SyncOutcome,
    ) -> None:
        """Turn payment claims and disputes in an email into (non-confirming) evidence."""
        body = strip_quoted_reply(message.body_text)
        if not body:
            return
        signals = extract_payment_signals(body, source="email_body")
        if not (signals.has_payment_claim or signals.has_dispute):
            return

        invoices = (
            await self.session.scalars(
                select(ledger.Invoice).where(
                    ledger.Invoice.workspace_id == self.account.workspace_id,
                    ledger.Invoice.customer_id == customer.id,
                )
            )
        ).all()
        if not invoices:
            return

        # Without an invoice number in the mail, the claim applies to every open invoice
        # for that customer - deliberately conservative: pausing too much is safe, and
        # sending a reminder for an invoice the customer just paid is not.
        targets = _match_claim_targets(signals, invoices, body)

        for invoice in targets:
            for is_dispute in (True, False):
                if is_dispute and not signals.has_dispute:
                    continue
                if not is_dispute and not signals.has_payment_claim:
                    continue
                snippet = signals.dispute_snippet if is_dispute else signals.claim_snippet
                event_type = (
                    PaymentEventType.EMAIL_DISPUTE if is_dispute
                    else PaymentEventType.EMAIL_PAYMENT_CLAIM
                )
                event, created = await ledger.record_payment_event(
                    self.session,
                    workspace_id=self.account.workspace_id,
                    provider=PaymentProvider.GMAIL,
                    provider_event_id=f"{message.gmail_message_id}:{event_type}:{invoice.id}",
                    event_type=event_type,
                    amount_paise=None if is_dispute else signals.paid_amount_paise,
                    observed_at=message.internal_date,
                    invoice_id=invoice.id,
                    customer_id=customer.id,
                    source_message_id=record.id,
                    evidence_snippet=snippet,
                    payload={"utr_reference": signals.utr_reference},
                    reconciliation_method="gmail_body_signal",
                )
                if created:
                    outcome.payment_events_recorded += 1
                if event is not None:
                    await ledger.link_evidence(
                        self.session,
                        workspace_id=self.account.workspace_id,
                        invoice_id=invoice.id,
                        link_type=LinkType.DISPUTE if is_dispute else LinkType.PAYMENT_CLAIM,
                        snippet=snippet,
                        locator=f"gmail:{message.gmail_message_id}",
                        source_message_id=record.id,
                        payment_event_id=event.id,
                        confidence=0.6,
                    )

            decision = await ledger.refresh_invoice_state(
                self.session,
                invoice,
                today=self.today,
                grace_days=self.settings.reminder_grace_days,
                reminder_cooldown_days=self.settings.reminder_cooldown_days,
            )
            await record_event(
                self.session,
                workspace_id=self.account.workspace_id,
                owner_id=self.account.user_id,
                event_type="ledger.followup_paused",
                summary=(
                    f"Follow-ups paused for invoice "
                    f"{invoice.invoice_number or invoice.id}: {decision.effective_state}"
                ),
                entity_type="invoice",
                entity_id=str(invoice.id),
                decision=decision.as_dict(),
                source_evidence=[e.as_dict() for e in signals.evidence],
                dedupe_key=audit_key(
                    "ledger.followup_paused",
                    invoice.id,
                    message.gmail_message_id,
                    decision.effective_state,
                ),
            )

    # ----------------------------------------------------------------------------------
    # Run bookkeeping
    # ----------------------------------------------------------------------------------

    async def _load_known_counterparties(self) -> None:
        """Seed known customers so their follow-up mail scores as relevant."""
        rows = (
            await self.session.execute(
                select(Customer.primary_email, Customer.domain).where(
                    Customer.workspace_id == self.account.workspace_id
                )
            )
        ).all()
        self._known_emails = {r[0] for r in rows if r[0]}
        self._known_domains = {r[1] for r in rows if r[1]}

    async def _start_run(self, mode: str) -> SyncRun:
        run = SyncRun(
            workspace_id=self.account.workspace_id,
            gmail_account_id=self.account.id,
            mode=str(mode),
            status=str(SyncStatus.RUNNING),
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def _finish_run(self, run: SyncRun, outcome: SyncOutcome) -> None:
        run.status = outcome.status
        run.finished_at = datetime.now(timezone.utc)
        run.messages_listed = outcome.messages_listed
        run.messages_metadata_fetched = outcome.messages_metadata_fetched
        run.messages_content_fetched = outcome.messages_content_fetched
        run.messages_ignored = outcome.messages_ignored
        run.attachments_processed = outcome.attachments_processed
        run.invoices_upserted = outcome.invoices_created + outcome.invoices_updated
        run.payment_events_upserted = outcome.payment_events_recorded
        run.start_history_id = outcome.start_history_id
        run.end_history_id = outcome.end_history_id
        run.history_fallback_used = outcome.history_fallback_used
        run.error = outcome.error
        run.details = {"notes": outcome.notes}

        # The cursor advances only on success, so a failed run re-reads rather than skips.
        if outcome.status == SyncStatus.COMPLETED and outcome.end_history_id:
            self.account.last_history_id = outcome.end_history_id

        await record_event(
            self.session,
            workspace_id=self.account.workspace_id,
            owner_id=self.account.user_id,
            event_type=f"gmail.sync_{outcome.status}",
            summary=(
                f"{outcome.mode} sync {outcome.status}: "
                f"{outcome.messages_content_fetched} fetched, "
                f"{outcome.messages_ignored} ignored, "
                f"{outcome.invoices_created} new invoices"
            ),
            actor_type=ActorType.SYSTEM,
            entity_type="sync_run",
            entity_id=str(run.id),
            decision=outcome.as_dict(),
            dedupe_key=audit_key("gmail.sync", run.id),
        )
        await self.session.flush()


def _is_pdf(ref: AttachmentRef) -> bool:
    return (ref.mime_type or "").lower() == "application/pdf" or ref.filename.lower().endswith(
        ".pdf"
    )


def _as_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _match_claim_targets(signals, invoices: Sequence[Any], body: str) -> list[Any]:
    """Pick which invoices a payment claim or dispute refers to."""
    from app.services.extraction import find_invoice_number

    number, _ = find_invoice_number(body, "email_body")
    if number:
        matched = [i for i in invoices if (i.normalized_number or "") == number]
        if matched:
            return matched
    if signals.paid_amount_paise:
        matched = [i for i in invoices if i.amount_paise == signals.paid_amount_paise]
        if matched:
            return matched
    # Fall back to every invoice that is still open for this customer.
    return [i for i in invoices if (i.balance_paise or 0) > 0]
