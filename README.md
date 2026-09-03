# Topline

Topline is a Gmail-based assistant that helps a small business owner get paid on time — without ever emailing a customer on its own.

---

## 1. What Topline Is

### Who this is for

A small business owner who sells on credit — sends an invoice, then waits (and hopes) to get paid. Usually this means Gmail, spreadsheets, and memory. There is no dedicated finance team to chase every unpaid invoice.

### The problem

- Invoices and payment proof live scattered across an inbox — some as emails, some as PDF attachments.
- It is easy to forget who still owes money, and easier still to forget to follow up.
- When a customer replies "already paid," it is tempting to just believe them and close the invoice — but without real proof, that can mean writing off money that was never actually received.
- Chasing payments by hand is repetitive and easy to keep putting off.

### The solution

Topline connects to the owner's Gmail (with permission) and quietly reads only the finance-relevant messages — invoices, payment confirmations, and customer replies about money. From that, it keeps one running, trustworthy record of who owes what, and how overdue it is.

Every day, Topline looks at which invoices genuinely need a nudge and prepares a polite reminder email for each one. But it never sends anything by itself. The owner always sees the complete message first, can edit it, and must explicitly approve it before it goes out. Nothing leaves the business's name without a human saying yes.

And when it comes to money: an email that says "I already paid" is treated as a claim, not proof. Topline pauses that invoice and asks the owner to double check, rather than quietly marking it settled. Only a real payment confirmation — from a payment provider or the owner themselves — can mark an invoice as actually paid.

### What Topline deliberately does not do

- It does not send a customer email without the owner's explicit approval.
- It does not treat "I paid you" over email as proof of payment.
- It does not need customers to sign up for anything — they simply keep emailing the business as usual.
- It does not hide uncertainty. Disputes, payment claims, and failures are shown clearly, not buried.

---

## 2. The Technical Approach

### The big picture

Topline has three parts:

1. **A backend service** that connects to Gmail and a payment provider, reads the relevant evidence, and keeps a database of customers, invoices, and payments.
2. **A small database** that stores that evidence and every decision made about it, so nothing is guessed twice and nothing is lost.
3. **A web dashboard** the owner opens to see totals, review reminder drafts, approve or reject them, and see a plain history of what happened and why.

An AI language model is used in a few narrow, supervised places to help write reminder emails and read plain-English instructions — never to decide what counts as "paid," and never to send anything by itself. More on this in [section 4](#4-the-agent-explained).

### Reading Gmail without overreaching

Topline does not download someone's whole mailbox. It works in careful stages:

1. **List.** It asks Gmail for a list of recent messages, narrowed by a basic finance-related filter and a date range.
2. **Peek, don't open.** For each message it first looks only at the sender, subject, and labels — not the full email body.
3. **Score.** A simple, transparent point system decides whether a message looks finance-relevant: invoice or payment wording, currency amounts, invoice-shaped PDF names, known payment senders, and so on. Newsletters and bulk mail lose points.
4. **Only then, read the rest.** Only messages that cross the relevance threshold have their full body and attachments fetched. Everything else is logged as "ignored," along with the reason, and its content is never stored.
5. **Read invoices out of PDFs.** For finance-relevant attachments, Topline first tries to read the embedded text; if a page is a scanned image with almost no text, it falls back to reading the image itself. If neither is possible, it says so honestly instead of guessing.
6. **Pull out the facts.** From the surviving text, Topline extracts concrete details — invoice number, amount, due date, and any payment-related statement — and keeps a verbatim snippet of exactly where each fact came from, so every number can be traced back to its source email.

Money amounts are always stored as whole smallest-unit numbers (paise, i.e. 1/100 of a rupee) rather than decimals, which avoids the rounding mistakes that come from using regular decimal math for currency.

### The ledger: a rulebook, not a guess

Every invoice moves through a small, fixed set of states, decided by plain code rules — not by the AI model:

| State | Meaning |
|---|---|
| `likely_unpaid` | Nothing has flagged this invoice; it is simply not due for a reminder yet, or waiting its turn |
| `ready_for_reminder` | Overdue, unpaid, and safe to remind about |
| `payment_claimed` | A customer said they paid, but there is no proof yet — follow-ups are paused |
| `disputed` | A customer pushed back on the invoice itself — follow-ups are paused |
| `needs_information` | Something about the invoice is unclear or incomplete |
| `paused` | The owner (or a cooldown period) is deliberately holding this back |
| `confirmed_paid` | Actually settled, backed by real proof |

**The one rule that matters most: an email can never move an invoice to `confirmed_paid`.** Only a genuine payment confirmation (from a payment provider's records, or an explicit manual override by the owner) can do that. This rule is not just a suggestion in one place — it is checked three separate times, independently:

1. In the code that decides state changes.
2. In the code that processes incoming payment-related events, which automatically downgrades anything coming from Gmail to a mere "claim."
3. In the database itself, which physically refuses to save a row that breaks the rule — so even a bug elsewhere in the code cannot slip past it.

This "three independent checks" approach means one mistake in one place is not enough to wrongly mark someone as paid.

### Confirming payment for real

Topline can also connect to a payment provider used for collecting payments in India. When that provider reports a successful payment, Topline verifies the message really came from the provider (using a signed, tamper-proof check) before trusting it, then tries to match it to the right invoice — by invoice reference, then payment reference, then customer email plus exact amount, in that order. If it cannot match confidently, it leaves the invoice as it was rather than guessing which invoice to mark paid.

### Keeping a paper trail

Every meaningful step — a message ignored, an invoice's state changing, a draft being created, an approval, a send, a failure — is written to an activity log with a timestamp, the reason, and a reference back to the original evidence. The dashboard's "Activity" screen is simply this log, made readable. Nothing happens invisibly.

### The daily cron job

Topline doesn't wait for the owner to open the dashboard to notice an overdue invoice. A **cron job** — a task that runs automatically on a fixed schedule, with no one needing to click anything — checks every connected business once a day, at 9 AM Indian time, and builds that day's digest: the list of invoices that are genuinely due for a reminder, worked out from the ledger.

A few things keep this safe and boring, on purpose:

- **It only prepares, never sends.** The cron job builds the digest and, from there, follows the exact same drafting and approval steps described in [section 4](#4-the-agent-explained). It has no shortcut that skips the owner's approval.
- **It's off by default.** A single setting turns it on; if it fails to start for any reason, the rest of the app keeps running normally, and the owner can still trigger the same daily check by hand at any time.
- **Only one copy runs at a time.** Even if the backend is running as several instances, the schedule enforces a single active run, so the same digest is never built — or sent — twice by accident.
- **A missed run catches up, once.** If the server happened to be down at 9 AM, the job is still allowed to run within about an hour afterwards. Past that window it simply waits for the next day, rather than firing a stale reminder late.

---

## 3. Flows

### User flow — what the owner actually does

```
1. Connect
   Sign in with Google so Topline can read (not send from) the business Gmail account.
   Optionally connect the payment provider used to collect customer payments.
        │
2. Import history
   Topline reads past finance-relevant emails once, to build the starting ledger.
        │
3. Check the dashboard (takes about a minute)
   - Total amount outstanding, and how many customers owe it
   - Anything that needs a decision (a payment claim or a dispute)
   - How many reminder drafts are waiting for approval
        │
4. Review a draft
   Open the approval queue and read each reminder email in full —
   subject, body, and the reasoning behind it.
        │
5. Decide
   Edit the wording if needed → Approve → Send
   or → Reject if it should not go out at all.
        │
6. Stay aware
   Watch the activity log for anything Topline flagged on its own:
   a customer reply, a send failure, an unclear instruction.
```

Nothing in steps 3–6 requires the owner to leave Gmail language behind — the daily summary and drafts can also be reviewed and approved by simply replying in plain English (for example, "send 1 and 3" or "skip 2"), because the same actions are available by email, not only through the dashboard.

### Technical flow — what the system does behind the scenes

```
Gmail inbox
    │  (scheduled or manual sync)
    ▼
Ingestion service — list → peek at headers → score relevance
    │
    ├── below threshold → logged as "ignored", nothing stored
    │
    └── above threshold → fetch full email + attachments
            │
            ▼
       Extraction — pull invoice number, amount, dates, payment wording
       (from email text and PDFs, always keeping the original snippet)
            │
            ▼
       Decision engine (plain code, no AI) — decide payment_state
       and reminder_state for the invoice, following the fixed rulebook
            │
            ▼
       Ledger (database) — one row per customer, invoice, and event,
       each traceable back to its source email or payment record
            │
            ▼
Daily cycle — triggered automatically by the cron job (or run by hand);
picks every invoice that is genuinely due for a reminder
            │
            ▼
AI-assisted drafting — write the reminder text using only that
customer's own invoice evidence, with a citation for every claim made
            │
            ▼
   confidence too low, or citing something outside the evidence?
            │
      ┌─────┴─────┐
      │             │
   yes → discard,   no → hold as a
   flag for review   pending draft
                          │
                          ▼
                 Owner reviews full text in the dashboard (or by email)
                          │
                          ▼
                 Owner edits (optional) → approves
                          │
                          ▼
                 Send only if: approved, and the approved text
                 still matches exactly what is about to be sent
                          │
                    ┌─────┴─────┐
                    │             │
                 success       failure
                    │             │
              logged as sent   logged + flagged for
              + invoice's      manual review, owner
              reminder marked  notified — never
              as sent          silently retried
                          │
                          ▼
              Customer reply arrives later
                          │
                          ▼
              Reply classified (already paid? dispute? question?)
                          │
              "already paid" or "dispute" → invoice paused
              automatically, owner notified — Topline never
              decides that dispute on its own
```

---

## 4. The Agent, Explained

"The agent" is the part of Topline that turns a plain ledger into a day's worth of ready-to-send reminder drafts, and that can understand a short plain-English instruction from the owner. It is best understood as a small coordinator, not a decision-maker: it chains a few steps together, but every step that matters for money or for what reaches a customer's inbox is checked before it is allowed to continue.

### What it actually does, in three jobs

**1. Builds the daily digest.**
Once a day — woken up automatically by the [cron job](#the-daily-cron-job), or triggered by hand — it looks at the ledger and picks out exactly the invoices the rulebook (see section 2) already marked as due for a reminder; it does not invent new candidates. It groups them by customer, explains in plain terms why each one qualifies (how much is owed, how overdue it is, whether reminders were already sent before), and sends the owner a short summary. The owner can reply in plain English — "send 1 and 3," "skip 2," "draft item 4, firmer tone" — and the AI model's only job here is to turn that sentence into a structured instruction (which item, which action, how confident it is that it understood correctly).

**2. Drafts the reminder email.**
For each invoice the owner asks Topline to draft, the AI model is given only that one customer's own evidence — their invoices, their payment history, their prior reminders — and asked to write a short, appropriately toned reminder email, citing which piece of evidence backs each fact it states. Nothing outside that customer's own file is visible to it, so it cannot borrow facts from someone else's invoice by mistake.

**3. Reads customer replies.**
When a customer replies to a reminder, the AI model classifies the reply's intent: already paid, disputing the invoice, asking a question, promising to pay, or unclear. A couple of the most sensitive cases — "already paid" and disputes — are actually caught by a simple keyword check first, before the AI model is even asked, so the most important safety behaviour does not depend on an AI call succeeding.

### The guardrails around it

The agent is deliberately built to be cautious rather than clever:

- **It never invents the invoice list.** It only acts on what the deterministic rulebook already decided was due.
- **It cannot cite evidence it wasn't given.** If a draft's reasoning refers to something outside that customer's own file, the draft is thrown away and flagged for a human to look at, rather than shown to the owner.
- **Low confidence means it stops, not guesses.** Every AI decision comes with a confidence score. Below a set threshold — or if the instruction is genuinely ambiguous — Topline does not act on it. Instead, it creates a task asking the owner to clarify, and explains why it stopped.
- **It fails safe, not silent.** If the AI model itself errors out or times out, that is treated the same as "not confident" — the affected invoice is paused for human review, never guessed forward.
- **It cannot mark anything as paid.** Payment status is entirely the job of the rulebook and the payment provider (section 2) — the agent has no path to change it.
- **It cannot send.** Sending a customer email requires a separate, explicit approval step, and the exact text approved must still match the text being sent — if the draft changed after approval, it must be approved again.
- **Everything it does is logged**, along with the evidence it used and its confidence, so any decision can be traced back and understood later.

In short: the agent is trusted to notice, summarise, and draft — never to decide or to act unsupervised on anything customer-facing or money-related.

---

## 5. Security & Privacy

Reading someone's inbox and touching money means the boring parts have to be solid. What actually protects the owner's data:

- **Least access, on purpose.** When Gmail is connected, Topline only ever asks for permission to read mail and to send mail — never permission to delete, relabel, or otherwise edit what's already in the inbox. That broader permission is deliberately never requested, and the app refuses to even start up if it's ever accidentally configured to ask for it.
- **Login keys are encrypted, not stored in plain text.** The tokens that keep Topline connected to Gmail are encrypted before they're saved. Reading the database directly would not hand over a usable login.
- **Secrets never reach the browser.** API keys, encryption keys, and provider credentials all live on the server. The dashboard the owner's browser loads never receives or stores anything that a malicious script could steal.
- **The database has no side door.** Direct outside access to the data is switched off entirely at the database level — the backend service is the only path in. Even if some other tool were pointed at the database, there is no open door for it to read through.
- **Payment confirmations are verified, not trusted blindly.** A message claiming an invoice was paid is checked with a signed, tamper-evident verification before Topline trusts it — a look-alike request cannot be used to falsely mark something paid.

None of this is about being clever. It's about making sure a mistake in one place can't quietly turn into lost money or a leaked inbox somewhere else.

## 6. Where Things Stand Today

Topline is a working product, not a finished one — being upfront about that:

- **One connected business at a time, for now.** Proper multi-business sign-in, and the database-level wall that keeps one business's data from ever being reachable by another, are both designed in but not switched on yet. This should land before more than one business genuinely shares the same deployment.
- **Scanned PDFs need an extra, optional step.** Reading text out of a scanned (image-only) invoice PDF needs an additional component installed. Without it, Topline is honest about the gap — it flags the attachment as unreadable rather than guessing at numbers it can't actually see.
- **Gmail is checked on a schedule, not instantly.** New mail is picked up periodically (and automatically, once a day, by [the cron job](#the-daily-cron-job)) rather than the moment it arrives. A newly received invoice-relevant email may take a little while to show up.
- **The daily digest runs on one fixed schedule.** Every connected business is checked at the same hour; there's no per-business timing yet.

Each of these is a deliberate, known trade-off for where the product is today, not an accident — and each one is designed to fail safely, by asking a human, rather than silently doing the wrong thing.

---

## Project layout

```
be/    FastAPI backend — Gmail ingestion, the ledger, payment reconciliation, and the agent layer
fe/    React + TypeScript dashboard — the owner's control panel
supabase/migrations/   Versioned database schema
```

Setup and run instructions for each half live in their own READMEs: [`be/README.md`](be/README.md) for the backend, [`fe/README.md`](fe/README.md) for the dashboard.
