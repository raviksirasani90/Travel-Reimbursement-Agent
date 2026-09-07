"""
Travel Reimbursement Approval Agent - Feasibility Prototype
=============================================================
This is a minimal, dependency-light simulation of the target architecture.
It mirrors the node/tool boundaries described in the architecture note:

  ClaimIntakeService -> PolicyRetrievalTool -> ReceiptValidationTool
  -> DuplicateCheckTool -> ApprovalMatrixTool -> DecisionEngine -> AuditTrail

In production, PolicyRetrievalTool would be a RAG service over a vector DB
(policy PDFs chunked + embedded), ReceiptValidationTool would call a
Document AI / OCR service, and the orchestrator would be a LangGraph state
machine calling Azure OpenAI for the "reasoning + explanation" step. Here,
retrieval is keyword-based and the LLM call is mocked/stubbed so the trace
can run fully offline and deterministically for grading/demo purposes.
"""

import json
import sqlite3
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "finance_db.sqlite"
POLICY_PATH = DATA_DIR / "policy_kb.json"

CONFIDENCE_THRESHOLD = 0.75  # below this -> Manual Review regardless of rule outcome


# ---------------------------------------------------------------------------
# Tool: Policy Retrieval (mock RAG)
# ---------------------------------------------------------------------------
class PolicyRetrievalTool:
    """Contract:
    input: {query: str, top_k: int}
    output: [{id, section, text, score}]
    failure: returns [] with retrieval_status='no_match' -> caller must route
             to Manual Review if a required category has zero grounded policy.
    """

    def __init__(self, kb_path=POLICY_PATH):
        self.kb = json.loads(Path(kb_path).read_text())

    def retrieve(self, query: str, top_k: int = 2):
        terms = set(re.findall(r"[a-z]+", query.lower()))
        scored = []
        for doc in self.kb:
            hay = (doc["section"] + " " + doc["text"]).lower()
            score = sum(1 for t in terms if t in hay)
            if score > 0:
                scored.append({**doc, "score": score})
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]


# ---------------------------------------------------------------------------
# Tool: Receipt Validation (mock Document AI / OCR)
# ---------------------------------------------------------------------------
class ReceiptValidationTool:
    """Contract:
    input: {line_item}
    output: {is_valid, reason, confidence}
    failure: missing receipt_id/receipt_attached=False -> is_valid=False,
             reason='missing_receipt' (never silently passes).
    """

    def validate(self, line_item: dict):
        if not line_item.get("receipt_attached") or not line_item.get("receipt_id"):
            return {"is_valid": False, "reason": "missing_receipt", "confidence": 1.0}
        if line_item.get("amount", 0) <= 0:
            return {"is_valid": False, "reason": "invalid_amount", "confidence": 1.0}
        # Mock OCR cross-check: in production, extracted vendor/amount/date from
        # the receipt image would be diffed against the claimed line item here.
        return {"is_valid": True, "reason": "ocr_match", "confidence": 0.95}


# ---------------------------------------------------------------------------
# Tool: Duplicate Check
# ---------------------------------------------------------------------------
class DuplicateCheckTool:
    """Contract:
    input: {employee_id, vendor, amount, travel_date}
    output: {is_duplicate, matched_claim_id}
    failure: DB unavailable -> raises ToolUnavailableError; orchestrator
             must treat this as non-fatal-but-blocking -> Manual Review
             (never assume "no duplicate" on tool failure).
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def check(self, employee_id, vendor, amount, travel_date):
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """SELECT claim_id FROM reimbursement_history
               WHERE employee_id=? AND vendor=? AND amount=? AND travel_date=?""",
            (employee_id, vendor, amount, travel_date),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return {"is_duplicate": True, "matched_claim_id": row[0]}
        return {"is_duplicate": False, "matched_claim_id": None}


# ---------------------------------------------------------------------------
# Tool: Approval Matrix + Category Cap Lookup
# ---------------------------------------------------------------------------
class ApprovalMatrixTool:
    """Contract:
    input: {total_amount}
    output: {approver_level, auto_approvable}
    input: {category, city_tier}
    output: {cap_amount}
    failure: no matching row -> defaults to most conservative
             (Finance Controller, auto_approvable=False).
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def approver_for(self, total_amount: float):
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """SELECT approver_level, auto_approvable FROM approval_matrix
               WHERE ? BETWEEN min_amount AND max_amount""",
            (total_amount,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return {"approver_level": "Finance Controller", "auto_approvable": False}
        return {"approver_level": row[0], "auto_approvable": bool(row[1])}

    def cap_for(self, category: str, city_tier: str):
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            """SELECT cap_amount FROM category_caps
               WHERE category=? AND (city_tier=? OR city_tier='any')
               ORDER BY city_tier DESC LIMIT 1""",
            (category, city_tier),
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# Decision Engine
# ---------------------------------------------------------------------------
class DecisionEngine:
    def __init__(self):
        self.policy = PolicyRetrievalTool()
        self.receipts = ReceiptValidationTool()
        self.dupes = DuplicateCheckTool()
        self.matrix = ApprovalMatrixTool()

    def evaluate(self, claim: dict) -> dict:
        trace = []
        evidence = []
        flags = []
        approved_total = 0
        requested_total = sum(li["amount"] for li in claim["line_items"])

        trace.append(f"[intake] claim_id={claim['claim_id']} employee={claim['employee_id']} "
                      f"requested_total={requested_total}")

        # 1. Exception claims never auto-approve
        if claim.get("exception_claimed"):
            docs = self.policy.retrieve("exception justification manual review")
            evidence += [d["id"] for d in docs]
            trace.append("[rule] exception_claimed=True -> forced Manual Review (POL-EXCEPTION-01)")
            return self._finalize(claim, "Manual Review", 0, requested_total,
                                   flags + ["exception_claimed"], evidence, trace,
                                   confidence=1.0,
                                   rationale="Claim invokes a policy exception. Per POL-EXCEPTION-01, "
                                             "exception claims always require Manual Review with the "
                                             "written justification reviewed by Finance, regardless of amount.")

        # 2. Per line item: receipt validation + policy cap check
        line_results = []
        for li in claim["line_items"]:
            rcpt = self.receipts.validate(li)
            cap_docs = self.policy.retrieve(f"{li['category']} cap policy")
            cap = self.matrix.cap_for(li["category"], claim.get("city_tier", "any"))
            evidence += [d["id"] for d in cap_docs]

            over_cap_pct = 0
            if cap and li["amount"] > cap:
                over_cap_pct = (li["amount"] - cap) / cap * 100

            approvable_amount = min(li["amount"], cap) if cap else li["amount"]

            if not rcpt["is_valid"]:
                flags.append(f"{li['category']}:{rcpt['reason']}")
                approvable_amount = 0
                trace.append(f"[receipt] {li['category']} ({li.get('receipt_id')}) -> INVALID "
                             f"({rcpt['reason']})")
            else:
                trace.append(f"[receipt] {li['category']} ({li['receipt_id']}) -> valid, "
                             f"cap={cap}, amount={li['amount']}, over_cap_pct={over_cap_pct:.0f}%")

            if over_cap_pct > 20:
                flags.append(f"{li['category']}:over_cap_{over_cap_pct:.0f}pct")

            line_results.append({
                "category": li["category"],
                "requested": li["amount"],
                "cap": cap,
                "approvable": approvable_amount,
                "receipt_valid": rcpt["is_valid"],
                "over_cap_pct": round(over_cap_pct, 1),
            })
            approved_total += approvable_amount

        # 3. Duplicate check (per line item, keyed on vendor+amount+date)
        for li in claim["line_items"]:
            dup = self.dupes.check(claim["employee_id"], li["vendor"], li["amount"], li["date"])
            if dup["is_duplicate"]:
                flags.append(f"duplicate_of:{dup['matched_claim_id']}")
                trace.append(f"[duplicate] {li['category']} matches prior claim "
                             f"{dup['matched_claim_id']} -> flagged")
                dup_docs = self.policy.retrieve("duplicate fraud control")
                evidence += [d["id"] for d in dup_docs]

        # 4. Any line item over cap by >20% -> forced Manual Review (policy rule)
        hard_manual_review = any("over_cap_" in f for f in flags) or any("duplicate_of" in f for f in flags)

        # 5. Approval matrix lookup on the (post-adjustment) approvable total
        matrix_result = self.matrix.approver_for(approved_total)
        trace.append(f"[approval_matrix] approved_total={approved_total} -> "
                      f"approver={matrix_result['approver_level']}, "
                      f"auto_approvable={matrix_result['auto_approvable']}")

        # 6. Confidence scoring (simplified: penalize missing receipts / flags)
        confidence = 1.0
        if any("missing_receipt" in f for f in flags):
            confidence -= 0.4
        if any("duplicate_of" in f for f in flags):
            confidence -= 0.3
        if any("over_cap_" in f for f in flags):
            confidence -= 0.2
        confidence = max(confidence, 0.0)

        # 7. Decision logic
        missing_receipt = any("missing_receipt" in f for f in flags)
        if missing_receipt or hard_manual_review or confidence < CONFIDENCE_THRESHOLD:
            decision = "Manual Review"
        elif approved_total == 0:
            decision = "Reject"
        elif approved_total < requested_total:
            decision = "Partially Approve"
        elif not matrix_result["auto_approvable"]:
            decision = "Manual Review"
            flags.append("above_auto_approval_threshold")
        else:
            decision = "Approve"

        rationale_parts = []
        if decision == "Approve":
            rationale_parts.append("All line items are within category caps, receipts are valid, "
                                    "no duplicates detected, and the total is within the auto-approval "
                                    "threshold (POL-APPROVAL-01).")
        if decision == "Partially Approve":
            rationale_parts.append("Some line items exceed policy caps or failed receipt validation; "
                                    "the compliant portion is approved and the remainder is disallowed.")
        if missing_receipt:
            rationale_parts.append("One or more line items lack a mandatory receipt (POL-RECEIPT-01), "
                                    "which blocks auto-approval for those items.")
        if any("duplicate_of" in f for f in flags):
            rationale_parts.append("A line item matches a previously reimbursed claim on vendor, amount, "
                                    "and date (POL-DUPLICATE-01) and requires Finance investigation.")
        if any("over_cap_" in f for f in flags):
            rationale_parts.append("A line item exceeds its category cap by more than 20% "
                                    "(POL-APPROVAL-01), which requires human sign-off even if the total "
                                    "is within threshold.")
        if decision == "Manual Review" and matrix_result["approver_level"] != "System (Auto)" and not flags:
            rationale_parts.append(f"Approved total requires {matrix_result['approver_level']} sign-off "
                                    "per the approval matrix.")

        return self._finalize(claim, decision, approved_total, requested_total, flags,
                               list(dict.fromkeys(evidence)), trace, confidence,
                               " ".join(rationale_parts), line_results, matrix_result)

    def _finalize(self, claim, decision, approved_total, requested_total, flags,
                  evidence, trace, confidence, rationale, line_results=None,
                  matrix_result=None):
        result = {
            "decision_id": str(uuid.uuid4()),
            "claim_id": claim["claim_id"],
            "employee_id": claim["employee_id"],
            "decision": decision,
            "requested_amount": requested_total,
            "approved_amount": approved_total,
            "rejected_amount": requested_total - approved_total,
            "confidence": round(confidence, 2),
            "flags": flags,
            "policy_references": evidence,
            "line_item_breakdown": line_results or [],
            "required_approver": matrix_result["approver_level"] if matrix_result else "N/A",
            "rationale": rationale,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        trace.append(f"[decision] {decision} | approved={approved_total}/{requested_total} "
                      f"| confidence={confidence:.2f} | flags={flags or 'none'}")
        result["_trace"] = trace
        return result


def run_batch(claims_path, out_json_path, out_trace_path):
    claims = json.loads(Path(claims_path).read_text())
    engine = DecisionEngine()
    decisions = []
    trace_lines = []
    conn = sqlite3.connect(DB_PATH)
    for claim in claims:
        result = engine.evaluate(claim)
        trace_lines.append(f"\n===== Claim {claim['claim_id']} =====")
        trace_lines.extend(result.pop("_trace"))
        decisions.append(result)
        # Ledger update: record this claim's line items so later claims in the
        # batch (or future submissions) can be checked against it for duplicates.
        for li in claim["line_items"]:
            conn.execute(
                "INSERT INTO reimbursement_history VALUES (?,?,?,?,?,?)",
                (claim["employee_id"], li["vendor"], li["amount"], li["date"],
                 claim["claim_id"], result["decision"]),
            )
        conn.commit()
    conn.close()

    Path(out_json_path).write_text(json.dumps(decisions, indent=2))
    Path(out_trace_path).write_text("\n".join(trace_lines))
    return decisions


if __name__ == "__main__":
    decisions = run_batch(
        DATA_DIR / "sample_claims.json",
        DATA_DIR.parent / "outputs" / "sample_decisions.json",
        DATA_DIR.parent / "outputs" / "workflow_trace.txt",
    )
    for d in decisions:
        print(f"{d['claim_id']}: {d['decision']} | approved={d['approved_amount']}/"
              f"{d['requested_amount']} | confidence={d['confidence']} | flags={d['flags']}")
