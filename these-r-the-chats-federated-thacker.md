# Topline — Gmail-Native Revenue Recovery Agent

## Context

**Why this exists.** Topline solves a real inbox problem for small businesses: invoices, payment confirmations, and customer replies are scattered across Gmail, so nobody has a trustworthy daily view of what is overdue or what should be followed up next.

The owner connects their existing Gmail account. Topline reads relevant historical and future business correspondence, extracts invoice and payment evidence from emails and attached PDFs, and builds a customer-specific receivables workspace. It then sends the owner one daily digest and turns their natural-language reply into safe, reviewable follow-up drafts.

**The core owner experience:**
1. At 9:00 AM, Topline emails the owner a daily list of likely-unpaid or overdue invoices.
2. The owner replies in plain English or Hinglish: `Acme ko firm bhejo, Bharat ko abhi chhod do`.
3. Topline retrieves Acme's relevant invoices and email context, drafts the branded reminder, and replies in the **same digest thread** with numbered drafts.
4. The owner edits or replies `send 1` / `send all`. Only then does Topline email a customer.

**What makes this an agent, not an email generator.** It turns unstructured inbox history into a source-backed invoice ledger, keeps a per-customer dossier, reasons over payment/dispute evidence, explains its recommendation, and stops follow-ups when payment or a customer claim requires review.

**Scope boundary.** Historical Gmail reading is in scope because it creates the initial invoice/customer context. Broad personality modelling, a vector database, BCC ingestion, and full autonomous collections are not. Topline stores extracted finance facts and relevant source messages; it does not indiscriminately copy a mailbox.

**Repo state.** `/home/asus/Desktop/Code/topline` is greenfield. `be/` and `fe/` are the backend and frontend roots. `KernelPulse-main` in the additional working directory is unrelated and must be ignored.

## Decisions taken

| Area | Decision |
|---|---|
| Primary user | Finance manager / business owner |
| Owner interaction | Daily Gmail digest first; dashboard is a supporting review surface |
| Mail plumbing | Gmail API + Google OAuth; historical backfill plus incremental sync |
| Invoice/context source | Gmail threads and attached invoice PDFs |
| Payment truth | Razorpay test-mode invoice sync + webhooks when available; otherwise show `likely_unpaid` rather than claim certainty |
| Database/auth | Supabase Postgres + Supabase Auth; versioned SQL migrations in `supabase/migrations` |
| Send gate | Always require explicit owner approval |
| AI | Gemini Flash model configured by `GEMINI_MODEL`, backend-only, structured JSON outputs |
| Frontend | Vite + React + TypeScript dashboard in `fe/` |

---

## The loop being built

```
 Gmail historical backfill ──▶ finance-relevant messages + invoice PDFs
        │                                  │
        │                                  ▼
        │                         invoice/customer ledger
        │                                  │
 Razorpay test events ─────────▶ confirmed payment evidence
        │                                  │
  Gmail incremental sync ──────────────────┘
        │
   [CRON 09:00 IST]
        │
        ▼
  DIGEST email ──────────────▶ OWNER inbox
   "3 need review: Acme ₹40k/32d, Bharat ₹12k/8d, Nova ₹5k/61d"
        │
 owner replies: "acme ko firm, nova ko final, bharat chhod do"
        │
  inbound router ─▶ OWNER COMMAND ─▶ validated actions + customer dossiers
        │
 Gemini drafts reminders ─▶ same digest thread: numbered pending drafts
        │
 owner replies: "send 1,2" / "skip 3" / inline edit
        │
 approval gate ─▶ branded customer email sent from connected Gmail
        │
 customer replies "already paid" / "next week" / "invoice galat hai"
        │
 inbound router ─▶ classify, pause unsafe follow-ups, create owner-review draft
```

Every arrow into a customer's inbox passes through the approval gate. A payment claim, dispute, low-confidence match, or missing critical invoice fact pauses automated follow-up.

---

## Stack

- **Backend** `be/` — FastAPI, SQLAlchemy 2.0 async, Pydantic, APScheduler, Python 3.11+
- **Database/Auth** — Supabase Postgres and Supabase Auth. Keep schema changes in versioned SQL migrations under `supabase/migrations`; FastAPI connects through the Supabase session pooler.
- **Mail** — Google OAuth via Authlib and Gmail API via `google-api-python-client`. Request `gmail.readonly`, `gmail.send`, `openid`, `email`, and `profile` only.
- **Invoices/PDFs** — Gmail message/attachment parsing with PyMuPDF or pypdf; OCR is a later fallback only for scanned fixtures.
- **Payments** — Razorpay Python SDK and verified webhooks, using one backend-held test-mode credential for the demo.
- **LLM** — Google Gen AI SDK. Use the Flash text model named by `GEMINI_MODEL`; validate every response against Pydantic schemas.
- **Frontend** `fe/` — Vite, React, TypeScript, Tailwind CSS, React Router, TanStack Query.
- **Deployment** — Vercel frontend, Render API, Supabase data/auth.

---

## Data model

Use `owner_id` / `workspace_id` throughout so the demo is single-owner now without blocking a later multi-tenant build.

| Table | Purpose / key columns |
|---|---|
| `owners` | `email`, `name`, `gmail_address`, encrypted Gmail refresh token, latest Gmail history id |
| `customers` | owner, name, email, phone, customer confidence/match metadata |
| `source_messages` | Gmail message/thread ids, sender/recipients, subject, relevant body, source date |
| `source_attachments` | message id, PDF metadata, extracted text, extraction status |
| `invoices` | customer, invoice number, amount in paise, dates, payment state, confidence, source message/attachment, Razorpay id when matched |
| `payment_events` | invoice, provider (`gmail` or `razorpay`), provider event id, amount, observed date, evidence payload |
| `email_threads` | Gmail thread id, kind (`digest`, `reminder`), customer and digest links |
| `digests` | owner, run date, Gmail thread id, customer count, total outstanding |
| `drafts` | customer, invoice ids, digest/reminder thread, kind, subject, body, tone, rationale, status, review flags |
| `activity_log` | append-only owner-visible record of every sync, decision, draft, send, reply, and pause |

### Required invoice states

- `confirmed_paid`: Razorpay or unambiguous payment evidence confirms payment.
- `likely_unpaid`: an issued invoice exists but payment is not confirmed.
- `payment_claimed`: customer says payment was made; pause reminders pending owner verification.
- `disputed`: hard stop; no reminder or reply is sent without owner action.
- `needs_information`: amount, due date, or customer match is missing.
- `ready_for_reminder` / `paused`: operational states for the daily queue.

---

## Backend layout (`be/`)

```
be/
  app/
    main.py              FastAPI app, lifespan starts scheduler, CORS
    config.py            pydantic-settings; secrets only from environment
    db.py                async engine + session factory
    models.py            SQLAlchemy models
    schemas.py           Pydantic request/response and AI result schemas
    api/
      auth.py            Google OAuth start/callback and current owner
      customers.py       customers, invoices, customer dossier
      drafts.py          draft list, edit, approve, reject
      sync.py            Gmail historical/incremental sync and Razorpay reconciliation
      ops.py             manual daily cycle, inbox poll, activity feed
    services/
      gmail.py           OAuth credentials, backfill, history polling, MIME sending
      extraction.py      email/PDF text extraction and structured invoice facts
      ledger.py          customer matching, invoice/payment upserts, confidence states
      razorpay_sync.py   test-mode invoice sync and payment webhook reconciliation
      digest.py          daily review list and same-thread digest/draft replies
      inbound.py         deterministic sender/thread router
      decisions.py       dispute, payment-claim, missing-data and pause rules
      approvals.py       email/dashboard approval and shared guarded send
      agent.py           Gemini structured-output calls
      scheduler.py       09:00 IST cycle and polling registration
  requirements.txt
  .env.example
supabase/
  migrations/
```

### Gmail ingestion (`services/gmail.py`)

- **Initial backfill:** query the owner-selected history range (default: 12 months), retrieve messages and PDF attachments, then retain only finance-relevant messages and extracted records.
- **Incremental sync:** persist Gmail's `historyId` only after a successful sync. Poll `history.list` for new messages; on `404`, run a full relevant re-sync.
- **Routing:** Topline only handles threads it created or messages matched to a known invoice/customer. Unrelated inbox mail is ignored and logged.
- **Sending:** build HTML and text MIME email, preserve `In-Reply-To` and `References`, and use the digest's Gmail thread id for owner-facing draft/approval messages.
- **Demo constraint:** add owner and customer Gmail accounts as Google OAuth test users. Test-mode authorization expires after seven days, so re-connect if necessary.

### Decision engine (`services/decisions.py`)

Run deterministic checks before calling Gemini:

1. `confirmed_paid` → stop all future reminders.
2. `payment_claimed` or `disputed` → pause and request owner review.
3. Missing critical facts → `needs_information`; do not draft.
4. Otherwise calculate delay, remaining balance, previous reminder count, and recent customer conversation before marking an invoice `ready_for_reminder`.

### Agent (`services/agent.py`)

Gemini is a bounded structured-data service, not an autonomous actor:

| Call | Input | Output |
|---|---|---|
| `extract_invoice_facts` | relevant email/PDF text | invoice/customer/payment candidates with evidence and confidence |
| `parse_owner_command` | digest customer list + owner's reply | validated customer actions, tone, and notes |
| `draft_reminder` | customer dossier + allowed action | subject, body, rationale |
| `classify_customer_reply` | new customer reply + invoice context | intent, confidence, review flag, suggested draft |

Validate customer/invoice references against the database. Strip quoted email history before classifying a new reply. Gemini never decides to send, marks an invoice paid, or bypasses deterministic safety checks.

### Approvals (`services/approvals.py`)

- Owner commands create pending drafts and post them as numbered sections in the **same daily digest thread**.
- The owner can reply `send all`, `send 1,3`, `skip 2`, or edit a numbered draft. The dashboard offers the same approve/edit/reject controls.
- Both routes call the same `send_draft()` function: render the selected branded template, send through Gmail, update the draft status, create/update the customer reminder thread, and append an audit event.
- Customer replies always create a pending owner-review item. A payment claim or dispute pauses that customer's reminders immediately.

### Scheduler (`services/scheduler.py`)

| Job | Cadence |
|---|---|
| Gmail incremental sync | every 60 seconds in the demo; manual endpoint always available |
| Razorpay reconciliation | every 30 minutes and before the daily cycle |
| Daily digest | 09:00 Asia/Kolkata |

Expose manual endpoints for every job so the stage demo never waits for the clock.

---

## Frontend (`fe/`)

1. **Connect & brand** — Gmail connection, Razorpay test-mode status, business name/logo, and branded email-template preview.
2. **Daily queue** — likely unpaid/overdue customers, amount, days overdue, source evidence, confidence, and current action state.
3. **Customer dossier** — invoice ledger, relevant email/PDF evidence, recent customer conversation, payment history, and decision rationale.
4. **Approval queue** — draft editor, branded preview, approve/reject/send controls, and prominent review badges for payment claims/disputes.
5. **Activity** — reverse-chronological audit trail for ingestion, extraction, digest, command, draft, approval, send, reply, payment, and pause events.

The dashboard supports the email-native experience; it does not replace it.

---

## Build order

| Phase | Deliverable |
|---|---|
| 0 | Scaffold `be/`, `fe/`, Supabase migrations, `.gitignore`, `.env.example`, auth shell |
| 1 | Gmail OAuth → historical relevant-message/PDF backfill → invoice/customer ledger → dashboard evidence view |
| 2 | Incremental Gmail sync, deterministic payment/dispute/missing-data states, manual daily-cycle endpoint |
| 3 | Daily digest emailed to owner and rendered in the queue |
| 4 | Gemini extraction, owner-command parsing, customer-specific draft creation in the same digest thread |
| 5 | Email/dashboard approval, branded send, customer-reply routing, pause logic, complete audit trail |
| 6 | Razorpay test-mode sync/webhooks for confirmed payment state; final visual polish and demo fixtures |

Phases 1–5 form the distinctive Gmail-native agent. Razorpay strengthens payment certainty without replacing the core product.

---

## End-to-end verification

1. Connect the owner Gmail test account and backfill test invoice emails/PDFs; verify only relevant messages produce ledger rows.
2. Verify an invoice without payment evidence is shown as `likely_unpaid`, not definitively unpaid.
3. Run the daily cycle; verify the digest lands in the owner inbox and a matching dashboard queue appears.
4. Reply in Hinglish: `acme ko firm bhejo, nova ko final notice, bharat ko abhi chhod do`; verify valid pending drafts appear in the same digest thread.
5. Edit or approve one draft by email and another in the dashboard; verify each uses the branded template and no unapproved draft is sent.
6. From the customer test inbox, reply `already paid on the 3rd`; verify Topline pauses reminders, creates an owner-review item, and sends nothing automatically.
7. Deliver a Razorpay test payment/webhook; verify the invoice becomes `confirmed_paid` and all future reminders stop.
8. Send unrelated mail to the owner inbox; verify it is ignored and logged as such.
9. Re-run historical/Gmail/Razorpay syncs; verify all upserts are idempotent and audit entries remain traceable.

---

## Deliberately not built now

- Personality modelling from the whole mailbox.
- Vector database/RAG over every email.
- BCC-based ingestion as a replacement for Gmail OAuth.
- Gmail push/PubSub; polling is sufficient for the demo.
- Autonomous sending, money movement, bank write access, broad collections escalation, multi-tenant onboarding, and production OAuth verification.

The result is a credible hackathon product: Topline does not merely write reminders; it finds invoice evidence in a real inbox, builds customer context, explains its decisions, and keeps humans in control of every customer-facing action.
