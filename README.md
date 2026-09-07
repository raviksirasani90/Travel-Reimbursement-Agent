# Travel Reimbursement Approval Agent

Target architecture and feasibility prototype for an enterprise agent that reviews
domestic travel reimbursement claims (flights, hotels, meals, taxis) against policy,
receipts, approval thresholds, and duplicate/fraud rules — and returns a structured
**Approve / Partially Approve / Reject / Manual Review** decision with evidence.

This is a design + feasibility exercise: mock policy, mock claims, and a small SQLite
approval matrix are used in place of a live policy KB / Finance ERP so the whole
pipeline runs offline and deterministically.

## Contents

| Path | What it is |
|---|---|
| `docs/Architecture_Note.docx` | Full architecture note: problem decomposition, agent pattern, component & sequence diagrams, tool contracts, reliability/audit design, enterprise readiness, deployment architecture, risks & next steps |
| `diagrams/` | Component architecture, sequence/workflow, and on-prem-vs-cloud deployment diagrams (PNG) |
| `src/agent.py` | Runnable prototype: policy retrieval, receipt validation, duplicate check, approval matrix, and decision engine, orchestrated end-to-end |
| `data/policy_kb.json` | Mock domestic travel policy, chunked and citable (id + section + text) |
| `data/sample_claims.json` | 6 sample claims covering approve / partial / reject-eligible / manual-review paths |
| `scripts/build_db.py` | Builds `data/finance_db.sqlite` (approval matrix, category caps, reimbursement history) |
| `outputs/sample_decisions.json` | Structured decision output for all 6 sample claims |
| `outputs/workflow_trace.txt` | Human-readable execution trace for every claim (audit-style) |

## Architecture at a glance

A deterministic orchestrator (state-graph pattern, intended for LangGraph in
production) coordinates four narrow tools — policy retrieval (RAG), receipt
validation, duplicate check, and approval-matrix lookup — and hands their combined
evidence to a decision engine. An LLM (Azure OpenAI in the target design) is used
**only** to generate a grounded natural-language rationale and a confidence signal;
all arithmetic, cap comparisons, and the approve/reject decision itself are
deterministic code, so every decision is reproducible and auditable line-by-line.

See `docs/Architecture_Note.docx` for the full write-up, and `diagrams/` for the
component, sequence, and deployment diagrams.

## Running the prototype

Requires Python 3.10+, standard library only (no pip installs needed).

```bash
python scripts/build_db.py     # creates data/finance_db.sqlite
python src/agent.py            # runs all 6 sample claims, prints decisions
```

Output:

```
CLM-1001: Manual Review | approved=16850/16850 | confidence=1.0 | flags=['above_auto_approval_threshold']
CLM-1002: Partially Approve | approved=20600/23100 | confidence=1.0 | flags=[]
CLM-1003: Manual Review | approved=7000/11300 | confidence=0.6 | flags=['hotel:missing_receipt']
CLM-1004: Manual Review | approved=8200/8200 | confidence=0.7 | flags=['duplicate_of:CLM-1001']
CLM-1005: Manual Review | approved=0/53300 | confidence=1.0 | flags=['exception_claimed']
CLM-1006: Approve | approved=6580/6580 | confidence=1.0 | flags=[]
```

Full JSON decisions are written to `outputs/sample_decisions.json` and a step-by-step
trace to `outputs/workflow_trace.txt`.

## Decision routing (by design)

A claim is routed to **Manual Review** whenever any of the following hold, regardless
of confidence score:

- A required receipt is missing or invalid
- The claim (or a line item) matches a previously reimbursed claim on
  vendor + amount + date
- Any line item exceeds its category cap by more than 20%
- The claim invokes a policy exception
- The approved total exceeds the system auto-approval threshold (routed to the
  correct human approver, not rejected)
- The computed confidence score falls below 0.75

## Known limitations / next steps

- Policy retrieval here is keyword match, not embeddings — production needs a real
  vector index (Azure AI Search, pgvector, etc.) over versioned policy documents.
- Receipt validation is mocked — production needs a real OCR / Document AI call with
  field cross-checking against the actual scanned receipt.
- Split-claim fraud (same expense divided across multiple small claims to dodge
  thresholds) needs a time-windowed aggregate check across an employee's recent
  claims, not just the exact-match duplicate check implemented here.
- No multi-currency, multi-entity, or international travel support in this scope.

See §11 of the architecture note for the full risk/governance discussion.

## License

MIT — see `LICENSE`.
