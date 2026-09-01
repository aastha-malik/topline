---
name: Topline
description: A quiet dispatch ledger for controlled invoice follow-up.
colors:
  paper: "#f4f1e8"
  surface: "#fffdf7"
  surface-strong: "#ffffff"
  ink: "#18211e"
  muted-ink: "#68716c"
  soft-fill: "#ebe7db"
  ledger-line: "#dcd6c8"
  ledger-navy: "#17384b"
  rail-navy: "#0e2937"
  rail-muted: "#a9bcc3"
  rail-hover: "#183947"
  approval-green: "#176b4d"
  approval-green-hover: "#105a40"
  approval-green-soft: "#dcebe2"
  attention-saffron: "#c77829"
  attention-saffron-soft: "#f7e4cc"
  refusal-red: "#873c35"
  refusal-red-soft: "#f6e2dd"
  warning-paper: "#fff0ce"
  warning-ink: "#6f410d"
  focus-blue: "#247499"
  rail-paper: "#f8f5eb"
typography:
  display:
    fontFamily: "Geist Variable, Geist, sans-serif"
    fontSize: "clamp(29px, 4vw, 42px)"
    fontWeight: 700
    lineHeight: 1.04
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "Geist Variable, Geist, sans-serif"
    fontSize: "clamp(24px, 2.8vw, 34px)"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Geist Variable, Geist, sans-serif"
    fontSize: "23px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  body:
    fontFamily: "Geist Variable, Geist, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: "Geist Variable, Geist, sans-serif"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.06em"
rounded:
  compact: "10px"
  field: "11px"
  control: "12px"
  container: "14px"
  sheet: "16px"
  pill: "999px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "14px"
  lg: "18px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.approval-green}"
    textColor: "{colors.surface-strong}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.approval-green-hover}"
    textColor: "{colors.surface-strong}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.refusal-red}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  chip-status:
    backgroundColor: "{colors.approval-green-soft}"
    textColor: "{colors.approval-green}"
    rounded: "{rounded.pill}"
    padding: "0 12px"
    height: "36px"
  card-document:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sheet}"
    padding: "22px 24px"
  input:
    backgroundColor: "{colors.surface-strong}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "0 13px"
    height: "44px"
  navigation-active:
    backgroundColor: "{colors.rail-paper}"
    textColor: "{colors.ledger-navy}"
    rounded: "{rounded.control}"
    padding: "0 13px"
    height: "48px"
  attention-row:
    backgroundColor: "{colors.warning-paper}"
    textColor: "{colors.warning-ink}"
    rounded: "{rounded.container}"
    padding: "16px 20px"
  audit-event:
    backgroundColor: "{colors.warning-paper}"
    textColor: "{colors.warning-ink}"
    rounded: "{rounded.control}"
    padding: "14px 16px"
---

# Design System: Topline

## Overview

**Creative North Star: "The Dispatch Ledger"**

Topline feels like a quiet Indian dispatch ledger made operational: warm paper carries dense, readable records; a deep ink rail anchors orientation; and status color behaves like a clerk's verification mark. The interface is calm but not sparse. It keeps invoice amounts, full email drafts, reasons, timestamps, and access boundaries visible so a business owner can make a safe decision quickly.

The cockpit earns trust through evidence rather than reassurance alone. Approval controls stay close to the complete draft, warnings interrupt the flow in saffron paper, and every automated act remains legible in a time-stamped rail. On a phone, the same record structure compresses without hiding meaning: navigation stays fixed, approval actions stay within one-handed reach, and tables become labelled cards.

**Key Characteristics:**

- Warm paper and white ledger sheets against an ink-navy orientation rail.
- Compact, number-conscious Geist typography with full drafts kept readable.
- Green verification, saffron attention, and red refusal used as operational semantics.
- Bordered records for reference data; soft elevation only for actionable document sheets.
- Responsive transformations that preserve labels, context, and safety actions.

## Colors

The palette separates the ledger's neutral material from three operational signals: navy for orientation, green for verified approval, saffron for attention, and red for refusal or critical delinquency. The same semantic roles are remapped through CSS custom properties in the implemented dark theme.

### Primary

- **Ledger Navy:** Anchors the approval summary, links, timeline nodes, and active mobile navigation.
- **Rail Navy:** Supplies the deeper desktop navigation field so the working surface reads as warm paper beside ink.

### Secondary

- **Approval Green:** Marks affirmative sending actions, connection verification, live status, and successful invoice state.
- **Approval Green Soft:** Carries status chips, seals, selection feedback, and low-intensity success surfaces.

### Tertiary

- **Attention Saffron:** Marks pending work and overdue attention without borrowing the stronger refusal semantics of red.
- **Warning Paper and Warning Ink:** Form a paired interruption surface for conflicts that require human checking before approval.
- **Refusal Red and Refusal Red Soft:** Mark rejection controls, failed states, unpaid status, and critical delinquency.

### Neutral

- **Paper:** The application canvas and timeline cutout color.
- **Surface and Surface Strong:** Ledger sheets, cards, controls, and editable document areas.
- **Ink and Muted Ink:** Primary reading color and secondary explanation respectively.
- **Soft Fill and Ledger Line:** Dense table headers, quiet hover fills, borders, and audit dividers.
- **Rail Muted, Rail Hover, and Rail Paper:** The inactive, interactive, and selected states of the desktop navigation rail.

**The Signal Has a Job Rule.** Green confirms or approves, saffron asks for attention, and red marks rejection or critical delinquency; none is ambient decoration.

**The Warning Pair Rule.** Human-review conflicts use warning paper and warning ink together so the interruption remains readable in light and dark themes.

## Typography

**Display Font:** Geist Variable (with Geist and sans-serif fallbacks)
**Body Font:** Geist Variable (with Geist and sans-serif fallbacks)
**Label Font:** Geist Variable (with Geist and sans-serif fallbacks)

**Character:** A compact grotesk keeps figures, timestamps, customer records, and email text within one practical voice. Hierarchy comes from size, weight, tight heading tracking, and tabular numerals rather than a decorative type contrast.

### Hierarchy

- **Display** (700, fluid 29–42px, 1.04): Screen introductions; settles to 30px on small phones.
- **Headline** (700, fluid 24–34px, 1.1): Sticky topbar greeting or current screen name; settles to 21px below the desktop rail breakpoint.
- **Title** (700, 23px, 1.2): Section headings, with nearby compact headings ranging from 16px to 24px by context.
- **Body** (400, 14px, 1.55): Explanations and full email drafts; the email sheet uses a 1.62 line-height and is constrained to 75ch.
- **Label** (700, 11px, 0.06em): Table headers and mobile field labels; functional labels may use uppercase.
- **Numeric Record** (650–750, tabular numerals): Amounts, overdue counts, dates, and timestamps.

**The Whole Draft Rule.** Draft emails remain normal reading text with generous line-height; never compress them into previews before an approval action.

**The Ledger Number Rule.** Use tabular numerals for money, dates, counts, overdue age, and audit time so records scan vertically.

## Layout

Desktop uses a 238px sticky rail beside a fluid working surface. The sticky topbar is 102px tall, and content sits in a centered region up to 1440px wide with fluid horizontal padding from 24px to 72px. Repeated working groups use 14–24px internal rhythm and compact 10–18px gaps; major sections separate by roughly 40px.

At 1000px, summary metrics move from three columns to two, with the approval metric spanning the row; connection cards stack. At 900px, the rail disappears, the shell becomes one column, the topbar reduces to 86px, and a five-item navigation dock fixes 10px from the bottom edge. Content reserves roughly 110px of bottom space for the dock.

At 640px, screen introductions stack, draft padding tightens to 18px, and approval actions become sticky 84px above the bottom so the dock and action tray do not collide. Invoice tables shed their header and each row becomes a two-column labelled card; email and status occupy the full row. Audit events narrow their time column but keep the timestamp, node, and description aligned. At 420px, approval controls wrap without displacing the primary action.

**The Label Survives Rule.** When a table becomes cards, every value keeps an explicit field label through `data-label`; responsive simplification may change structure, never meaning.

**The One-Handed Decision Rule.** On small screens, fixed navigation and sticky approval actions must remain separated and simultaneously reachable.

## Elevation & Depth

Topline is mostly flat and structural: lines, tonal fills, and the navy rail establish hierarchy. Soft ambient shadow is reserved for document-like draft and connection cards, while the approval summary lifts slightly because it is actionable. The sticky topbar and mobile dock use translucent context layers with blur, and the dark theme increases shadow density without hardening edges.

### Shadow Vocabulary

- **Document Ambient** (`0 14px 36px rgba(45, 42, 30, 0.09)`): Draft and connection sheets in light mode.
- **Document Ambient Dark** (`0 18px 44px rgba(0, 0, 0, 0.28)`): The same sheets in the implemented dark theme.
- **Approval Lift** (`0 12px 28px rgba(18, 50, 68, 0.18)`): Actionable approval summary at rest, increasing on hover.
- **Dock Lift** (`0 12px 30px rgba(0, 0, 0, 0.18)`): Fixed mobile navigation above scrolling content.

**The Paper Before Shadow Rule.** Use borders and tonal paper for reference records; use ambient elevation only when a surface behaves like a document or persistent control layer.

## Shapes

The form language is gently administrative: controls use compact 10–12px corners, records and attention rows use 14px corners, and document sheets use 16px corners. Pills are reserved for small status and tone labels. Verification marks combine circular seals, shield outlines, dots, and check forms; borders are thin and quiet, while dashed borders identify empty and feedback states.

**The Radius Follows Scale Rule.** Keep controls tighter than records and records tighter than document sheets; pills belong only to compact status metadata.

## Components

### Buttons

- **Shape:** Gently curved controls with a 44px minimum height and 12px radius.
- **Primary:** Approval green with white text, strong weight, and a small ambient green shadow; hover deepens the green and lifts by 1px.
- **Secondary:** Surface fill with a ledger-line border; hover changes to soft fill.
- **Quiet and Danger:** Transparent refusal red keeps rejection subordinate until confirmation, when the action becomes solid red.
- **Focus:** Every interactive component uses a 3px focus-blue outline offset by 3px.

### Chips

- **Style:** Compact pill labels pair a soft semantic fill with the matching dark semantic text.
- **State:** Green means gentle, approved, or live; saffron means firm or partial attention; red means final notice, unpaid, or critical status; warning paper marks disputed or claimed evidence.

### Cards / Containers

- **Corner Style:** 14px for bordered records; 16px for elevated document sheets.
- **Background:** Warm surface or strong white for editable content; warnings tint the sheet instead of adding a hard outline.
- **Shadow Strategy:** Reference records stay flat; draft and connection sheets use Document Ambient.
- **Internal Padding:** 18px on compact mobile records and 22–24px on desktop sheets.

### Inputs / Fields

- **Style:** Draft-editing and rejection fields use strong surface fill, a ledger-line border, an 11px radius, and plain labels above the field; single-line inputs have a 44px minimum height.
- **Focus:** The global focus-blue outline remains visible outside the field border.
- **Editing Context:** Subject and full email body stay inside the same draft document as invoice context and decision controls.

### Navigation

The desktop rail is deep navy with 48px rows, subdued inactive text, a quiet rail-hover fill, and a paper-filled active row. Below 900px it becomes a five-column fixed dock with 54px targets; the active item becomes navy.

### Approval Draft

The draft is a document, not a summary card. Customer identity and tone sit above invoice context; the complete email body occupies a readable 75ch sheet; the agent's reason follows; and approval, edit, and reject share a separated action tray. On small phones, the tray sticks above the navigation dock.

### Activity Rail

Each event preserves time, a navy node, and explanatory copy along a continuous ledger line. The current attention event changes only its node and content surface to saffron warning semantics; history remains structurally consistent.

## Do's and Don'ts

### Do:

- **Do** keep the exact draft, agent reason, and send decision in the same document surface.
- **Do** use green, saffron, and red only for their assigned operational meanings.
- **Do** preserve timestamps, field labels, payment provenance, and access boundaries as visible evidence.
- **Do** keep the 3px focus outline and reduced-motion fallback on new interactive components.
- **Do** transform dense desktop records into labelled mobile cards and reserve clearance for fixed controls.

### Don't:

- **Don't** hide an email body behind truncation, a modal, or a preview when approval is available.
- **Don't** use green for a pending action, saffron for decoration, or red for routine navigation.
- **Don't** detach sticky actions from the draft they affect or let them overlap the mobile dock.
- **Don't** replace explicit verification text with a mark alone; safety and access remain readable in plain language.
- **Don't** add hard offset shadows, ornamental gradients, or glass effects outside the implemented sticky context layers.
