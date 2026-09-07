"""
Builds the mock finance_db.sqlite used by src/agent.py.
Run this once before running the prototype:

    python scripts/build_db.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "finance_db.sqlite"


def build():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""CREATE TABLE approval_matrix (
        min_amount INTEGER, max_amount INTEGER,
        approver_level TEXT, auto_approvable INTEGER)""")
    cur.executemany("INSERT INTO approval_matrix VALUES (?,?,?,?)", [
        (0, 10000, "System (Auto)", 1),
        (10001, 50000, "Manager", 0),
        (50001, 999999999, "Finance Controller", 0),
    ])

    cur.execute("""CREATE TABLE category_caps (
        category TEXT, city_tier TEXT, cap_amount INTEGER)""")
    cur.executemany("INSERT INTO category_caps VALUES (?,?,?)", [
        ("flight", "any", 15000),
        ("hotel", "metro", 7000),
        ("hotel", "non_metro", 5000),
        ("meals", "any", 1500),
        ("taxi", "any", 999999),
    ])

    cur.execute("""CREATE TABLE reimbursement_history (
        employee_id TEXT, vendor TEXT, amount INTEGER,
        travel_date TEXT, claim_id TEXT, status TEXT)""")

    conn.commit()
    conn.close()
    print(f"Created {DB_PATH}")


if __name__ == "__main__":
    build()
