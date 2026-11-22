"""SQLite access. Plain SQL, no ORM. Everything that touches the database goes through here."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from bookly.data import seed

SCHEMA = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repo:
    def __init__(self, path: Path | str, today: date | None = None):
        self.path = str(path)
        self._today = today
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.lock = threading.RLock()

    # ---- lifecycle -------------------------------------------------------

    def today(self) -> date:
        return self._today or date.today()

    def is_initialized(self) -> bool:
        row = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customers'").fetchone()
        return row is not None

    def reset(self) -> None:
        """Drop everything, recreate the schema and load the seed (dates relative to today)."""
        with self.lock:
            tables = [r["name"] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            self.conn.execute("PRAGMA foreign_keys = OFF")
            for t in tables:
                self.conn.execute(f"DROP TABLE IF EXISTS {t}")
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript("BEGIN;\n" + SCHEMA.read_text() + "\nCOMMIT;")
            today = self.today()
            self.conn.execute("BEGIN")
            self.conn.executemany("INSERT INTO customers VALUES (?,?,?)", seed.CUSTOMERS)
            self.conn.executemany("INSERT INTO catalog VALUES (?,?,?,?,?,?,?,?)", seed.CATALOG)
            self.conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", seed.orders_for(today))
            self.conn.executemany("INSERT INTO items VALUES (?,?,?,?,?)", seed.ITEMS)
            self.conn.execute("COMMIT")

    def ensure(self) -> None:
        if not self.is_initialized():
            self.reset()

    # ---- reads -----------------------------------------------------------

    def _one(self, sql: str, *args: Any) -> dict | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args: Any) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def customer(self, customer_id: str) -> dict | None:
        return self._one("SELECT * FROM customers WHERE id = ?", customer_id)

    def find_customer_by_order_and_email(self, order_number: str, email: str) -> dict | None:
        return self._one(
            """SELECT c.* FROM customers c JOIN orders o ON o.customer_id = c.id
               WHERE o.order_number = ? AND c.email = ? COLLATE NOCASE""",
            order_number.strip().lstrip("#"), email.strip(),
        )

    def order(self, order_number: str) -> dict | None:
        return self._one("SELECT * FROM orders WHERE order_number = ?", order_number.strip().lstrip("#"))

    def orders_for_customer(self, customer_id: str) -> list[dict]:
        return self._all("SELECT * FROM orders WHERE customer_id = ? ORDER BY placed_at DESC", customer_id)

    def items_for_order(self, order_number: str) -> list[dict]:
        return self._all(
            """SELECT i.id AS item_id, i.order_number, i.price_cents, i.downloaded,
                      c.id AS catalog_id, c.title, c.author, c.genre, c.format, c.staff_pick
               FROM items i JOIN catalog c ON c.id = i.catalog_id
               WHERE i.order_number = ? ORDER BY i.id""",
            order_number,
        )

    def item(self, item_id: str) -> dict | None:
        return self._one(
            """SELECT i.id AS item_id, i.order_number, i.price_cents, i.downloaded,
                      c.id AS catalog_id, c.title, c.author, c.genre, c.format, c.staff_pick,
                      o.customer_id, o.delivered_at, o.status
               FROM items i JOIN catalog c ON c.id = i.catalog_id
               JOIN orders o ON o.order_number = i.order_number
               WHERE i.id = ?""",
            item_id,
        )

    def return_for_item(self, item_id: str) -> dict | None:
        return self._one("SELECT * FROM returns WHERE item_id = ?", item_id)

    def returns_in_last_days(self, customer_id: str, days: int) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM returns WHERE customer_id = ? AND created_at >= ?", (customer_id, since)
        ).fetchone()
        return int(row["n"])

    def returns(self) -> list[dict]:
        return self._all("SELECT * FROM returns ORDER BY id")

    def owned_catalog_ids(self, customer_id: str) -> set[str]:
        rows = self._all(
            """SELECT DISTINCT i.catalog_id FROM items i JOIN orders o ON o.order_number = i.order_number
               WHERE o.customer_id = ?""",
            customer_id,
        )
        return {r["catalog_id"] for r in rows}

    def staff_picks(self) -> list[dict]:
        return self._all("SELECT * FROM catalog WHERE staff_pick = 1 ORDER BY id")

    def tickets(self) -> list[dict]:
        rows = self._all("SELECT * FROM tickets ORDER BY id DESC")
        for r in rows:
            r["tried"] = json.loads(r.pop("tried_json"))
            r["policy_refs"] = json.loads(r.pop("policy_refs_json"))
        return rows

    def password_resets(self) -> list[dict]:
        return self._all("SELECT * FROM password_resets ORDER BY id")

    def events(self, session_id: str, turn: int | None = None) -> list[dict]:
        if turn is None:
            rows = self._all("SELECT * FROM events WHERE session_id = ? ORDER BY id", session_id)
        else:
            rows = self._all("SELECT * FROM events WHERE session_id = ? AND turn = ? ORDER BY id", session_id, turn)
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json"))
        return rows

    # ---- writes ----------------------------------------------------------

    def insert_return(self, *, item_id: str, customer_id: str, resolution: str, amount_cents: int,
                      reason: str, policy_ref: str, idempotency_key: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO returns (item_id, customer_id, resolution, amount_cents, reason, policy_ref,
                                        idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, customer_id, resolution, amount_cents, reason, policy_ref, idempotency_key, utcnow()),
            )
            return int(cur.lastrowid)

    def insert_password_reset(self, customer_id: str, sent_to: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO password_resets (customer_id, sent_to, created_at) VALUES (?,?,?)",
                (customer_id, sent_to, utcnow()),
            )
            return int(cur.lastrowid)

    def insert_ticket(self, *, session_id: str, queue: str, priority: str, customer: str, intent: str,
                      trigger: str, summary: str, tried: list[str], policy_refs: list[str], sentiment: str,
                      suggested_action: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO tickets (session_id, queue, priority, customer, intent, trigger, summary, tried_json,
                                        policy_refs_json, sentiment, suggested_action, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, queue, priority, customer, intent, trigger, summary, json.dumps(tried),
                 json.dumps(policy_refs), sentiment, suggested_action, utcnow()),
            )
            return int(cur.lastrowid)

    def insert_event(self, *, session_id: str, turn: int, stage: str, type: str, payload: dict,
                     model: str | None = None, latency_ms: int | None = None, input_tokens: int | None = None,
                     output_tokens: int | None = None, cost_usd: float | None = None) -> dict:
        created = utcnow()
        with self.lock:
            cur = self.conn.execute(
                """INSERT INTO events (session_id, turn, stage, type, payload_json, model, latency_ms,
                                       input_tokens, output_tokens, cost_usd, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, turn, stage, type, json.dumps(payload, default=str), model, latency_ms,
                 input_tokens, output_tokens, cost_usd, created),
            )
        return {"id": cur.lastrowid, "session_id": session_id, "turn": turn, "stage": stage, "type": type,
                "payload": payload, "model": model, "latency_ms": latency_ms, "input_tokens": input_tokens,
                "output_tokens": output_tokens, "cost_usd": cost_usd, "created_at": created}
