import sqlite3
import os

DB_FILES = ["billbuddy.db", "ledger_web.db"]

for db_file in DB_FILES:
    if not os.path.exists(db_file):
        continue
    print(f"=== Checking {db_file} ===")
    con = sqlite3.connect(db_file)
    cur = con.cursor()

    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("Existing tables:", tables)

    # Helper to add column if missing
    def add_col_if_missing(table, col_name, col_type):
        if table not in tables:
            return
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
        if col_name not in cols:
            print(f"  Adding column '{col_name}' ({col_type}) to table '{table}' in {db_file}")
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")

    add_col_if_missing("groups", "currency", "VARCHAR(10) DEFAULT 'USD'")
    add_col_if_missing("expenses", "currency", "VARCHAR(10) DEFAULT 'USD'")
    add_col_if_missing("expenses", "category", "VARCHAR(50) DEFAULT 'General'")
    add_col_if_missing("expenses", "split_type", "VARCHAR(20) DEFAULT 'EQUAL'")
    add_col_if_missing("expenses", "fx_rate_to_group", "NUMERIC(12,6) DEFAULT 1.0")
    add_col_if_missing("expense_splits", "split_value", "NUMERIC(10,2)")
    add_col_if_missing("monthly_periods", "target_currency", "VARCHAR(10)")
    add_col_if_missing("monthly_periods", "settlement_fx_rate", "NUMERIC(12,6)")
    add_col_if_missing("account_settings", "preferred_currency", "VARCHAR(10) DEFAULT 'USD'")
    add_col_if_missing("pending_uploads", "account_id", "INTEGER")

    con.commit()
    con.close()

print("\nDatabase migration check completed.")
