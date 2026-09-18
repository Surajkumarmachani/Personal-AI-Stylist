# Error budget policy

**Status: IN FORCE — clause 3 only. Signed 2026-09-17 by Suraj Kumar.**
Clauses 1 and 2 are agreed in principle and DORMANT: they are written against a
burn rate nothing currently computes, and a clause with no trigger is not a
control. Clause 3 binds today.

The Phase 9 exit criterion is not "a policy exists", it is "signed off". §B1
puts it plainly: *an unagreed error budget is not a control.* That criterion is
now **met for clause 3** and explicitly not claimed for the other two.

Owner of the roadmap and owner of reliability are the same person here. That
makes the agreement easier to get and easier to quietly ignore, which is the
reason for writing it down rather than remembering it.

---

## The budgets

From §B1, unchanged. A 30-day rolling window.

| SLI | SLO | Budget @ 30d |
|---|---|---|
| Suggestion availability (non-5xx on `/suggestions`) | 99.5% | 3h 36m |
| Suggestion latency (p95 `/suggestions`) | ≤ 1500ms | 5% of requests |
| Ingest success (reach ≥ `CLASSIFIED` within 60s) | 99.0% | 1% |
| Ingest completeness (reach `COMPLETE` within 10m) | 97.0% | 3% |
| Tag accuracy (category + primary colour, golden set) | ≥ 92% | CI gate |
| Data durability (garment rows + originals) | 99.999999% | — |

Try-on is absent, but no longer because it is unbuilt. The render path exists;
what does not exist is a single measured render on this account — every call
degrades to a board until `VTON_API_TOKEN` is set. An SLO over zero samples is
a number nobody can breach, and setting one now would be the decoration §B1
warns about. It belongs here once the benchmark grid has run.

## The policy

1. **Budget >50% consumed → feature freeze.** Reliability work only until the
   window rolls or the budget recovers. No new endpoints, no new phases.
2. **Budget exhausted → no non-critical deploys.** Security fixes and
   reliability fixes still ship. Everything else waits.
3. **Data durability has no budget.** There is no acceptable rate of losing a
   user's photographs. A single incident is a stop-everything event, not a
   percentage.

## What is NOT in the budget, deliberately

**Recommendation quality.** Wear-through rate, like-rate and wardrobe coverage
are tracked and they must not gate a deploy. §B1's reasoning: you would never
ship an experiment. A suggestion being *bad* is a product problem; a suggestion
being *absent* is a reliability problem, and only the second one is here.

**The two Phase 7 rate SLIs** (rationale cache hit rate, `validator.reject`
rate). They are exit criteria tracked toward a threshold. §D3 says "resist
adding more; unactionable alerts train people to ignore pages", and neither
needs a deploy stopped.

---

## What makes this hard to sign today

Three of the six SLIs **cannot currently be measured**, and signing a policy
that cannot be evaluated is how a control becomes a document.

| SLI | Measurable now? | Blocked on |
|---|---|---|
| Suggestion availability | **yes** — `/ops/alerts` `api_5xx` | — |
| Suggestion latency | **yes** — measured 6.4-22ms warm, 1.2-1.5s cold | — |
| Ingest success | **yes** — `ops_ingest_stats(24)` | — |
| Ingest completeness | **yes** — same source | — |
| Tag accuracy | **no** | the 500-image golden set; `eval/golden/labels.jsonl` does not exist and all three eval entry points exit 2 |
| Data durability | **partly** | restore drill passes (2.8s, verified). Backups are in the SAME storage account as the data — §C5 asks for a second — so this survives a bad migration, not a deleted account |

There is also no burn-rate alerting: the four §D3 alerts are threshold alerts,
not multi-window burn-rate alerts, and nothing computes budget consumption over
a rolling 30 days. **The policy above therefore has no trigger.** Writing "a
freeze at 50%" without a number that reaches 50% is the decoration §B1 warns
about.

## What signing this actually commits you to

Be clear-eyed: at n=1 with no traffic, the availability and latency budgets will
essentially never be consumed, so clauses 1 and 2 will not bind for months. The
clause that can bite today is **3** — data durability — and it is the one worth
agreeing to now, because the failure it covers does not need traffic to happen.

The honest options:

- **Sign clause 3 now**, and mark 1 and 2 as taking effect when there is a
  measurable burn rate. Small, true, and enforceable today.
- **Sign the whole thing now** and accept that two thirds of it is aspirational
  until there is traffic and a golden set.
- **Defer entirely** until burn-rate alerting exists, and record that Phase 9's
  criterion is genuinely unmet rather than papered over.

The first is the recommendation. It is the only one that is both agreed and
true.

**DECIDED 2026-09-17: the first.** Clause 3 is in force. Clauses 1 and 2 take
effect automatically the day burn-rate alerting lands — no re-signing required,
because the commitment was made here and only the trigger was missing.

---

## Sign-off

| | |
|---|---|
| Policy version | 1 |
| Drafted | 2026-09-17 |
| Agreed by | Suraj Kumar |
| Date agreed | 2026-09-17 |
| Clauses in force | **3 only** — data durability. Binding today. |
| Clauses agreed but dormant | 1 and 2 — no burn rate is computed, so neither has a trigger |
| What activates 1 and 2 | multi-window burn-rate alerting over a rolling 30 days |
| Review | when burn-rate alerting lands, or when traffic makes clauses 1-2 measurable |

## What clause 3 obliges, starting now

Not a percentage and not a page — a standing rule:

- Losing a user's garment rows or originals is a **stop-everything** event. Not
  triaged against other work, not scheduled. Everything else waits.
- It applies to ONE incident. There is no "acceptable rate" to measure against
  and no budget to spend down.

**And it is signed over a gap that is still open.** Backups are logical dumps
into the SAME storage account as the data they protect (`scripts/backup.py`
says so in its own docstring). That survives a bad migration; it does not
survive a deleted or compromised account. §C5 asks for a second account and
there is one.

Signing clause 3 over that gap is deliberate, not an oversight: the clause is
what makes closing it a priority rather than a wish. **Closing it is the first
piece of work this signature obliges.**

---

Clause 3 is in force from 2026-09-17. Phase 9's "error budget policy signed
off" criterion is **met for clause 3**, and deliberately not claimed for
clauses 1 and 2 — recording that accurately is worth more than a document that
claims a control nobody can trigger.
