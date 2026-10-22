
"""CourseVault SQLite database layer.

Uses a local SQLite file instead of PostgreSQL.
No external database server or database credentials required.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = "coursevault.db"


def _database_path() -> str:
    """Resolve the local database path."""
    path = os.getenv("SQLITE_DB_PATH", DEFAULT_DB_PATH).strip()
    if not path:
        raise ValueError("SQLITE_DB_PATH cannot be empty.")

    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(
            parents=True, exist_ok=True
        )
        return str(Path(path).expanduser())

    return path


class SQLiteDatabase:
    """Small async-compatible SQLite database wrapper."""

    def __init__(self, path: str) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=15,
            isolation_level="DEFERRED",
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")

        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")

        return connection

    async def run(self, function, *args):
        """Run SQLite work off the event loop."""
        return await asyncio.to_thread(function, *args)

    async def close(self) -> None:
        """No persistent connection is held open."""
        return None


async def create_pool(database_url: str | None = None) -> SQLiteDatabase:
    """Initialize SQLite and create/update the schema.

    The optional argument exists for compatibility with the previous
    PostgreSQL startup call. SQLite does not use a database URL.
    """
    del database_url

    path = _database_path()
    database = SQLiteDatabase(path)
    await database.run(_initialize, path)
    return database


def _initialize(path: str) -> None:
    database = SQLiteDatabase(path)
    connection = database._connect()

    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                recommendations_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                price_paise INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                impact TEXT NOT NULL DEFAULT '',
                prerequisites TEXT NOT NULL DEFAULT '',
                telegram_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'published', 'archived')),
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wishlists (
                telegram_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, course_id),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (course_id) REFERENCES courses(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                amount_paise INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                provider TEXT,
                provider_payment_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                FOREIGN KEY (course_id) REFERENCES courses(id)
            );

            CREATE TABLE IF NOT EXISTS entitlements (
                telegram_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                order_id INTEGER,
                granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, course_id),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                FOREIGN KEY (course_id) REFERENCES courses(id),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_telegram_id INTEGER,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_courses_status_category
                ON courses(status, category);

            CREATE INDEX IF NOT EXISTS idx_orders_user
                ON orders(telegram_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_audit_logs_created
                ON audit_logs(created_at);
            """
        )

        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (version)
            VALUES (1)
            """
        )
        connection.commit()
    finally:
        connection.close()


def _fetchall(path: str, query: str, parameters: tuple = ()) -> list[dict]:
    connection = SQLiteDatabase(path)._connect()
    try:
        rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _fetchone(path: str, query: str, parameters: tuple = ()) -> dict | None:
    connection = SQLiteDatabase(path)._connect()
    try:
        row = connection.execute(query, parameters).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


async def upsert_user(
    database: SQLiteDatabase,
    telegram_id: int,
    username: str | None,
    first_name: str,
) -> None:
    def operation() -> None:
        connection = database._connect()
        try:
            connection.execute(
                """
                INSERT INTO users (telegram_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (telegram_id, username, first_name or ""),
            )
            connection.commit()
        finally:
            connection.close()

    await database.run(operation)


async def list_categories(database: SQLiteDatabase) -> list[str]:
    return await database.run(
        lambda: [
            row["category"]
            for row in _fetchall(
                database.path,
                """
                SELECT DISTINCT category FROM courses
                WHERE status = 'published'
                ORDER BY category COLLATE NOCASE
                """,
            )
        ]
    )


async def list_courses(
    database: SQLiteDatabase,
    category: str | None = None,
) -> list[dict]:
    if category:
        return await database.run(
            _fetchall,
            database.path,
            """
            SELECT * FROM courses
            WHERE status = 'published' AND category = ?
            ORDER BY created_at DESC, id DESC
            """,
            (category,),
        )

    return await database.run(
        _fetchall,
        database.path,
        """
        SELECT * FROM courses
        WHERE status = 'published'
        ORDER BY created_at DESC, id DESC
        """,
    )


async def get_course(
    database: SQLiteDatabase,
    course_id: int,
) -> dict | None:
    return await database.run(
        _fetchone,
        database.path,
        """
        SELECT * FROM courses
        WHERE id = ? AND status = 'published'
        """,
        (course_id,),
    )


async def add_to_wishlist(
    database: SQLiteDatabase,
    telegram_id: int,
    course_id: int,
) -> bool:
    def operation() -> bool:
        connection = database._connect()
        try:
            course = connection.execute(
                "SELECT id FROM courses WHERE id = ? AND status = 'published'",
                (course_id,),
            ).fetchone()
            user = connection.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

            if not course or not user:
                return False

            connection.execute(
                """
                INSERT OR IGNORE INTO wishlists (telegram_id, course_id)
                VALUES (?, ?)
                """,
                (telegram_id, course_id),
            )
            connection.commit()
            return True
        finally:
            connection.close()

    return await database.run(operation)


async def list_wishlist(
    database: SQLiteDatabase,
    telegram_id: int,
) -> list[dict]:
    return await database.run(
        _fetchall,
        database.path,
        """
        SELECT c.* FROM courses c
        INNER JOIN wishlists w ON w.course_id = c.id
        WHERE w.telegram_id = ? AND c.status = 'published'
        ORDER BY w.created_at DESC
        """,
        (telegram_id,),
    )


async def list_course_drafts(database: SQLiteDatabase) -> list[dict]:
    return await database.run(
        _fetchall,
        database.path,
        """
        SELECT * FROM courses
        WHERE status = 'draft'
        ORDER BY created_at DESC, id DESC
        """,
    )


async def write_audit_log(
    database: SQLiteDatabase,
    actor_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if not action or len(action) > 100:
        raise ValueError("Audit action must be 1–100 characters.")

    serialized_details = json.dumps(
        details or {},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    def operation() -> None:
        connection = database._connect()
        try:
            connection.execute(
                """
                INSERT INTO audit_logs (
                    actor_telegram_id, action, target_type,
                    target_id, details
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    action,
                    target_type,
                    str(target_id) if target_id is not None else None,
                    serialized_details,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    await database.run(operation)


async def publish_course(
    database: SQLiteDatabase,
    course_id: int,
    owner_id: int,
) -> bool:
    """Publish a draft and record the action in one transaction."""

    def operation() -> bool:
        connection = database._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            course = connection.execute(
                "SELECT title FROM courses WHERE id = ? AND status = 'draft'",
                (course_id,),
            ).fetchone()

            if not course:
                connection.rollback()
                return False

            connection.execute(
                """
                UPDATE courses
                SET status = 'published', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'draft'
                """,
                (course_id,),
            )
            connection.execute(
                """
                INSERT INTO audit_logs (
                    actor_telegram_id, action, target_type,
                    target_id, details
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    "course_published",
                    "course",
                    str(course_id),
                    json.dumps({"title": course["title"]}),
                ),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    return await database.run(operation)


async def close_pool(database: SQLiteDatabase | None) -> None:
    """Compatibility wrapper for the previous PostgreSQL interface."""
    if database is not None:
        await database.close()
