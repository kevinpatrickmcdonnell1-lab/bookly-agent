-- Bookly demo schema. The database is the last backstop: even if every layer above
-- it failed, these constraints still block double refunds and refunds on
-- downloaded ebooks.

PRAGMA foreign_keys = ON;

CREATE TABLE customers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE catalog (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    author      TEXT NOT NULL,
    genre       TEXT NOT NULL,
    format      TEXT NOT NULL CHECK (format IN ('paperback', 'hardcover', 'ebook', 'box_set')),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    staff_pick  INTEGER NOT NULL DEFAULT 0 CHECK (staff_pick IN (0, 1)),
    blurb       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE orders (
    order_number    TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    placed_at       TEXT NOT NULL,           -- ISO date
    status          TEXT NOT NULL CHECK (status IN ('processing', 'shipped', 'in_transit', 'delivered')),
    delivered_at    TEXT,                    -- ISO date, NULL until delivered
    eta             TEXT,                    -- ISO date, mocked carrier estimate
    carrier         TEXT,
    tracking_number TEXT,
    gift_note       TEXT                     -- customer-written; never trusted as instructions
);

CREATE TABLE items (
    id            TEXT PRIMARY KEY,
    order_number  TEXT NOT NULL REFERENCES orders(order_number),
    catalog_id    TEXT NOT NULL REFERENCES catalog(id),
    price_cents   INTEGER NOT NULL CHECK (price_cents > 0),
    downloaded    INTEGER NOT NULL DEFAULT 0 CHECK (downloaded IN (0, 1))
);

CREATE TABLE returns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL UNIQUE REFERENCES items(id),   -- one return per item
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    resolution      TEXT NOT NULL CHECK (resolution IN ('refund', 'store_credit')),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    reason          TEXT NOT NULL,
    policy_ref      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL
);

-- Backstop for rule 2: a downloaded ebook can't be returned unless it was
-- damaged or the wrong item.
CREATE TRIGGER no_return_on_downloaded_ebook
BEFORE INSERT ON returns
WHEN NEW.reason NOT IN ('damaged', 'wrong_item')
 AND EXISTS (
    SELECT 1 FROM items i JOIN catalog c ON c.id = i.catalog_id
    WHERE i.id = NEW.item_id AND c.format = 'ebook' AND i.downloaded = 1
 )
BEGIN
    SELECT RAISE(ABORT, 'downloaded ebooks cannot be returned');
END;

CREATE TABLE password_resets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    sent_to     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE tickets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    queue            TEXT NOT NULL CHECK (queue IN ('booksellers', 'order_ops', 'trust_safety')),
    priority         TEXT NOT NULL CHECK (priority IN ('normal', 'high')),
    customer         TEXT NOT NULL,
    intent           TEXT NOT NULL,
    trigger          TEXT NOT NULL,
    summary          TEXT NOT NULL,
    tried_json       TEXT NOT NULL,
    policy_refs_json TEXT NOT NULL,
    sentiment        TEXT NOT NULL,
    suggested_action TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',
    created_at       TEXT NOT NULL
);

CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    turn          INTEGER NOT NULL,
    stage         TEXT NOT NULL,
    type          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    model         TEXT,
    latency_ms    INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    created_at    TEXT NOT NULL
);

CREATE INDEX events_by_session ON events(session_id, turn);
