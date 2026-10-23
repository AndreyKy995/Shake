"""SQLite-хранилище: клиенты, история переписки, карточки лидов у владельца."""

import sqlite3
import time
from dataclasses import dataclass


@dataclass
class Client:
    channel: str
    user_id: str
    name: str
    username: str
    status: str  # dialog | hot | human | not_target | spam
    mode: str  # bot — отвечает ИИ, human — диалог взял владелец
    summary: str
    contact: str


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                channel TEXT NOT NULL,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'dialog',
                mode TEXT NOT NULL DEFAULT 'bot',
                summary TEXT NOT NULL DEFAULT '',
                contact TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY (channel, user_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,  -- user | assistant | owner
                text TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_by_client ON messages (channel, user_id, id);
            CREATE TABLE IF NOT EXISTS cards (
                owner_msg_id INTEGER PRIMARY KEY,
                channel TEXT NOT NULL,
                user_id TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def get_or_create(self, channel: str, user_id: str, name: str, username: str) -> Client:
        self.db.execute(
            """
            INSERT INTO clients (channel, user_id, name, username, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (channel, user_id) DO UPDATE SET name = excluded.name, username = excluded.username
            """,
            (channel, user_id, name, username, time.time()),
        )
        self.db.commit()
        return self.get(channel, user_id)

    def get(self, channel: str, user_id: str) -> Client | None:
        row = self.db.execute(
            "SELECT * FROM clients WHERE channel = ? AND user_id = ?", (channel, user_id)
        ).fetchone()
        if row is None:
            return None
        return Client(**{k: row[k] for k in Client.__dataclass_fields__})

    def update(self, channel: str, user_id: str, **fields) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE clients SET {sets} WHERE channel = ? AND user_id = ?",
            (*fields.values(), channel, user_id),
        )
        self.db.commit()

    def add_message(self, channel: str, user_id: str, role: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO messages (channel, user_id, role, text, ts) VALUES (?, ?, ?, ?, ?)",
            (channel, user_id, role, text, time.time()),
        )
        self.db.commit()

    def history(self, channel: str, user_id: str, limit: int = 40) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT role, text FROM messages WHERE channel = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (channel, user_id, limit),
        ).fetchall()
        return [(r["role"], r["text"]) for r in reversed(rows)]

    def save_card(self, owner_msg_id: int, channel: str, user_id: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cards (owner_msg_id, channel, user_id) VALUES (?, ?, ?)",
            (owner_msg_id, channel, user_id),
        )
        self.db.commit()

    def card_client(self, owner_msg_id: int) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT channel, user_id FROM cards WHERE owner_msg_id = ?", (owner_msg_id,)
        ).fetchone()
        return (row["channel"], row["user_id"]) if row else None

    def leads(self, statuses: tuple[str, ...], limit: int = 20) -> list[Client]:
        marks = ", ".join("?" for _ in statuses)
        rows = self.db.execute(
            f"SELECT * FROM clients WHERE status IN ({marks}) ORDER BY updated_at DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
        return [Client(**{k: r[k] for k in Client.__dataclass_fields__}) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) AS n FROM clients GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
