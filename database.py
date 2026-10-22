
"""CourseVault database layer.

PostgreSQL access, versioned migrations, course management,
wishlists, and owner audit logging.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger("coursevault.database")

_MIGRATION_LOCK_KEY = 0x43564D47
MAX_AUDIT_DETAILS_BYTES = 8192

MIGRATIONS = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            recommendations_enabled BOOLEAN NOT NULL DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS courses (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            price_paise BIGINT NOT NULL CHECK (price_paise >= 0),
            description TEXT NOT NULL,
            impact TEXT NOT NULL DEFAULT '',
            prerequisites TEXT NOT NULL DEFAULT '',
            zip_file_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'published', 'archived')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS wishlists (
            telegram_id BIGINT NOT NULL
                REFERENCES users(telegram_id) ON DELETE CASCADE,
            course_id BIGINT NOT NULL
                REFERENCES courses(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (telegram_id, course_id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id UUID PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
            course_id BIGINT NOT NULL REFERENCES courses(id),
            amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),
            currency TEXT NOT NULL DEFAULT 'INR',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending', 'paid', 'failed', 'refunded', 'disputed'
                )),
            provider_reference TEXT UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            paid_at TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS entitlements (
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
            course_id BIGINT NOT NULL REFERENCES courses(id),
            order_id UUID NOT NULL UNIQUE REFERENCES orders(id),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (telegram_id, course_id)
        );

        CREATE INDEX IF NOT EXISTS courses_status_category_idx
            ON courses(status, category);

        CREATE INDEX IF NOT EXISTS orders_status_created_idx
            ON orders(status, created_at DESC);

        CREATE INDEX IF NOT EXISTS orders_user_created_idx
            ON orders(telegram_id, created_at DESC);
        """,
    ),
    (
        2,
        """
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS recommendations_enabled
            BOOLEAN NOT NULL DEFAULT TRUE;

        ALTER TABLE courses
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
            NOT NULL DEFAULT NOW();

        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGSERIAL PRIMARY KEY,
            actor_telegram_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS audit_logs_created_at_idx
            ON audit_logs(created_at DESC);

        CREATE INDEX IF NOT EXISTS audit_logs_actor_created_idx
            ON audit_logs(actor_telegram_id, created_at DESC);
        """,
    ),
)


async def _run_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        locked = False
        try:
            await conn.execute(
                "SELECT pg_advisory_lock($1::BIGINT)",
                _MIGRATION_LOCK_KEY,
            )
            locked = True

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            rows = await conn.fetch(
                "SELECT version FROM schema_migrations"
            )
            applied = {int(row["version"]) for row in rows}
            known = {version for version, _ in MIGRATIONS}

            if applied - known:
                raise RuntimeError(
                    "Unknown database migration versions detected. "
                    "Reconcile the migration history before starting."
                )

            for version, sql in MIGRATIONS:
                if version in applied:
                    continue

                logger.info("Applying migration %s", version)
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        """
                        INSERT INTO schema_migrations(version)
                        VALUES ($1)
                        """,
                        version,
                    )
                logger.info("Migration %s completed", version)

        finally:
            if locked:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1::BIGINT)",
                        _MIGRATION_LOCK_KEY,
                    )
                except asyncpg.PostgresError:
                    logger.exception("Failed to release migration lock")


async def create_pool(database_url: str) -> asyncpg.Pool:
    if not database_url or not database_url.strip():
        raise ValueError("DATABASE_URL must be configured")

    pool = await asyncpg.create_pool(
        dsn=database_url.strip(),
        min_size=1,
        max_size=5,
        command_timeout=30,
        timeout=15,
        server_settings={"application_name": "coursevault"},
    )

    try:
        await _run_migrations(pool)
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return pool
    except Exception:
        await pool.close()
        raise


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()


async def upsert_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    first_name: str,
) -> None:
    await pool.execute(
        """
        INSERT INTO users (telegram_id, username, first_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (telegram_id) DO UPDATE
        SET username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
        """,
        telegram_id,
        username,
        (first_name or "")[:256],
    )


async def list_categories(pool: asyncpg.Pool) -> list[str]:
    rows = await pool.fetch(
        """
        SELECT DISTINCT category
        FROM courses
        WHERE status = 'published'
        ORDER BY category
        """
    )
    return [row["category"] for row in rows]


async def list_courses(
    pool: asyncpg.Pool,
    category: str | None = None,
) -> list[asyncpg.Record]:
    if category is None:
        return await pool.fetch(
            """
            SELECT id, title, category, price_paise
            FROM courses
            WHERE status = 'published'
            ORDER BY created_at DESC
            LIMIT 50
            """
        )

    return await pool.fetch(
        """
        SELECT id, title, category, price_paise
        FROM courses
        WHERE status = 'published' AND category = $1
        ORDER BY created_at DESC
        LIMIT 50
        """,
        category,
    )


async def get_course(
    pool: asyncpg.Pool,
    course_id: int,
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT id, title, category, price_paise, description,
               impact, prerequisites, zip_file_id, status
        FROM courses
        WHERE id = $1 AND status = 'published'
        """,
        course_id,
    )


async def add_to_wishlist(
    pool: asyncpg.Pool,
    telegram_id: int,
    course_id: int,
) -> bool:
    row = await pool.fetchrow(
        """
        INSERT INTO wishlists (telegram_id, course_id)
        SELECT $1, c.id
        FROM courses c
        JOIN users u ON u.telegram_id = $1
        WHERE c.id = $2 AND c.status = 'published'
        ON CONFLICT DO NOTHING
        RETURNING course_id
        """,
        telegram_id,
        course_id,
    )
    return row is not None


async def list_wishlist(
    pool: asyncpg.Pool,
    telegram_id: int,
) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT c.id, c.title, c.category, c.price_paise
        FROM wishlists w
        JOIN courses c ON c.id = w.course_id
        WHERE w.telegram_id = $1 AND c.status = 'published'
        ORDER BY w.created_at DESC
        LIMIT 100
        """,
        telegram_id,
    )


async def list_course_drafts(
    pool: asyncpg.Pool,
    limit: int = 50,
) -> list[asyncpg.Record]:
    safe_limit = max(1, min(int(limit), 100))
    return await pool.fetch(
        """
        SELECT id, title, category, price_paise, zip_file_id, created_at
        FROM courses
        WHERE status = 'draft'
        ORDER BY created_at DESC
        LIMIT $1
        """,
        safe_limit,
    )


async def publish_course(
    pool: asyncpg.Pool,
    actor_telegram_id: int,
    course_id: int,
) -> str:
    """Publish a draft after owner authorization and explicit confirmation."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            course = await conn.fetchrow(
                """
                SELECT id, title
                FROM courses
                WHERE id = $1 AND status = 'draft'
                FOR UPDATE
                """,
                course_id,
            )

            if course is None:
                return "not_found"

            await conn.execute(
                """
                UPDATE courses
                SET status = 'published', updated_at = NOW()
                WHERE id = $1 AND status = 'draft'
                """,
                course_id,
            )

            await conn.execute(
                """
                INSERT INTO audit_logs (
                    actor_telegram_id, action, entity_type,
                    entity_id, details
                )
                VALUES ($1, 'course_published', 'course', $2, $3::jsonb)
                """,
                actor_telegram_id,
                str(course_id),
                json.dumps({"title": course["title"]}),
            )

    return "published"


async def write_audit_log(
    pool: asyncpg.Pool,
    actor_telegram_id: int,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if not action or len(action) > 100:
        raise ValueError("action must be 1-100 characters")
    if not entity_type or len(entity_type) > 100:
        raise ValueError("entity_type must be 1-100 characters")
    if entity_id is not None and len(entity_id) > 200:
        raise ValueError("entity_id must be at most 200 characters")

    payload = json.dumps(
        details or {},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > MAX_AUDIT_DETAILS_BYTES:
        raise ValueError("Audit details exceed the maximum allowed size")

    await pool.execute(
        """
        INSERT INTO audit_logs (
            actor_telegram_id, action, entity_type, entity_id, details
        )
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """,
        actor_telegram_id,
        action,
        entity_type,
        entity_id,
        payload,
    )
```

### Important before restarting BotKeep

This version removes the ZIP-validation requirement, but **the existing `main.py` must also be updated** so it no longer imports `zip_security` or expects validation columns. We'll do that next.

Also, if you've already run the earlier migration that added ZIP-validation columns, PostgreSQL will keep those columns; this replacement doesn't delete them or erase course records.

Once you've committed this file, tell me **“Done”** and we'll update `main.py` to match it.
