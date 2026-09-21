#!/usr/bin/env python3
# filepath: budget_cli.py
"""
Budget CLI: SQLite-backed budgeting/expense tracker.

Usage (examples):
  python3 Budget.py init--db
  python3 Budget.py tx add --amount 1500 --category Wages --type income --desc "November Paycheck" --date 2025-11-30
  python3 Budget.py tx list --from 2025-11-01 --to 2025-11-30
  python3 Budget.py tx list
  python3 Budget.py budget set --month 2025-11 --category Coffee --amount 120
  python3 Budget.py report month --month 2025-11
  python3 Budget.py recurring add --amount 60 --category Internet --type expense --desc "ISP" --date 2025-01-15 --freq monthly --interval 1
  python3 Budget.py recurring apply --month 2025-11
  python3 Budget.py tx export --to-file my_txs.csv --from 2025-01-01 --to 2025-12-31
  python3 Budget.py tx import --from-file my_txs.csv --strict
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import os
import sqlite3
import sys
from typing import Any, Iterable, Optional, Sequence, Tuple

DB_PATH = os.environ.get("BUDGET_DB", "test_budget.db")      

#  Utilities 

def connect() -> sqlite3.Connection:
    """Open DB connection with sane defaults."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indices if missing."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY,
            date TEXT NOT NULL,                         -- ISO YYYY-MM-DD
            amount REAL NOT NULL CHECK(amount > 0),    -- always positive; type decides sign
            category TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('expense','income')),
            description TEXT DEFAULT '',
            account TEXT DEFAULT 'cash',
            recurring_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY(recurring_id) REFERENCES recurring(id)
        );

        CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_tx_cat ON transactions(category);
        CREATE INDEX IF NOT EXISTS idx_tx_type ON transactions(type);

        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY,
            month TEXT NOT NULL,                        -- YYYY-MM
            category TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount >= 0),
            UNIQUE(month, category)
        );

        CREATE INDEX IF NOT EXISTS idx_bud_month ON budgets(month);

        CREATE TABLE IF NOT EXISTS recurring (
            id INTEGER PRIMARY KEY,
            start_date TEXT NOT NULL,                  -- first occurrence date
            amount REAL NOT NULL CHECK(amount > 0),
            category TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('expense','income')),
            description TEXT DEFAULT '',
            account TEXT DEFAULT 'cash',
            frequency TEXT NOT NULL CHECK(frequency IN ('monthly','weekly','yearly')),
            interval INTEGER NOT NULL DEFAULT 1 CHECK(interval >= 1),
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()

def parse_date(s: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except Exception as e:
        raise SystemExit(f"Invalid date '{s}'. Use YYYY-MM-DD.") from e

def parse_month(s: str) -> Tuple[int, int]:
    try:
        y, m = s.split("-", 1)
        year, month = int(y), int(m)
        if not (1 <= month <= 12):
            raise ValueError
        return year, month
    except Exception as e:
        raise SystemExit(f"Invalid month '{s}'. Use YYYY-MM.") from e

def month_bounds(month_str: str) -> Tuple[dt.date, dt.date]:
    y, m = parse_month(month_str)
    start = dt.date(y, m, 1)
    if m == 12:
        end = dt.date(y + 1, 1, 1) - dt.timedelta(days=1)
    else:
        end = dt.date(y, m + 1, 1) - dt.timedelta(days=1)
    return start, end

def clamp_day(year: int, month: int, day: int) -> dt.date:
    """Clamp to last day of target month if day overflow occurs."""
    last_day = (dt.date(year + (month // 12), (month % 12) + 1, 1) - dt.timedelta(days=1)).day if month != 12 else 31
    safe_day = min(day, last_day)
    return dt.date(year, month, safe_day)

def fmt_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"

def to_signed(amount: float, tx_type: str) -> float:
    return -amount if tx_type == "expense" else amount

def fail(msg: str, code: int = 2) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    raise SystemExit(code)

def now_utc_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")

#  Data access 

def add_transaction(
    conn: sqlite3.Connection,
    *,
    date: dt.date,
    amount: float,
    category: str,
    tx_type: str,
    description: str = "",
    account: str = "cash",
    recurring_id: Optional[int] = None,
) -> int:
    if amount <= 0:
        fail("amount must be > 0")
    if tx_type not in ("expense", "income"):
        fail("type must be 'expense' or 'income'")
    cur = conn.execute(
        """
        INSERT INTO transactions (date, amount, category, type, description, account, recurring_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (date.isoformat(), amount, category, tx_type, description, account, recurring_id, now_utc_iso(), now_utc_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)

def list_transactions(
    conn: sqlite3.Connection,
    *,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    category: Optional[str] = None,
    account: Optional[str] = None,
    tx_type: Optional[str] = None,
    search: Optional[str] = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM transactions WHERE 1=1"
    args: list[Any] = []
    if date_from:
        sql += " AND date >= ?"
        args.append(date_from.isoformat())
    if date_to:
        sql += " AND date <= ?"
        args.append(date_to.isoformat())
    if category:
        sql += " AND category = ?"
        args.append(category)
    if account:
        sql += " AND account = ?"
        args.append(account)
    if tx_type:
        if tx_type not in ("expense", "income"):
            fail("type must be expense|income when filtering")
        sql += " AND type = ?"
        args.append(tx_type)
    if search:
        sql += " AND (description LIKE ? OR category LIKE ? OR account LIKE ?)"
        like = f"%{search}%"
        args.extend([like, like, like])
    sql += " ORDER BY date ASC, id ASC"
    cur = conn.execute(sql, args)
    return list(cur.fetchall())

def get_transaction(conn: sqlite3.Connection, tx_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,))
    return cur.fetchone()

def update_transaction(conn: sqlite3.Connection, tx_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"date", "amount", "category", "type", "description", "account"}
    for k in fields:
        if k not in allowed:
            fail(f"invalid field '{k}' for edit")
    if "type" in fields and fields["type"] not in ("expense", "income"):
        fail("type must be 'expense' or 'income'")
    set_clauses = [f"{k} = ?" for k in fields]
    args = list(fields.values())
    set_clauses.append("updated_at = ?")
    args.append(now_utc_iso())
    args.append(tx_id)
    sql = f"UPDATE transactions SET {', '.join(set_clauses)} WHERE id = ?"
    cur = conn.execute(sql, args)
    if cur.rowcount == 0:
        fail(f"transaction {tx_id} not found", 1)
    conn.commit()

def delete_transaction(conn: sqlite3.Connection, tx_id: int) -> None:
    cur = conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    if cur.rowcount == 0:
        fail(f"transaction {tx_id} not found", 1)
    conn.commit()

def upsert_budget(conn: sqlite3.Connection, month: str, category: str, amount: float) -> None:
    if amount < 0:
        fail("budget amount must be >= 0")
    conn.execute(
        """
        INSERT INTO budgets (month, category, amount)
        VALUES (?, ?, ?)
        ON CONFLICT(month, category) DO UPDATE SET amount = excluded.amount
        """,
        (month, category, amount),
    )
    conn.commit()

def list_budgets(conn: sqlite3.Connection, month: Optional[str]) -> list[sqlite3.Row]:
    if month:
        return list(conn.execute("SELECT * FROM budgets WHERE month = ? ORDER BY category", (month,)).fetchall())
    return list(conn.execute("SELECT * FROM budgets ORDER BY month, category").fetchall())

def add_recurring(
    conn: sqlite3.Connection,
    *,
    start_date: dt.date,
    amount: float,
    category: str,
    tx_type: str,
    description: str,
    account: str,
    frequency: str,
    interval: int,
) -> int:
    if frequency not in ("monthly", "weekly", "yearly"):
        fail("frequency must be monthly|weekly|yearly")
    if interval < 1:
        fail("interval must be >= 1")
    cur = conn.execute(
        """
        INSERT INTO recurring (start_date, amount, category, type, description, account, frequency, interval, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (start_date.isoformat(), amount, category, tx_type, description, account, frequency, interval),
    )
    conn.commit()
    return int(cur.lastrowid)

def list_recurring_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM recurring ORDER BY active DESC, id ASC").fetchall())

def set_recurring_active(conn: sqlite3.Connection, rid: int, active: bool) -> None:
    cur = conn.execute("UPDATE recurring SET active = ? WHERE id = ?", (1 if active else 0, rid))
    if cur.rowcount == 0:
        fail(f"recurring rule {rid} not found", 1)
    conn.commit()

def transaction_exists(
    conn: sqlite3.Connection,
    *,
    date: dt.date,
    amount: float,
    category: str,
    tx_type: str,
    description: str,
    account: str,
    recurring_id: Optional[int],
) -> bool:
    cur = conn.execute(
        """
        SELECT 1 FROM transactions
        WHERE date = ? AND amount = ? AND category = ? AND type = ? AND description = ? AND account = ?
              AND (recurring_id IS ? OR recurring_id = ?)
        LIMIT 1
        """,
        (date.isoformat(), amount, category, tx_type, description, account, None if recurring_id is None else recurring_id, recurring_id),
    )
    return cur.fetchone() is not None

#  Reporting 

def month_report(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    start, end = month_bounds(month)
    rows = list_transactions(conn, date_from=start, date_to=end)
    income = sum(to_signed(r["amount"], r["type"]) for r in rows if r["type"] == "income")
    expenses = sum(to_signed(r["amount"], r["type"]) for r in rows if r["type"] == "expense")
    net = income + expenses
    by_cat: dict[str, float] = {}
    for r in rows:
        signed = to_signed(r["amount"], r["type"])
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + signed
    # budgets / variance (expense side)
    bcur = conn.execute("SELECT category, amount FROM budgets WHERE month = ?", (month,))
    budgets = {row["category"]: row["amount"] for row in bcur.fetchall()}
    variance = []
    for cat, actual_signed in by_cat.items():
        actual_exp = abs(actual_signed) if actual_signed < 0 else 0.0
        b = budgets.get(cat, 0.0)
        variance.append((cat, b, actual_exp, b - actual_exp))
    # include budgeted categories with no spend
    for cat, b in budgets.items():
        if cat not in by_cat:
            variance.append((cat, b, 0.0, b - 0.0))
    variance.sort(key=lambda t: t[3])  # worst variance first
    return {
        "month": month,
        "income": income,
        "expenses": expenses,
        "net": net,
        "by_category": dict(sorted(by_cat.items(), key=lambda kv: kv[0].lower())),
        "variance": variance,
        "count": len(rows),
    }

# CSV 

CSV_HEADERS = ["date", "amount", "category", "type", "description", "account"]

def export_csv(conn: sqlite3.Connection, path: str, **filters: Any) -> int:
    rows = list_transactions(conn, **filters)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "date": r["date"],
                    "amount": f"{r['amount']:.2f}",
                    "category": r["category"],
                    "type": r["type"],
                    "description": r["description"] or "",
                    "account": r["account"] or "",
                }
            )
    return len(rows)

def import_csv(conn: sqlite3.Connection, path: str, *, strict: bool, dry_run: bool) -> Tuple[int, int]:
    if not os.path.exists(path):
        fail(f"file not found: {path}")
    added, skipped = 0, 0
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = [h.strip().lower() for h in reader.fieldnames or []]
        if header != CSV_HEADERS:
            fail(f"CSV header must be: {', '.join(CSV_HEADERS)}")
        for i, row in enumerate(reader, start=2):
            try:
                date = parse_date(row["date"].strip())
                amount = float(row["amount"])
                if amount <= 0:
                    raise ValueError("amount must be > 0")
                category = row["category"].strip()
                tx_type = row["type"].strip().lower()
                if tx_type not in ("expense", "income"):
                    raise ValueError("type must be expense|income")
                description = row.get("description", "").strip()
                account = row.get("account", "cash").strip() or "cash"
                if not dry_run:
                    add_transaction(
                        conn,
                        date=date,
                        amount=amount,
                        category=category,
                        tx_type=tx_type,
                        description=description,
                        account=account,
                    )
                added += 1
            except Exception as e:
                if strict:
                    fail(f"Row {i}: {e}")
                skipped += 1
    return added, skipped

#  Recurrence generation 

def iter_occurrences(rule: sqlite3.Row, month: str) -> Iterable[dt.date]:
    start, end = month_bounds(month)
    first = parse_date(rule["start_date"])
    if first > end or rule["active"] == 0:
        return []
    freq = rule["frequency"]
    interval = int(rule["interval"])
    dates: list[dt.date] = []
    if freq == "weekly":
        # align to first weekday on/after start bound
        # Step from first occurrence to >= start
        d = first
        # fast-forward to start
        if d < start:
            delta_days = (start - d).days
            steps = (delta_days + (7 * interval) - 1) // (7 * interval)
            d = d + dt.timedelta(days=steps * 7 * interval)
        while d <= end:
            dates.append(d)
            d = d + dt.timedelta(days=7 * interval)
    elif freq == "monthly":
        # iterate months
        y, m = start.year, start.month
        # find first month >= start where (year,month) >= (start.year, start.month or first)
        base = dt.date(start.year, start.month, 1)
        # start from max(first.month, start.month)
        cy, cm = max(first.year, y), max(first.month if first.year == y else 1, m) if first.year == y else first.month
        # Align to month >= start
        # We'll iterate k steps from first's y,m in "interval" month jumps
        # Compute k such that occurrence date >= start
        def add_months(y: int, m: int, k: int) -> Tuple[int, int]:
            tot = (y * 12 + (m - 1)) + k * interval
            ny, nm = divmod(tot, 12)
            return ny, nm + 1
        # find k
        from_month_tot = first.year * 12 + (first.month - 1)
        start_month_tot = start.year * 12 + (start.month - 1)
        if start_month_tot <= from_month_tot:
            k0 = 0
        else:
            diff = start_month_tot - from_month_tot
            k0 = (diff + interval - 1) // interval
        k = k0
        while True:
            yy, mm = add_months(first.year, first.month, k)
            if dt.date(yy, mm, 1) > end:
                break
            d = clamp_day(yy, mm, first.day)
            if start <= d <= end:
                dates.append(d)
            k += 1
    else:  # yearly
        y = max(first.year, month_bounds(month)[0].year)
        # Step by interval years
        # find first y' >= start.year with (y' - first.year) % interval == 0
        diff = (y - first.year) % interval
        if diff != 0:
            y += (interval - diff)
        while y <= month_bounds(month)[1].year:
            dday = first.day
            dmonth = first.month
            try:
                d = dt.date(y, dmonth, dday)
            except ValueError:
                # Feb 29 clamp to Feb 28 on non-leap years
                last_day = (dt.date(y, dmonth % 12 + 1, 1) - dt.timedelta(days=1)).day if dmonth != 12 else 31
                d = dt.date(y, dmonth, min(dday, last_day))
            if month_bounds(month)[0] <= d <= month_bounds(month)[1]:
                dates.append(d)
            y += interval
    return dates

def apply_recurring(conn: sqlite3.Connection, month: str) -> Tuple[int, int]:
    rules = list_recurring_rules(conn)
    created, skipped = 0, 0
    for r in rules:
        if r["active"] == 0:
            continue
        for d in iter_occurrences(r, month):
            exists = transaction_exists(
                conn,
                date=d,
                amount=r["amount"],
                category=r["category"],
                tx_type=r["type"],
                description=r["description"],
                account=r["account"],
                recurring_id=r["id"],
            )
            if exists:
                skipped += 1
                continue
            add_transaction(
                conn,
                date=d,
                amount=r["amount"],
                category=r["category"],
                tx_type=r["type"],
                description=r["description"],
                account=r["account"],
                recurring_id=r["id"],
            )
            created += 1
    return created, skipped

#  Printing 

def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join("{:" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))

def print_transactions(rows: list[sqlite3.Row]) -> None:
    headers = ["id", "date", "type", "category", "amount", "account", "description"]
    data = []
    for r in rows:
        data.append([
            r["id"],
            r["date"],
            r["type"][0].upper(),
            r["category"],
            fmt_money(to_signed(r["amount"], r["type"])),
            r["account"],
            (r["description"] or "")[:60],
        ])
    print_table(headers, data)
    total = sum(to_signed(r["amount"], r["type"]) for r in rows)
    print(f"\nCount: {len(rows)}   Total: {fmt_money(total)}")

def print_budgets(rows: list[sqlite3.Row]) -> None:
    headers = ["id", "month", "category", "amount"]
    data = [[r["id"], r["month"], r["category"], fmt_money(r["amount"])] for r in rows]
    print_table(headers, data)

def print_month_report(rep: dict[str, Any]) -> None:
    print(f"Report — {rep['month']}\n")
    print(f"Income:   {fmt_money(rep['income'])}")
    print(f"Expenses: {fmt_money(rep['expenses'])}")
    print(f"Net:      {fmt_money(rep['net'])}\n")

    if rep["by_category"]:
        print("By category:")
        cat_rows = []
        for cat, signed in rep["by_category"].items():
            cat_rows.append([cat, fmt_money(signed)])
        print_table(["Category", "Total"], cat_rows)
        print()
    if rep["variance"]:
        print("Budget vs Actual (expenses only):")
        var_rows = []
        for cat, b, actual, var in rep["variance"]:
            var_rows.append([cat, fmt_money(b), fmt_money(-actual), fmt_money(var)])
        print_table(["Category", "Budget", "Actual", "Variance"], var_rows)

#  CLI

def main(argv: Optional[Sequence[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="budget", description="Budgeting / Expense Tracker (SQLite)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create DB schema if missing")

    tx = sub.add_parser("tx", help="transaction operations")
    tx_sub = tx.add_subparsers(dest="tx_cmd", required=True)

    tx_add = tx_sub.add_parser("add", help="add a transaction")
    tx_add.add_argument("--date", default=dt.date.today().isoformat())
    tx_add.add_argument("--amount", type=float, required=True)
    tx_add.add_argument("--category", required=True)
    tx_add.add_argument("--type", choices=["expense", "income"], required=True)
    tx_add.add_argument("--desc", default="")
    tx_add.add_argument("--account", default="cash")

    tx_list = tx_sub.add_parser("list", help="list transactions")
    tx_list.add_argument("--from", dest="date_from")
    tx_list.add_argument("--to", dest="date_to")
    tx_list.add_argument("--category")
    tx_list.add_argument("--account")
    tx_list.add_argument("--type", choices=["expense", "income"])
    tx_list.add_argument("--search")

    tx_edit = tx_sub.add_parser("edit", help="edit a transaction")
    tx_edit.add_argument("id", type=int)
    tx_edit.add_argument("--date")
    tx_edit.add_argument("--amount", type=float)
    tx_edit.add_argument("--category")
    tx_edit.add_argument("--type", choices=["expense", "income"])
    tx_edit.add_argument("--desc")
    tx_edit.add_argument("--account")

    tx_del = tx_sub.add_parser("delete", help="delete a transaction")
    tx_del.add_argument("id", type=int)

    tx_exp = tx_sub.add_parser("export", help="export transactions to CSV")
    tx_exp.add_argument("--to-file", required=True)
    tx_exp.add_argument("--from", dest="date_from")
    tx_exp.add_argument("--to", dest="date_to")
    tx_exp.add_argument("--category")
    tx_exp.add_argument("--account")
    tx_exp.add_argument("--type", choices=["expense", "income"])
    tx_exp.add_argument("--search")

    tx_imp = tx_sub.add_parser("import", help="import transactions from CSV")
    tx_imp.add_argument("--from-file", required=True)
    tx_imp.add_argument("--strict", action="store_true")
    tx_imp.add_argument("--dry-run", action="store_true")

    bg = sub.add_parser("budget", help="budget operations")
    bg_sub = bg.add_subparsers(dest="bg_cmd", required=True)

    bg_set = bg_sub.add_parser("set", help="set a category budget for a month")
    bg_set.add_argument("--month", required=True)  # YYYY-MM
    bg_set.add_argument("--category", required=True)
    bg_set.add_argument("--amount", type=float, required=True)

    bg_list = bg_sub.add_parser("list", help="list budgets")
    bg_list.add_argument("--month")

    rpt = sub.add_parser("report", help="reports")
    rpt_sub = rpt.add_subparsers(dest="rpt_cmd", required=True)
    rpt_m = rpt_sub.add_parser("month", help="monthly report")
    rpt_m.add_argument("--month", required=True)

    rec = sub.add_parser("recurring", help="recurring rules")
    rec_sub = rec.add_subparsers(dest="rec_cmd", required=True)

    rec_add = rec_sub.add_parser("add", help="add a recurring rule")
    rec_add.add_argument("--date", required=True, help="first occurrence date YYYY-MM-DD")
    rec_add.add_argument("--amount", type=float, required=True)
    rec_add.add_argument("--category", required=True)
    rec_add.add_argument("--type", choices=["expense", "income"], required=True)
    rec_add.add_argument("--desc", default="")
    rec_add.add_argument("--account", default="cash")
    rec_add.add_argument("--freq", choices=["monthly", "weekly", "yearly"], required=True)
    rec_add.add_argument("--interval", type=int, default=1)

    rec_list = rec_sub.add_parser("list", help="list recurring rules")

    rec_en = rec_sub.add_parser("enable", help="enable a recurring rule")
    rec_en.add_argument("id", type=int)
    rec_dis = rec_sub.add_parser("disable", help="disable a recurring rule")
    rec_dis.add_argument("id", type=int)

    rec_apply = rec_sub.add_parser("apply", help="generate transactions for a month")
    rec_apply.add_argument("--month", required=True)

    args = ap.parse_args(argv)
    conn = connect()

    if args.cmd == "init-db":
        ensure_schema(conn)
        print(f"DB ready at {DB_PATH}")
        return

    ensure_schema(conn)

    if args.cmd == "tx":
        if args.tx_cmd == "add":
            d = parse_date(args.date)
            tx_id = add_transaction(
                conn,
                date=d,
                amount=args.amount,
                category=args.category,
                tx_type=args.type,
                description=args.desc,
                account=args.account,
            )
            print(f"Added transaction #{tx_id}")
        elif args.tx_cmd == "list":
            df = parse_date(args.date_from) if args.date_from else None
            dt_ = parse_date(args.date_to) if args.date_to else None
            rows = list_transactions(
                conn,
                date_from=df,
                date_to=dt_,
                category=args.category,
                account=args.account,
                tx_type=args.type,
                search=args.search,
            )
            print_transactions(rows)
        elif args.tx_cmd == "edit":
            updates = {}
            if args.date:
                updates["date"] = parse_date(args.date).isoformat()
            if args.amount is not None:
                if args.amount <= 0:
                    fail("amount must be > 0")
                updates["amount"] = args.amount
            if args.category:
                updates["category"] = args.category
            if args.type:
                updates["type"] = args.type
            if args.desc is not None:
                updates["description"] = args.desc
            if args.account:
                updates["account"] = args.account
            update_transaction(conn, args.id, **updates)
            print(f"Updated transaction #{args.id}")
        elif args.tx_cmd == "delete":
            delete_transaction(conn, args.id)
            print(f"Deleted transaction #{args.id}")
        elif args.tx_cmd == "export":
            df = parse_date(args.date_from) if args.date_from else None
            dt_ = parse_date(args.date_to) if args.date_to else None
            n = export_csv(
                conn,
                args.to_file,
                date_from=df,
                date_to=dt_,
                category=args.category,
                account=args.account,
                tx_type=args.type,
                search=args.search,
            )
            print(f"Exported {n} rows to {args.to_file}")
        elif args.tx_cmd == "import":
            added, skipped = import_csv(conn, args.from_file, strict=args.strict, dry_run=args.dry_run)
            flag = " (dry-run)" if args.dry_run else ""
            print(f"Imported{flag}: added={added}, skipped={skipped}")

    elif args.cmd == "budget":
        if args.bg_cmd == "set":
            # validate month
            parse_month(args.month)
            upsert_budget(conn, args.month, args.category, args.amount)
            print(f"Budget set: {args.month} • {args.category} • {fmt_money(args.amount)}")
        elif args.bg_cmd == "list":
            if args.month:
                parse_month(args.month)
            rows = list_budgets(conn, args.month)
            print_budgets(rows)

    elif args.cmd == "report":
        if args.rpt_cmd == "month":
            parse_month(args.month)
            rep = month_report(conn, args.month)
            print_month_report(rep)

    elif args.cmd == "recurring":
        if args.rec_cmd == "add":
            rid = add_recurring(
                conn,
                start_date=parse_date(args.date),
                amount=args.amount,
                category=args.category,
                tx_type=args.type,
                description=args.desc,
                account=args.account,
                frequency=args.freq,
                interval=args.interval,
            )
            print(f"Recurring rule #{rid} added")
        elif args.rec_cmd == "list":
            rows = list_recurring_rules(conn)
            headers = ["id", "active", "start_date", "freq", "interval", "type", "category", "amount", "account", "description"]
            data = [
                [r["id"], "Y" if r["active"] else "N", r["start_date"], r["frequency"], r["interval"], r["type"][0].upper(),
                 r["category"], fmt_money(r["amount"]), r["account"], (r["description"] or "")[:50]]
                for r in rows
            ]
            print_table(headers, data)
        elif args.rec_cmd == "enable":
            set_recurring_active(conn, args.id, True)
            print(f"Recurring rule #{args.id} enabled")
        elif args.rec_cmd == "disable":
            set_recurring_active(conn, args.id, False)
            print(f"Recurring rule #{args.id} disabled")
        elif args.rec_cmd == "apply":
            parse_month(args.month)
            created, skipped = apply_recurring(conn, args.month)
            print(f"Applied recurring for {args.month}: created={created}, skipped={skipped}")
    else:
        ap.print_help()

if __name__ == "__main__":
    main()
