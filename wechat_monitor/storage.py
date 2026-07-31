from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    id: int
    captured_at: str
    sent_at: str | None
    message_order: int | None
    conversation: str
    message_type: str
    direction: str
    sender: str
    text: str
    source: str
    fingerprint: str


class EventStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    sent_at TEXT,
                    message_order INTEGER,
                    conversation TEXT NOT NULL,
                    message_type TEXT NOT NULL DEFAULT 'text',
                    direction TEXT NOT NULL DEFAULT 'unknown',
                    sender TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE
                )
                """
            )
            self._migrate_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_monitor_events_conversation_time
                ON monitor_events(conversation, sent_at)
                """
            )

    @staticmethod
    def _migrate_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(monitor_events)"
            ).fetchall()
        }
        migrations = {
            "sent_at": "ALTER TABLE monitor_events ADD COLUMN sent_at TEXT",
            "message_order": (
                "ALTER TABLE monitor_events ADD COLUMN message_order INTEGER"
            ),
            "message_type": (
                "ALTER TABLE monitor_events ADD COLUMN message_type "
                "TEXT NOT NULL DEFAULT 'text'"
            ),
            "direction": (
                "ALTER TABLE monitor_events ADD COLUMN direction "
                "TEXT NOT NULL DEFAULT 'unknown'"
            ),
            "sender": (
                "ALTER TABLE monitor_events ADD COLUMN sender "
                "TEXT NOT NULL DEFAULT ''"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    def add(
        self,
        conversation: str,
        text: str,
        source: str,
        fingerprint: str,
        message_type: str = "text",
        direction: str = "unknown",
        sender: str = "",
        sent_at: str | None = None,
        message_order: int | None = None,
    ) -> MonitorEvent | None:
        captured_at = datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        )
        try:
            with self._session() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO monitor_events(
                        captured_at, sent_at, message_order, conversation, message_type,
                        direction, sender, text, source, fingerprint
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        captured_at,
                        sent_at,
                        message_order,
                        conversation,
                        message_type,
                        direction,
                        sender,
                        text,
                        source,
                        fingerprint,
                    ),
                )
                event_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None
        return MonitorEvent(
            id=event_id,
            captured_at=captured_at,
            sent_at=sent_at,
            message_order=message_order,
            conversation=conversation,
            message_type=message_type,
            direction=direction,
            sender=sender,
            text=text,
            source=source,
            fingerprint=fingerprint,
        )

    def recent(self, limit: int = 200) -> list[MonitorEvent]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT id, captured_at, sent_at, message_order, conversation, message_type,
                       direction, sender, text, source, fingerprint
                FROM monitor_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [MonitorEvent(**dict(row)) for row in rows]

    def clear(self) -> None:
        with self._session() as connection:
            connection.execute("DELETE FROM monitor_events")
