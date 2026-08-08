# Personal Finance Tracking System - Professional Blueprint

Version 1.1 | Scope: Single-user, India-centric (UPI / GPay / CRED / credit cards / Amazon Pay) | Constraint: 100% free tooling

---

## 1. Executive Summary

The goal is a single, trustworthy ledger of all personal money movement, built only from free components, that:

1. Ingests transactions from many channels: bank SMS, bank email, UPI app exports (CSV), PDF statements, and Telegram messages.
2. Never double-counts a transaction regardless of how many channels report it or in what order they arrive (SMS first, email later, PDF up to 45 days later, Telegram manual updates in between).
3. Handles the shared-expense edge case properly: flag expenses as shared, track the expected reimbursement as a receivable (not income), and net it out when money actually comes back.
4. Puts a human-in-the-loop feedback loop on Telegram: every new entry is confirmed/categorized, shared expenses are flagged, and open receivables are reconciled over time.

The architecture is a pipeline: **Ingest -> Parse -> Normalize -> Deduplicate -> Persist -> Classify (feedback loop) -> Report**.

---

## 2. System Requirements

| # | Requirement |
|---|-------------|
| R1 | Multi-source ingestion: SMS, Email, PDF export, UPI-app CSV, Telegram manual entry |
| R2 | Zero duplicates even with out-of-order arrival across channels |
| R3 | Shared-expense lifecycle: flag -> track receivable -> match reimbursement -> settle |
| R4 | Reimbursements must be recorded as "return of my money", never as income |
| R5 | Telegram feedback loop for classification and shared-expense flagging |
| R6 | Monthly bank statement reconciliation (catch missing or stray transactions) |
| R7 | Reports: monthly spend by category, shared ledger net per person, cashflow |
| R8 | All components must cost Rs 0 (free tiers / self-hosted OSS only) |
| R9 | Privacy: SMS data is sensitive; no raw OTPs ever leave the device |

---

## 3. High-Level Architecture

```mermaid
graph TD
    A["Android SMS Forwarder"] --> B["Webhook"]
    C["Gmail (bank emails)"] --> D["Email Poller"]
    E["PDF / CSV upload"] --> F["Telegram Bot"]
    G["Manual message"] --> F
    B --> H["Parser & Normalizer"]
    D --> H
    F --> H
    H --> I["Deduplication Engine"]
    I --> J["Transactions DB"]
    J --> K["Shared Ledger (receivables)"]
    J --> L["Monthly Reconciliation"]
    F --> M["Classification & Feedback Loop"]
    M --> K
    K --> N["Reports / Dashboard"]
    L --> J
```

All components are free (details in Section 11).

---

## 4. Ingestion Layer (Multiple Sources, One Pipeline)

### 4.1 Bank SMS (fastest signal)
- Android app **SMS Forwarder** (free, open source) monitors the inbox for bank sender IDs (e.g., HDFCBK, VJPAY, PAYTM, ICICIB).
- Filter ONLY transaction keywords: `spent`, `debited`, `credited`, `UPI`, `txn`, `account`. **Never forward OTP messages**.
- Each SMS is forwarded to your **Telegram bot** (SMS Forwarder supports this natively) - no public webhook, no port forwarding, no tunnel. The always-on VM polls Telegram/Gmail. The SMS carries the **UTR / UPI reference number**, which is the strongest dedup key available.

### 4.2 Bank Email (slower, richer)
- Gmail receives bank alerts and monthly statements.
- A poller (IMAP or Gmail API) reads matching labels and pushes body + attachments into the pipeline.
- Emails usually arrive minutes to hours after the SMS for the same transaction.

### 4.3 PDF Statements (authoritative but stale)
- User sends the PDF to the Telegram bot (manual, monthly).
- Text-based PDFs parsed with a PDF text extractor; scanned PDFs run through free OCR (Tesseract).
- Used for **month-end reconciliation** (Section 8), not real-time tracking.

### 4.4 UPI App Exports (CSV)
- GPay / PhonePe / Paytm statement exports (CSV) are uploaded to the Telegram bot or a webhook.
- These contain the UTR column, which makes dedup near-perfect.

### 4.5 Telegram Manual Entry
- Free-text: `450 zomato` or `-450 zomato`, optional category and date.
- Also supports marking reimbursements: `received 500 from ravi for zomato`.

### 4.6 Raw Event Log (non-negotiable)
Every message (raw SMS text, raw email, uploaded file) is stored as an immutable event with:
`ingestion_id, source, channel, received_at, raw_payload`.
Nothing is dropped, even duplicates. This is the audit trail that makes every later decision explainable.

### 4.7 Connectivity: phone -> Telegram -> poll (no webhook, no tunnel)
Your phone and laptop are both behind NAT - a phone cannot "reach" your laptop. Flip the direction:

- SMS Forwarder forwards bank SMS to your **Telegram bot** (native support).
- The always-on cloud VM **polls** Telegram/Gmail with its trigger nodes (Telegram trigger and Gmail trigger both poll). No public webhook, no port forwarding, no tunnel required.
- Your Telegram manual-entry bot talks to the same always-on instance.

Result: phone and laptop can both be off; the cloud VM does all the work. Latency is "every 1-5 minutes" in polling mode - fine for a tracker. If you ever want sub-second (webhook) mode, the always-on VM's public IP + auth-token webhook covers that too.

---

## 5. Parsing & Normalization

### 5.1 Parser
Bank SMS formats differ (HDFC vs ICICI vs Axis vs Paytm). Maintain a **per-bank parser table** keyed on sender ID / email domain. Each parser extracts:

- `txn_date`, `txn_time`
- `amount` (decimal)
- `type` (`DEBIT` / `CREDIT`)
- `counterparty` (merchant or person name, e.g. "Paytm", "Zomato", "Ravi")
- `account` (masked, e.g. "XX1234")
- `mode` (`UPI`, `CARD`, `NEFT`, `IMPS`)
- `reference` (UTR / ref number if present)

Each field also gets a **confidence score**. Raw text is always retained next to the parsed fields.

### 5.2 Normalizer
- Amount -> integer **paise** (`450.50` -> `45050`). Never use floats for money.
- Date -> ISO-8601, fixed timezone.
- Counterparty -> lowercase, strip `UPI/`, strip prefixes/suffixes like `Paytm Payments Bank` -> alias map (`PAYTM`, `PayTM`, `paytm` all -> `paytm`).
- Reference -> kept verbatim (UTR is case-sensitive in matching).

### 5.3 AI-Assisted Layer (optional enhancement, not the foundation)
Deterministic rules handle the high-volume, well-formed 90% (SMS/UTR parsing, CSV columns). Reserve AI for the messy 10%:

- **Telegram natural-language entry**: `dinner 450 sam` -> `{amount: 450, merchant: sam, category: food, shared: true}`. When you would have to type structured fields, AI parses free text into the schema.
- **Ambiguous categorization**: low-confidence or unmatched entries get an AI-suggested category that you confirm in chat (never silently applied).
- **Exception queue triage**: unmatched/flagged transactions routed to AI for a suggested explanation.
- **Monthly narration**: "You spent 14% more on food this month" style report summaries.

Free execution options, in order of privacy:
1. **Local Ollama model** (free, fully private - financial data never leaves your machine). Best for Option A (self-hosted).
2. **Bring-your-own free-tier API** for Option B (Apps Script calls an LLM endpoint via `UrlFetchApp`). Data goes to that provider - only send anonymized, merchant-level data, never full account/card numbers or OTPs.

Rule: the deterministic path must always work standalone. If AI is unavailable, the system degrades to rules + your Telegram confirmation - never to silent wrong data.

---

## 6. Deduplication Engine (The Core)

### 6.1 Why ordering across channels breaks naive dedup
The same transaction arrives multiple times and out of order:

| Order | Channel | Delay |
|-------|---------|-------|
| 1 | SMS | seconds |
| 2 | Email alert | minutes to hours |
| 3 | Telegram manual note | user-dependent |
| 4 | UPI app CSV | when exported |
| 5 | PDF statement | up to 45 days later |

A naive "same amount on same day" check fails in both directions:
- It merges two genuinely different purchases (two coffees, same amount, same day).
- It misses a duplicate arriving a month later via the PDF.

### 6.2 Fingerprint keys (layered matching)
Compute two keys per record:

| Key | Composed of | Strength |
|-----|-------------|----------|
| `key_exact` | sha256(mode + account + date + amount + type + **reference/UTR**) | Highest. UTR is globally unique per UPI transaction. |
| `key_soft` | sha256(normalized_date + amount + type + normalized_counterparty) | High, but can collide for identical same-day purchases. |

- `key_exact` gets a unique database index. Presence of a UTR makes dedup deterministic forever (even 45 days later).
- `key_soft` is used only as a matching hint within a short window.

### 6.3 Match resolution
For an incoming record, query candidates by: same `type`, same amount, date within +/- 3 days, and (`reference` equal OR normalized counterparty similarity >= 0.85 using Levenshtein/Jaro-Winkler).

| Outcome | Decision | Action |
|---------|----------|--------|
| EXACT | UTR/ID hit (Tier 1) | Auto-merge duplicate, log mapping to canonical, add source alias, discard copy |
| STRONG | key_soft hit + merchant similar + unique in window (Tier 2) | Auto-merge, enrich canonical with missing fields, add source alias |
| WEAK / needs_review | fuzzy score in mid band, or multiple same-day candidates, or a Tier 2 match that is not exact on the ID | **Never silently merge.** Queue to Telegram: "Duplicate of Rs 450 at Zomato on 08 Aug? [Merge] [No]" |
| NEW | no match | Insert row (status = pending), then trigger the classification feedback loop |

### 6.4 Confidence ladder (provisional -> confirmed -> verified)
Mirrors banking reconciliation semantics (provisional vs final):

| Status | Meaning | How it is reached |
|--------|---------|-------------------|
| `pending` | Single source, unconfirmed | One SMS or one PDF line only |
| `confirmed` | Corroborated by 2+ independent sources, OR you acknowledged it in the Telegram loop | SMS + email, SMS + CSV, or your "yes" tap |
| `verified` | Reconciled against the bank statement total | Monthly reconciliation (Section 8) passed |

The confidence ladder feeds reports: only `confirmed`/`verified` rows count toward hard numbers; `pending` rows show as "provisional". This is the same discipline a risk desk applies - nothing is final until corroborated or reconciled.

### 6.4 Channel priority (who is the canonical copy)
When merging, the highest-priority channel's record becomes canonical and missing fields are filled from lower-priority records:

SMS (5) > Email alert (4) > Telegram manual (3) > UPI-app CSV (2) > PDF (1)

### 6.5 Channel priority (who is the canonical copy)
When merging, the highest-priority channel's record becomes canonical and missing fields are filled from lower-priority records:

SMS (5) > Email alert (4) > Telegram manual (3) > UPI-app CSV (2) > PDF (1)

Every corroborating source is recorded as an **alias** on the canonical row (e.g. `sources: [sms, email, gpay_pdf]`). This proves the merge happened and drives the confidence ladder.

### 6.6 Reconciliation closes the gaps the fuzzy matcher can't
A UTR-less, out-of-window duplicate (email-only txn that also shows up in the PDF two months later) is caught by **monthly reconciliation** (Section 8), not by windowed fuzzy matching. This is the deliberate design: fuzzy matching stays narrow to avoid false merges, and the statement-based reconciliation is the long-term safety net.

### 6.7 Dedup KPIs
- Duplicate auto-catch rate (target > 95% via UTR)
- False-merge rate (must stay ~0%; every suspected merge with ambiguity is sent to the human)
- Capture rate = recorded / bank statement transactions (target > 98%)
- `needs_review` queue drain rate (target: cleared within 48h)

---

## 7. Shared Expenses & Reimbursement Ledger

### 7.1 The banker's model: a clearing account
From a BlackRock/Morgan Stanley standpoint, shared expenses are **not** ordinary expenses. Treat them like a clearing account:

- The full amount is spent and recorded.
- Only the user's **personal share** is real personal spending.
- The friends' share is a **receivable** (money owed to you), and the eventual reimbursement is a **reduction of that receivable**, never income.

### 7.2 Lifecycle (matches your "flag now, calculate later" idea)

| State | What happens |
|-------|--------------|
| `OPEN` | Expense flagged Shared. Ledger row created with `expected_receivable` (user's share; leave blank = flag only, per your preference). |
| `PARTIALLY_SETTLED` | Money returned for part of the share. Receivable reduced. |
| `SETTLED` | Receivable = 0. |
| `WRITTEN_OFF` | Explicit decision: friend never returned it. Removed from active receivables; recorded as real personal loss. |

### 7.3 Transaction state machine (tag now, compute later)
Flagging without the share amount is not a shortcoming - it is the accurate design, because the true share often becomes knowable only at settlement (when Sam sends Rs 600, you learn your share was Rs 300). Deferred math is more accurate, not less:

| Transaction state | Meaning |
|-------------------|---------|
| `personal` | Normal expense, done. No prompt needed. |
| `maybe_shared` | Heuristically flagged as possibly shared; awaiting your answer. Best-effort, never blocking - if unanswered it stays here and appears in a "needs review" list. Reports still count it in gross spend. |
| `flagged_shared` | You said Yes. Share amount **not yet known**. Full amount stays in spend until resolved. |
| `resolved_shared` | Share computed at settlement: `full - received = your_share`. Report recomputes to net automatically. |

Deferred math is safe because the ledger is event-sourced: `flagged_shared` keeps the full amount in spend; the moment a matching inbound closes it, all aggregates recompute idempotently to net (Section 10.26). This also powers an **"in-flight shared spend"** dashboard figure - money you have spent that others will repay.

### 7.4 Reimbursement matching
- An incoming `CREDIT` triggers the bot: "Rs 500 received from Ravi. Reimbursement? [Yes] [No]".
- If Yes -> match against open shared expenses from that person/group. Handles:
  - Partial reimbursement
  - One payment settling multiple expenses (link table)
  - Group settlement where the amount is a net balance, not tied to one expense
  - Cash reimbursement (manual entry; flag as "received offline")
- Net position per person/group: `sum(expected_receivable) - sum(received)` -> a clean "who owes me / I owe" balance.

### 7.5 Edge cases in the shared ledger (banker's review)
- **Aging of receivables**: report shows how long each receivable has been open; reminders fire after configurable periods (e.g., 7 / 30 / 60 days).
- **Over-payment**: friend sends more than the flagged share -> excess goes to a `prepaid` balance against that group, applied to future shared expenses.
- **Multi-person splits**: optional `split_of` group field; default share suggestion = 1/N, but the user can keep it as flag-only (no calculation) until settlement time.
- **Reversal of a reimbursement** (money taken back): handled as a debit against the receivable, not a new expense.
- **Cross-currency**: if a friend repays in a different currency or forex is involved, record amount in original currency + rate; keep INR equivalent for the ledger.

---

## 8. Monthly Reconciliation

Every month-end, when the statement PDF/CSV arrives:

1. Group all canonical transactions by account and month.
2. Sum debits and credits; compare with the statement totals / closing balance.
3. Any statement line not matched to an existing record -> likely a **missed transaction** (SMS never captured). Insert as `NEW` with `source = statement`, then run the classification loop.
4. Any existing record not in the statement -> **flag for review** (possible stray/unauthorized transaction).
5. Publish a Telegram report: "Reconciled. Found 3 missed, 1 anomaly, 0 duplicates."

This is the system's ultimate integrity check and closes every dedup gap left open by Section 6.

---

## 9. Telegram Feedback Loop (your proposed "small feedback loop")

Validated: a human-in-the-loop classification stage is the right call. It solves classification AND acts as a final dedup/attribution safety net.

### 9.1 Trigger-driven prompt (not universal)
Asking about every transaction is friction and users ignore it within a week. The loop is **trigger-driven**: prompt only when a heuristic suspects shared:

- **Odd / non-rounded amounts** (Rs 749, Rs 2,367) - typical of split bills
- **Counterparty is a known person** (your friends/group list from prior settlements)
- **Category is naturally shared** (restaurants, groceries, group trips, hotels, fuel, movie tickets)
- **Amount above a threshold** (e.g., Rs 500) where split likelihood matters
- **Inbound settlements** matching a prior outflow

Everything else is auto-accepted as `personal` with zero prompts. (Configurable toggle: "ask me about everything" for the first weeks if you prefer - see Decisions Log #5.)

When triggered, the bot sends a non-blocking inline keyboard:

```
Rs 900 at Olive Bar (dinner) - Shared expense?  [Yes] [No]
```

- **Yes** -> `flagged_shared`. No math asked. Later, when Rs 600 inbound from Sam is seen, the bot asks "Sam paid you Rs 600 - settlement for Olive Bar (Rs 900)? [Confirm] [No]". On confirm -> your share = 300, receivable closed, reports recompute.
- **No** -> `personal`. Done.

### 9.2 Idempotent confirmations
The prompt carries the transaction's **dedup key**. If the same txn arrives via SMS and later via PDF, you answer once and the merge applies your flag to the single canonical row - never a second prompt.

### 9.3 Debouncing (SMS floods)
SMS forwarders often dump several messages at once. Requests are batched: collect for ~60 seconds, then send one message with inline buttons for up to 5 transactions. No notification spam.

### 9.4 Quiet hours + auto-snooze
No pings between 23:00 and 07:00; entries queue and are asked the next morning. If you consistently ignore prompts, the system auto-snoozes to fewer, batched prompts rather than spamming.

### 9.5 Reimbursement prompt
On incoming credits (Section 7.4). Supports matching and partial settlements.

### 9.6 Digests
- **Daily**: "3 expenses categorized, 1 flagged shared."
- **Weekly**: spend by category, open receivables.
- **Monthly**: full report + reconciliation summary.

### 9.7 Why this makes the system more accurate, not just nicer
Every decision goes through a confirmable action, so:
- Ambiguous dedup gets a human override.
- Shared flags are created at point of spend (when memory is fresh), not reconstructed weeks later.
- Receivables are reconciled at point of money movement.
- The user retains full control; nothing is silently auto-categorized forever.

---

## 10. Edge Cases - Banker's Review (BlackRock / Morgan Stanley lens)

1. **Authorization vs settlement**: card/UPI "pending" holds vs posted amounts; only the settled amount is a transaction. Track a pending state to avoid double counting.
2. **Transaction date vs value date vs posting date**: statement cycles straddle calendar months; store all three dates, report by transaction date, reconcile by statement cycle.
3. **Refunds, reversals, chargebacks**: negative flows net against spend. A refund of a previously-shared expense reduces the receivable too.
4. **Rounding & precision**: store paise (integer). GST splits, cashback partials, and tips must not drift the ledger.
5. **EMI transactions**: a purchase of Rs 60,000 shown as Rs 5,000/month debit. Tag an `emi_group` so the statement shows real liability, but budget shows the obligation.
6. **Recurring subscriptions**: match against known schedules to pre-flag and dedup.
7. **Merchant aliases & truncation**: SMS merchants are truncated ("PAYTM"), emails are verbose ("Paytm Payments Bank Ltd."). The alias map + fuzzy matching handles this.
8. **Identical same-day purchases**: two Rs 450 Zomato orders must stay separate. UTR solves it; without UTR, ambiguity is pushed to the human, never auto-merged.
9. **Cash & offline spend**: silent blind spot; add a manual Telegram entry path and a monthly "did I miss cash?" check.
10. **Multi-currency / forex**: Amazon global, travel. Store original currency + rate + INR equivalent.
11. **TDS on card payments / other statutory deductions**: treat as a normal outflow with a `tax` tag so it doesn't look like spend.
12. **Shared card / family members**: "Not mine" classification keeps other people's transactions out of your personal budget but visible in the audit log.
13. **Fraud & anomaly detection**: amounts far outside your normal distribution, duplicate charges from a bank error, or unknown entries surface in reconciliation for a look.
14. **Data quality confidence**: parsers score fields; low-confidence parses are shown to the user for correction, never silently trusted.
15. **Long-tail duplicates**: the PDF arriving 45 days late is caught by reconciliation, not by windowed fuzzy logic.
16. **Privacy & retention**: SMS data contains OTPs and balances; filter at the source, keep data in your own store, and allow full export (takeout) at any time.
17. **Credit lines disguised as wallets (CRED / Slice / Amazon Pay Later)**: these are liabilities, not wallet debits. A Slice "pay in 3" or Amazon Pay Later purchase is a credit advance repaid over time - recording it as a plain expense double-counts the outflow when the EMI is paid. Tag them with an `emi_group` (see #5) so the statement shows the real debt and the budget shows the real obligation.
18. **Rewards / cashback as credits (CRED, GPay)**: rewards redeemed land as small `CREDIT`s. Auto-route to a `rewards` category, never to income and never mistaken for a reimbursement.
19. **Card payment vs card spend**: paying your credit card bill via CRED/UPI is a **liability settlement** (account transfer), not spending. It must be excluded from spend categories or the monthly "spend" is massively inflated. This is a top-3 trap in the CRED/Amazon Pay/Slice set.
20. **Top-ups to wallets (Amazon Pay balance, GPay)**: moving money into a wallet is also a transfer, not spend; the actual spend is the wallet debit later. Tag both sides so neither is double-counted.
21. **Failed / declined / reversed transactions**: a UPI debit that fails then reverses, or a card authorization that drops off, must be **excluded or netted**, never counted as spend. Track a `FAILED` / `REVERSED` state; the reversal of a debit nets against it. This is the biggest source of phantom spend in naive trackers.
22. **Three distinct credit types - never conflate them**:
    | Credit type | Meaning | Handling |
    |-------------|---------|----------|
    | Refund | Merchant returns part of an expense | Nets against the original expense (reduces spend) |
    | Reimbursement | Friend/group repays your shared expense | Closes a receivable (Section 7), never income |
    | Income | Salary, interest, etc. | The only type that counts as income |
    The model must tell them apart before any credit is bucketed.
23. **Bill-level grouping**: one restaurant bill paid 3 ways (your card + friend's UPI + cash) is one **logical expense** with multiple raw transactions. Need a `bill_id` grouping level above transactions, so reports show "Dinner: Rs 1200" once, not three fragments.
24. **Multi-account self-transfers**: savings -> wallet -> card repayment are transfers, not expense/income. Detect same-owner accounts and exclude from spend; the system tracks them only for balance verification.
25. **Source format drift**: banks change SMS/email templates and GPay changes export formats. Any parsing failure must land in the **unmatched/exception queue** with an alert to you - never silently corrupt or drop data. A parser version field on each record makes drift diagnosable.
26. **Backdated / late entries**: event-sourcing style - a PDF arriving 40 days late must be attributed to its transaction date, not today, and all aggregates recomputed idempotently from raw events. Never store pre-computed running totals that a late entry can corrupt.

---

## 11. Technology & Hosting (All Free)

> Decision (user-confirmed): first parsers target GPay, CRED, Amazon Pay, Slice. Hosting recommendation in Section 11.4.

### Option A - Self-hosted N8N (recommended; most power)
- **Host**: Oracle Cloud **Always Free** ARM VM (4 OCPU / 24 GB RAM) or **Google Cloud e2-micro free tier** (1 small always-on VM, fine for N8N) or a home Raspberry Pi. (Railway/Render free tiers sleep and break real-time ingestion - good for a demo, not a tracker. Fly.io free allowance has limited monthly hours.)
- **Automation**: N8N (self-hosted, OSS, free forever). Telegram/Gmail trigger nodes **poll** - no public webhook needed for the phone side.
- **AI**: native **AI Agent node** (LangChain-based) + **AI Transform node** (natural-language parsing) built into Community Edition, free. Run a **local Ollama model** so financial data never leaves your machine.
- **Database**: PostgreSQL (or SQLite for simplicity) on the same box.
- **SMS**: SMS Forwarder app -> Telegram bot (native) -> N8N polls Telegram.
- **Email**: N8N IMAP trigger on Gmail.
- **PDF/CSV**: Telegram bot -> N8N parser (PDF text extraction / Tesseract OCR).
- **Dashboard**: Grafana or Metabase (OSS) or Telegram reports only.
- **Cost**: Rs 0. Time cost: initial setup (~1hr) + light maintenance.

### Option B - Zero-server Google stack (simplest, zero maintenance)
- **Automation**: Google Apps Script (free, runs on triggers).
- **Database**: Google Sheets (free) with dedup logic in Apps Script.
- **Email**: Gmail filter + Apps Script (`onMessage` trigger).
- **SMS**: SMS Forwarder -> Gmail (forward) or a published Apps Script webhook.
- **Telegram**: Apps Script calls the Telegram Bot API via `UrlFetchApp`.
- **Limits**: ~20k executions/day, 6-min cap per run - ample for personal use.
- **Caveat**: text-based PDF parsing in Apps Script is limited; for statement PDFs prefer Option A or extract text via Google Drive conversion.

### Option C - Serverless (technically cleanest free tier)
- **Compute**: Cloudflare Workers free tier (100k req/day) + scheduled cron.
- **Database**: Supabase free tier (Postgres, 500 MB) or Neon free tier.
- **Telegram**: bot webhook -> Worker.
- **Cost**: Rs 0. Requires comfort with a bit of code.

### 11.4 Recommendation (for this user)

The previous sessions changed the calculus in two ways: the **AI Agent/Transform nodes, local Ollama, native PDF parsing, and one-tool automation** all live in N8N, and the **phone -> Telegram -> poll** pattern (Section 4.7) removes the need for a public webhook entirely. If you can manage a free VM, **Option A (N8N self-hosted) is now the primary recommendation** - it is the only option with a private, free, native AI path, always-on, and it removes the Apps Script PDF weakness.

Revised guidance:

1. **Option A (N8N + Ollama on Oracle Always Free / Google Cloud e2-micro)** if you are comfortable following a one-time server setup guide (docker, a bit of config). Best fit for the AI-assisted entry and full "pro" trajectory. Phone and laptop can both be off; the cloud VM does all the work.
2. **Option B (Google zero-server)** remains the zero-maintenance fallback. AI is still possible (Apps Script calls a free-tier LLM endpoint for the messy 10%), just not native and not fully private. Core deterministic tracking works perfectly either way.

Timeline: run Phase 1-2 on your chosen stack, re-evaluate after 3 months of use. The pipeline design is identical across all options - only the plumbing differs.

All options keep the Telegram feedback loop as the primary UI.

---

## 12. Cost Breakdown (Rs 0)

| Component | Free choice |
|-----------|-------------|
| Automation | N8N self-hosted (with native AI Agent/Transform nodes) OR Apps Script OR Cloudflare Workers |
| AI | Local Ollama (private, free) OR free-tier LLM API |
| Database | SQLite / PostgreSQL on own box / Supabase / Neon |
| SMS intake | SMS Forwarder (open source) |
| Email intake | Gmail |
| Bot | Telegram Bot API |
| OCR / parsing | Tesseract / PDF text extractors (OSS) |
| Dashboard | Telegram / Grafana / Metabase (OSS) |
| Monitoring | UptimeRobot free (if self-hosting) |

---

## 13. Security & Privacy

- **Filter at the source**: SMS Forwarder forwards only transaction messages, never OTPs.
- **Secrets**: bot token and DB credentials live in environment secrets, never in code or committed files.
- **Self-host = data stays yours**: Option A keeps all financial data on your own machine.
- **Encryption at rest**: encrypt the DB; keep daily backups to your own storage.
- **Audit trail**: immutable raw event log (Section 4.6) for every decision.
- **Export / portability**: full CSV takeout of all tables at any time.

---

## 14. Roadmap

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| 1. Skeleton | Choose host; stand up pipeline; SMS ingestion for one bank; raw event log + DB | SMS transactions recorded |
| 2. Dedup | UTR key, key_soft matching, channel priority, merge logic | Zero duplicates across SMS+email for one bank |
| 3. Feedback loop | Telegram bot: classify, shared flag, duplicate override, debounce, quiet hours | 95% of entries classified via bot |
| 4. Shared ledger | Receivable rows, reimbursement matching, balances per person, aging, bill-level grouping | Net shared position accurate |
| 5. Reconciliation | Monthly statement reconcile + anomaly report + confidence ladder verified status | Capture rate >= 98% |
| 6. Reporting | Categorization + monthly dashboard + takeout + monthly narration (AI) | Full monthly report delivered |
| 7. Hardening | More banks, OCR PDFs, multi-currency, AI-assisted entry (Ollama), source-format-drift alerting, KPIs, backups | Runs unattended for 3 months |

---

## 15. Decisions Log (user-confirmed)

| # | Decision | Resolution |
|---|----------|------------|
| 1 | Hosting | **Revised**: Option A (N8N self-hosted + local Ollama on Oracle Always Free) is primary - only free option with native/private AI. Option B (Google) stays as zero-maintenance fallback (Section 11.4) |
| 2 | First parsers | GPay, CRED, Amazon Pay, Slice. CRED/Slice/Amazon Pay Later must be treated as credit lines (Section 10.17-10.20) |
| 3 | Shared-expense flag | Flag-only by default; if user supplies a share amount/%, system calculates the receivable from that input (Section 9.1) |
| 4 | Reimbursement matching | Human-confirmed on Telegram first; auto-match only after the user has accepted the same pattern repeatedly |
| 5 | AI usage | Optional enhancement for the messy 10% (free-text entry, ambiguous category, exception triage, monthly narration). Deterministic rules always work standalone (Section 5.3) |
| 6 | Confidence model | pending -> confirmed -> verified ladder, corroboration-based, drives reports (Section 6.4) |
| 7 | Shared-expense prompting | **Trigger-driven**, not universal: prompt only on heuristic suspects (odd amounts, known persons, shared categories, high value, inbound settlements). Configurable toggle for "ask me about everything" (Section 9.1) |
| 8 | Shared state machine | personal -> maybe_shared -> flagged_shared -> resolved_shared; deferred math at settlement (Section 7.3) |
| 9 | Connectivity | phone -> Telegram -> poll on always-on VM. No public webhook, no port forwarding, no tunnel (Section 4.7) |
| 10 | Transaction states | `FAILED`/`REVERSED` excluded or netted; three credit types (refund/reimbursement/income) never conflated (Section 10.21-10.22) |
| 11 | Flagged-shared vs receivable | Flagging shared != creating a receivable. Settlement matcher only matches an actual `group_expense` record - safer than auto-matching flagged txns (prevents false positives). In the bot, "Yes, shared" must surface a "Create group expense" action (Section 17.2) |
| 12 | Classification pipeline order | dedup -> settlement-match -> credit-type -> category. Inbound person credits are typed (reimbursement/refund/income) BEFORE any spend category is applied (Section 17.3) |

---

## 16. Summary of Answers to Your Specific Questions

- **How are duplicate entries prevented across SMS/email/PDF/Telegram?** Layered matching: UTR-based exact key (forever, across all channels), soft key + fuzzy within a short window, human override for ambiguity, and monthly reconciliation as the long-tail safety net. Channel priority decides the canonical copy. Nothing is deleted - all duplicates are linked and archived.
- **How is the "shared expense" feedback loop handled?** Validated as the right approach. New entries are asked about on Telegram (debounced, quiet-hours aware). Yes = flag, no calculation needed. Money coming back later is matched as a receivable reduction. A clearing-account model keeps "who owes me" accurate and never treats reimbursements as income.
- **Is the plan fully professional?** Yes - it includes an immutable audit trail, reconciliation with the bank as the source of truth, aging and write-off handling for receivables, fraud/anomaly detection, paise-level precision, per-field confidence scoring, and KPIs for capture/duplicate/classification quality.

---

## 17. Implementation Review (prototype validated via live smoke test)

A working prototype (FastAPI + Telegram bot) was built and verified end-to-end:

| Step | Result |
|------|--------|
| 1. UPI SMS ingest | Auto-ingested, `pending` |
| 2. Same txn via email | Deduped as duplicate via UTR match |
| 3. PDF export, no UTR | Merged via merchant+amount+date fingerprint |
| 4. Shared prompt = Yes | Flagged `flagged_shared` |
| 5. Friend pays Rs 600 back | Auto-matched -> reclassified as reimbursement (not income); your share = Rs 300 |
| 6. Rs 450 below threshold | No shared prompt (minimal-friction rule works) |

### 17.1 Bug found & fixed: substring regex false positives
`ravi@ybl` was classified as category `bills` because the regex `vi\b` matched inside the word "ravi" (the brand "Vi" telecom rule fired on a person's VPA). Fix: use whole-word anchors `\bvi\b` and prefer the known-persons list before category regexes. Lesson for the classifier: **always match person/merchant identity first, category keywords second, and use token boundaries on both sides**.

### 17.2 Flagging shared is not creating a receivable
In the smoke test, step 4 flagged the ledger row but created no `group_expense`, so step 5's settlement did not match. This is **correct, safe behavior**: auto-matching flagged txns to inbound settlements could create false positives (a genuine income that merely resembles a flagged amount). The bot must therefore surface a "Create group expense" action immediately after "Yes, shared" - that is what makes the receivable real and matchable. This is an explicit UX requirement, not a gap.

### 17.3 Classification pipeline order matters (credit typing before category)
The smoke test classified `ravi@ybl` (an inbound settlement) into a spend category before trying settlement matching. Correct order is:

```
dedup -> settlement-match -> credit-type (reimbursement/refund/income) -> category
```

Inbound credits from known persons must be typed as reimbursement candidates **first**; a spend category should never be applied to money coming in. This prevents a reimbursement from polluting spend reports.
