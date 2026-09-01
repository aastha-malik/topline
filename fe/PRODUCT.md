# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Delegated from the approved migration plan: Vite, React, and TypeScript in the standalone `fe` application. The frontend consumes the existing FastAPI `/api/v1` service and does not duplicate backend, database, OAuth secret, or deployment responsibilities.

## Users

The primary user is an Indian small-business owner, typically checking Topline briefly from a phone or laptop between other work. They need to understand overdue receivables and safely approve follow-up messages without learning a complex finance system.

## Product Purpose

Topline helps the owner recover overdue invoice revenue. It gathers invoice and payment evidence, prepares reminder drafts, keeps uncertain claims or disputes visible, and requires explicit owner approval before any customer email is sent. Success is a confident daily review completed in about a minute.

## Positioning

Topline is an approval-gated accounts-receivable copilot. Its defining promise is operational: the agent may investigate and draft, but the owner sees the complete message and remains the final decision-maker.

## Operating Context

The owner primarily works through Gmail and Razorpay. The web app is a concise control panel for checking outstanding invoices, reviewing full reminder drafts, inspecting the activity record, triggering safe sync actions, and managing the Gmail connection. Customers do not use this interface.

## Capabilities and Constraints

- Show live ledger totals, customers, invoices, overdue status, and evidence-derived payment states from the FastAPI API.
- Show complete pending reminder drafts and their rationale.
- Allow draft editing, explicit approval, sending only after approval, and rejection.
- Surface payment claims, disputes, paused work, send failures, and unavailable integrations without hiding them behind colour alone.
- Show a chronological audit trail and recent sync status.
- Connect or disconnect Gmail through the backend OAuth flow.
- Report Razorpay configuration and reconciliation status without exposing or collecting server secrets in the browser.
- Support a clearly labelled illustrative-data mode for frontend development; production defaults to the live API.
- Do not add autonomous sending, customer accounts, a customer portal, analytics charts, or client-side storage of backend credentials.

## Brand Commitments

Name: Topline. Voice: calm, trustworthy, slightly serious, and clear. The supplied reference implementation at `/home/asus/Documents/Codex/2026-08-27/sites-plugin-sites-openai-bundled-create` is the approved visual and interaction authority.

## Evidence on Hand

The existing backend exposes ledger, activity, sync, Gmail connection, and approval-gated agent endpoints. The reference includes illustrative records for Acme Industries, Nova Textiles, and Bharat Traders; those records may be used only in explicitly labelled demo mode. There are no supplied testimonials, production-performance claims, or public customer proof.

## Product Principles

- Keep approval one obvious action away while showing the whole message first.
- Make uncertainty, disputes, and failure states more visible than reassurance.
- Preserve the inbox as the main work surface; keep this control panel concise.
- Explain what Topline did, what evidence it used, and what the owner can do next.
- Keep integration authority least-privileged and secrets server-side.

## Accessibility & Inclusion

The interface must support keyboard use, visible focus, reduced motion, high text contrast, touch targets suitable for one-handed phone use, responsive text scaling, and status cues that never rely on colour alone.
