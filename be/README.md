# Topline — Backend (platform & Gmail ingestion)

FastAPI service for **Topline**, a Gmail-native revenue recovery agent.

This README covers the **platform and ingestion half** of the backend: Gmail connection,
finance-relevant mail ingestion, invoice/payment evidence extraction, the receivables
ledger, and Razorpay confirmations. The digest/draft/approval agent lives in
[`app/agent_layer/`](app/agent_layer/) and is owned separately.

---

## The evidence policy

> **Gmail evidence is never presented as guaranteed payment truth.**

An email saying *"we already paid"* is a **claim**, not a settlement. It moves the invoice to
`payment_claimed`, pauses follow-ups, and asks the owner to verify. Only a payment provider
(Razorpay, or an explicit owner override) can produce `confirmed_paid`.

That rule is enforced in three independent places, so no single bug can bypass it:

| Layer | Enforcement |
|---|---|
| Decision engine | `decide_state()` requires `has_provider_confirmation` for `confirmed_paid` |
| Event folding | `fold_payment_events()` downgrades any Gmail row claiming confirmation to a claim |
| Database | `ck_invoices_confirmed_paid_requires_provider`, `ck_payment_events_gmail_never_confirms` |

---

## What is implemented

### Foundation
- FastAPI app with lifespan management, CORS, a request-id middleware, and a safe
  catch-all error handler that never leaks internals into a response.
- `pydantic-settings` configuration; **secrets come from the environment only**.
- Async SQLAlchemy 2.0 over Supabase Postgres (session pooler), with prepared-statement
  caching disabled as the pooler requires.
- Structured JSON logging with per-request correlation ids.
- `GET /api/v1/health` (liveness) and `GET /api/v1/health/ready` (reports every dependency).

### Database
Twelve tables in [`supabase/migrations`](../supabase/migrations), mirrored by
[`app/models.py`](app/models.py):

`workspaces` · `users` · `gmail_accounts` · `oauth_states` · `sync_runs` ·
`source_messages` · `source_attachments` · `customers` · `invoices` · `payment_events` ·
`invoice_source_links` · `activity_log`

Money is stored in **paise as `BIGINT`** — never floats. Row Level Security is enabled and
forced on every table with no permissive policy for `anon`/`authenticated`: the FastAPI
backend is the only way in, and it scopes every query by `workspace_id` (and `owner_id` on
the ledger tables).

### Google OAuth + owner sessions (least privilege)
- Scopes requested: `openid`, `email`, `profile`, `gmail.readonly`, and `gmail.send`.
- `gmail.send` is **provisioned but never exercised here** — nothing in this milestone sends
  mail. Set `ENABLE_GMAIL_SEND_SCOPE=false` to drop it entirely.
- **`gmail.modify` is never requested.** Topline reads mail and (later, behind an approval
  gate) sends new mail; it has no reason to mutate the owner's mailbox. `FORBIDDEN_SCOPES`
  in [`app/config.py`](app/config.py) makes configuring a write scope a startup error.
- Refresh tokens are Fernet-encrypted at rest; plaintext never reaches a column, a log line,
  or an API response. OAuth state is single-use and expires.
- **Connecting Google is signing in.** The callback resolves the owner from the verified
  Google `sub` — a returning identity reuses its workspace; a new one gets a fresh
  `workspace` + `owner` — then mints a Fernet-signed session token
  ([`app/services/session.py`](app/services/session.py)). Every owner-facing route depends
  on `CurrentOwner`; there is no unauthenticated fallback. `X-Owner-Id` is honoured only
  when `ALLOW_OWNER_HEADER=true` and `ENVIRONMENT` is `local`/`test`.
- The token reaches the browser two ways, so both deployment shapes work:
  - an **HttpOnly cookie** — all that's needed when the API and the SPA share an origin
    (the Vite dev proxy, or a Vercel `/api` rewrite). Keep `SESSION_COOKIE_SAMESITE=lax`.
  - the redirect's **URL fragment** (`#s=…`) — for a cross-origin SPA the browser won't
    send the cookie to; the SPA stores it and sends `Authorization: Bearer …`. Point
    `GOOGLE_REDIRECT_URI` straight at this backend in that case.
  Set `SESSION_COOKIE_SECURE=true` in every deployed environment.

### Gmail ingestion
Ingestion is deliberately staged so the mailbox is never bulk-copied:

1. **List** with a server-side finance pre-filter, bounded by a date window.
2. **Metadata fetch** — headers, labels, attachment names. No bodies.
3. **Score** ([`services/relevance.py`](app/services/relevance.py)) as a transparent sum of
   named rules: invoice/payment keywords, Hinglish payment terms, claim/dispute language,
   currency amounts, invoice-like PDF filenames, payment-provider and bank senders, known
   customers, minus penalties for bulk senders and demoted Gmail categories.
4. **Full fetch for candidates only.** Below the threshold, the body is *never downloaded*;
   the message is recorded as `ignored` with its scoring reasons and **no body retained**.
5. **PDF extraction** — embedded text first; OCR only when a page yields almost no text.
   Without an OCR backend, extraction reports `ocr_unavailable` rather than failing the run.
6. **Deterministic extraction** of invoice facts and payment signals, each carrying a
   verbatim snippet and character offsets back into the source.

**Incremental sync** uses the stored Gmail `historyId`. When Gmail rejects it — it expires
after an idle period — the sync falls back to a **date-scoped resync** (`FALLBACK_RESYNC_DAYS`),
not a full mailbox re-read. The cursor advances **only after a run succeeds**, so a crash
re-reads rather than silently skipping messages.

### Invoice states
Seven states, stored as two orthogonal columns so the queue and the money are independent
(this matches the contract the agent layer builds against):

- `invoices.payment_state` → `confirmed_paid` · `likely_unpaid` · `payment_claimed` · `disputed` · `needs_information`
- `invoices.reminder_state` → `ready_for_reminder` · `paused`

`effective_state` collapses them into the single seven-valued label the API returns.
Precedence, highest first: `confirmed_paid` → `disputed` → `payment_claimed` →
`needs_information` → `paused` → `ready_for_reminder` → `likely_unpaid`.

`paused` means something deliberately holds the invoice (an owner pause, `paused_until`).
An invoice that is merely not actionable yet — not due, or inside the reminder cooldown —
reads as `likely_unpaid`, which is the honest default the product depends on.

### Idempotency
Re-running any sync converges instead of duplicating. Every entity has a natural key:

| Entity | Key |
|---|---|
| source message | `(gmail_account_id, gmail_message_id)` |
| invoice | `(workspace_id, dedupe_key)` — customer + invoice number, or customer + amount + issue date |
| evidence link | `(invoice_id, link_type, evidence_hash)` |
| payment event | `(workspace_id, provider, provider_event_id)` |
| audit row | `(workspace_id, dedupe_key)` |

### Razorpay (optional confirmation source)
- `POST /api/v1/webhooks/razorpay` verifies `X-Razorpay-Signature` with a constant-time
  HMAC-SHA256 compare **before** the body is trusted. There is no skip-verification path.
- Idempotent on the Razorpay event id — retries are the normal case, not an error.
- Returns `200` for duplicates and for events we intentionally ignore, so Razorpay stops
  retrying; `4xx` only when the request itself is untrustworthy.
- Reconciliation matches strongest-first (Razorpay invoice id → payment id →
  `notes.invoice_number` → customer email + exact amount) and **refuses to guess**: an
  ambiguous match is left unreconciled for the owner rather than marking the wrong invoice paid.
- Unmatched events are retained; `POST /api/v1/sync/razorpay/reconcile` retries them once
  the invoice has been extracted from Gmail.

---

## Intentionally deferred

Owned by the agent developer (`app/agent_layer/`), not this milestone:
digest generation and sending, natural-language owner-command parsing, Gemini draft
generation, the approval workflow and guarded send, customer-reply classification, and the
`digests` / `drafts` / `review_tasks` tables.

Deferred by design:

- **No mail is sent.** `gmail.send` is provisioned; the send path is the agent layer's.
- **Auth is session-cookie only.** The Google OAuth callback establishes the session
  (see above). Supabase Auth (`SUPABASE_JWT_SECRET`, `users.supabase_user_id`) is still
  unused — a future option if password/MFA login is wanted. DB-enforced RLS policies
  (connecting as `authenticated` with per-request JWT claims) are also a future
  hardening step; today isolation is enforced in the query layer with deny-all RLS as
  the "no PostgREST" backstop.
- **OCR needs opt-in system packages** (`pytesseract` + `tesseract-ocr`, and a rasteriser).
  Without them, scanned PDFs are flagged `ocr_unavailable`, never silently dropped.
- **No Gmail push/PubSub** — polling is sufficient for the demo.
- **No scheduler wired here.** Manual endpoints exist for every job.
- LLM-assisted extraction: [`InvoiceFactExtractor`](app/services/extraction.py) is the
  protocol the Gemini extractor plugs into. The deterministic pass always runs first.

---

## Setup

```bash
cd be
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt   # or requirements.txt for runtime only

cp .env.example .env
.venv/bin/python -m app.keygen        # paste into TOKEN_ENCRYPTION_KEY
```

Fill in the **required** variables in `.env`: `DATABASE_URL` (Supabase **session pooler**
URI), `TOKEN_ENCRYPTION_KEY`, and — to connect a mailbox — `GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET`. Every variable is documented inline in
[`.env.example`](.env.example).

### Google Cloud setup
1. Enable the **Gmail API**.
2. Create an **OAuth client ID** (Web application).
3. Add `http://localhost:8000/api/v1/auth/google/callback` as an authorised redirect URI.
4. Add the owner and customer Gmail accounts as **OAuth test users**. Test-mode
   authorisation expires after seven days — reconnect if a sync starts returning 401s.

### Migrations

```bash
supabase db push                       # with the Supabase CLI, or:
.venv/bin/python scripts/migrate.py    # applies pending files, tracked in schema_migrations
.venv/bin/python scripts/migrate.py --status
```

Migrations are versioned SQL in [`supabase/migrations`](../supabase/migrations) and apply in
filename order. `scripts/migrate.py` records a checksum per file and warns if an applied
migration is edited afterwards.

### Run

```bash
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- OpenAPI JSON: <http://localhost:8000/openapi.json>

### Connect a mailbox and ingest

Sign in through the dashboard (`npm run dev`, then "Continue with Google") so the browser
holds a session cookie. For raw `curl` against a local run, set `ALLOW_OWNER_HEADER=true`
and pass the owner id:

```bash
curl -X POST localhost:8000/api/v1/auth/google/start     # open authorization_url in a browser
OWNER=$(curl -s localhost:8000/api/v1/auth/session -H "X-Owner-Id: <owner-uuid>" | jq -r .user.id)
curl -X POST localhost:8000/api/v1/sync/backfill -H "X-Owner-Id: $OWNER" -H 'Content-Type: application/json' -d '{"months": 12}'
curl "localhost:8000/api/v1/invoices?state=ready_for_reminder" -H "X-Owner-Id: $OWNER"
curl localhost:8000/api/v1/messages -H "X-Owner-Id: $OWNER"   # includes ignored mail, with reasons
```

---

## Endpoint inventory

Full schemas are published at `/openapi.json` and rendered at `/docs`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/health` | Liveness; does not touch the database |
| `GET` | `/api/v1/health/ready` | Readiness: database, token encryption, Google, Razorpay |
| `POST` | `/api/v1/auth/google/start` | Begin OAuth; returns the consent URL + single-use state |
| `GET` | `/api/v1/auth/google/callback` | OAuth redirect target; stores tokens, sets the session cookie |
| `GET` | `/api/v1/auth/session` | Current owner + workspace; `401` when not signed in |
| `POST` | `/api/v1/auth/logout` | Clear the session cookie |
| `GET` | `/api/v1/auth/connection` | Gmail/Razorpay connection status |
| `DELETE` | `/api/v1/auth/connection/{account_id}` | Disconnect; discards tokens, retains the ledger |
| `POST` | `/api/v1/sync/backfill` | Backfill finance-relevant Gmail history |
| `POST` | `/api/v1/sync/incremental` | Incremental sync via `historyId`, with scoped fallback |
| `GET` | `/api/v1/sync/runs` | Recent sync runs and their counters |
| `POST` | `/api/v1/sync/razorpay/reconcile` | Retry unmatched Razorpay events |
| `POST` | `/api/v1/webhooks/razorpay` | Razorpay webhook receiver (signature verified) |
| `GET` | `/api/v1/customers` | List customers |
| `GET` | `/api/v1/customers/{id}/dossier` | Invoices + evidence + recent correspondence |
| `GET` | `/api/v1/invoices` | List invoices; filter by `state` and `customer_id` |
| `GET` | `/api/v1/invoices/{id}` | One invoice with its full evidence trail |
| `GET` | `/api/v1/invoices/{id}/source` | Attachments behind an invoice |
| `GET` | `/api/v1/ledger/summary` | Totals and a per-state breakdown |
| `GET` | `/api/v1/messages` | Source messages, **including ones deliberately ignored** |
| `GET` | `/api/v1/activity` | Audit trail, newest first |

Agent-layer routes mount under `/api/v1/agent/*` when that module is importable. The mount is
defensive: ingestion and the ledger keep serving even if the agent side is unavailable.

---

## Tests

```bash
cd be
.venv/bin/python -m pytest                    # everything
.venv/bin/python -m pytest -v                 # per-test names
.venv/bin/python -m pytest tests/test_gmail_ingest.py
```

Tests run against in-memory SQLite with a fake Gmail transport and committed invoice PDF
fixtures — no network, no credentials, no Postgres required.

| File | Covers |
|---|---|
| `test_relevance.py` | Scoring; **unrelated mail is ignored**; newsletters and spam excluded |
| `test_extraction.py` | Amounts in paise, dates, invoice numbers, PDF text, OCR fallback, claims/disputes, quoted-reply stripping |
| `test_decisions.py` | All seven states; **Gmail can never confirm payment**; precedence; cooldown |
| `test_gmail_ingest.py` | Backfill → ledger; evidence traceability; idempotency; claims pausing follow-ups |
| `test_incremental_sync.py` | `historyId` sync; expiry → scoped resync; cursor only advances on success |
| `test_razorpay.py` | Signature verification, reconciliation strategies, replay idempotency, refunds |
| `test_api.py` | Health, OpenAPI inventory, ledger reads, webhook HTTP behaviour |
| `test_auth_session.py` | Session cookie required; **one owner never sees another's ledger** |
| `test_link_account.py` | One workspace per Google identity; additive mailbox linking |
| `test_security.py` | Token encryption at rest, key rotation, scope guard |
| `test_schema_parity.py` | Models vs SQL migration drift; every enum value has a CHECK constraint |

The suite includes the required negative case: an unrelated personal email
(*"Team lunch on Friday?"*) produces no customer, no invoice, **no body download**, and an
`gmail.message_ignored` audit entry recording why.

### Verified against real Postgres

Beyond the SQLite suite, the migrations were applied to a throwaway Postgres 16 container
and the safety constraints exercised directly. Confirmed rejected: `confirmed_paid` with
Gmail-only evidence, a Gmail payment event flagged as a confirmation, duplicate invoice
dedupe keys, duplicate Razorpay event ids, and invalid state values. Confirmed accepted:
provider-confirmed payment and a first-delivery Razorpay confirmation. The API was also
booted against that database and a signed webhook replayed twice, producing one
`payment_events` row and one audit row.
