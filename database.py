"""CourseVault PostgreSQL access, migrations, and data operations.

Requirements:
    asyncpg>=0.30,<1.0

Database:
    PostgreSQL

Security notes:
    - All values are passed as SQL parameters.
    - Schema migrations are versioned and serialized with a PostgreSQL lock.
    - Course publication requires a previously validated ZIP record.
    - Payment verification and entitlement granting are deliberately
      not implemented in this module.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg


logger = logging.getLogger("coursevault.database")

# A fixed application-specific key used only to serialize CourseVault's
# schema migrations. Do not reuse this key for unrelated applications.
_MIGRATION_LOCK_KEY = 0x43564D47

_MIN_POOL_SIZE = 1
_MAX_POOL_SIZE = 5
_COMMAND_TIMEOUT_SECONDS = 30
_CONNECT_TIMEOUT_SECONDS = 15
_MAX_AUDIT_DETAILS_BYTES = 8_192


# Each migration is applied once, in version order. Existing tables from the
# earlier CourseVault starter schema are retained and upgraded by migration 2.
_MIGRATIONS: tuple[tuple[int, str], ...] = (
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
            telegram_id BIGINT NOT NULL
                REFERENCES users(telegram_id),
            course_id BIGINT NOT NULL
                REFERENCES courses(id),
            amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),
            currency TEXT NOT NULL DEFAULT 'INR'
                CHECK (currency ~ '^[A-Z]{3}$'),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (
                    status IN (
                        'pending', 'paid', 'failed',
                        'refunded', 'disputed'
                    )
                ),
            provider_reference TEXT UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            paid_at TIMESTAMPTZ,
            UNIQUE (telegram_id, course_id, id)
        );

        CREATE TABLE IF NOT EXISTS entitlements (
            telegram_id BIGINT NOT NULL
                REFERENCES users(telegram_id),
            course_id BIGINT NOT NULL
                REFERENCES courses(id),
            order_id UUID NOT NULL UNIQUE
                REFERENCES orders(id),
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
            ADD COLUMN IF NOT EXISTS zip_size_bytes BIGINT;

        ALTER TABLE courses
            ADD COLUMN IF NOT EXISTS zip_sha256 TEXT;

        ALTER TABLE courses
            ADD COLUMN IF NOT EXISTS zip_validation_status TEXT
            NOT NULL DEFAULT 'unverified';

        ALTER TABLE courses
            ADD COLUMN IF NOT EXISTS zip_validated_at TIMESTAMPTZ;

        ALTER TABLE courses
            ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

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

        DO $migration$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'courses_zip_validation_status_check'
                  AND conrelid = 'courses'::regclass
            ) THEN
                ALTER TABLE courses
                    ADD CONSTRAINT
                    courses_zip_validation_status_check
                    CHECK (
                        zip_validation_status IN (
                            'unverified', 'passed', 'rejected'
                        )
                    ) NOT VALID;
            END IF;
        END
        $migration$;

        ALTER TABLE courses
            VALIDATE CONSTRAINT courses_zip_validation_status_check;

        DO $migration$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'courses_zip_size_bytes_check'
                  AND conrelid = 'courses'::regclass
            ) THEN
                ALTER TABLE courses
                    ADD CONSTRAINT courses_zip_size_bytes_check
                    CHECK (
                        zip_size_bytes IS NULL OR zip_size_bytes >= 0
                    ) NOT VALID;
            END IF;
        END
        $migration$;

        ALTER TABLE courses
            VALIDATE CONSTRAINT courses_zip_size_bytes_check;

        DO $migration$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'courses_zip_sha256_check'
                  AND conrelid = 'courses'::regclass
            ) THEN
                ALTER TABLE courses
                    ADD CONSTRAINT courses_zip_sha256_check
                    CHECK (
                        zip_sha256 IS NULL
                        OR zip_sha256 ~ '^[a-fA-F0-9]{64}$'
                    ) NOT VALID;
            END IF;
        END
        $migration$;

        ALTER TABLE courses
            VALIDATE CONSTRAINT courses_zip_sha256_check;
        """,
    ),
)


async def _run_migrations(pool: asyncpg.Pool) -> None:
    """Apply unapplied migrations safely across concurrent app startups."""
    async with pool.acquire() as connection:
        lock_acquired = False

        try:
            # Session-level lock is held on this connection through all
            # migrations and released in the finally block.
            await connection.execute(
                "SELECT pg_advisory_lock($1::BIGINT)",
                _MIGRATION_LOCK_KEY,
            )
            lock_acquired = True

            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            applied_rows = await connection.fetch(
                "SELECT version FROM schema_migrations"
            )
            applied_versions = {
                int(row["version"]) for row in applied_rows
            }

            known_versions = {version for version, _ in _MIGRATIONS}
            unknown_versions = applied_versions - known_versions

            if unknown_versions:
                raise RuntimeError(
                    "Database contains migration versions unknown to this "
                    f"application: {sorted(unknown_versions)}. "
                    "Stop deployment and reconcile the migration history."
                )

            for version, sql in _MIGRATIONS:
                if version in applied_versions:
                    continue

                logger.info("Applying database migration %s", version)

                async with connection.transaction():
                    await connection.execute(sql)
                    await connection.execute(
                        """
                        INSERT INTO schema_migrations (version)
                        VALUES ($1)
                        ON CONFLICT (version) DO NOTHING
                        """,
                        version,
                    )

                logger.info("Database migration %s applied", version)

        finally:
            if lock_acquired:
                try:
                    await connection.execute(
                        "SELECT pg_advisory_unlock($1::BIGINT)",
                        _MIGRATION_LOCK_KEY,
                    )
                except asyncpg.PostgresError:
                    # PostgreSQL releases session locks if the connection
                    # closes. Log without exposing connection credentials.
                    logger.exception(
                        "Could not explicitly release migration lock"
                    )


async def create_pool(database_url: str) -> asyncpg.Pool:
    """Create the connection pool and bring the schema up to date.

    Raises:
        ValueError: when the database URL is missing.
        asyncpg.PostgresError: when PostgreSQL operations fail.
        RuntimeError: when migration history is inconsistent.
    """
    if not database_url or not database_url.strip():
        raise ValueError("DATABASE_URL must be configured")

    pool = await asyncpg.create_pool(
        dsn=database_url.strip(),
        min_size=_MIN_POOL_SIZE,
        max_size=_MAX_POOL_SIZE,
        command_timeout=_COMMAND_TIMEOUT_SECONDS,
        timeout=_CONNECT_TIMEOUT_SECONDS,
        server_settings={
            "application_name": "coursevault",
        },
    )

    try:
        await _run_migrations(pool)
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")
        return pool
    except Exception:
        await pool.close()
        raise


async def close_pool(pool: asyncpg.Pool) -> None:
    """Close the PostgreSQL connection pool cleanly."""
    await pool.close()


async def upsert_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    first_name: str,
) -> None:
    """Create a user or update their current Telegram profile."""
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


async def set_recommendations_enabled(
    pool: asyncpg.Pool,
    telegram_id: int,
    enabled: bool,
) -> bool:
    """Update a user's recommendations preference."""
    result = await pool.execute(
        """
        UPDATE users
        SET recommendations_enabled = $2
        WHERE telegram_id = $1
        """,
        telegram_id,
        enabled,
    )
    return result.endswith(" 1")


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
    """Return a bounded list of published courses."""
    if category is None:
        return await pool.fetch(
            """
            SELECT id, title, category, price_paise
            FROM courses
            WHERE status = 'published'
            ORDER BY published_at DESC NULLS LAST, id DESC
            LIMIT 50
            """
        )

    return await pool.fetch(
        """
        SELECT id, title, category, price_paise
        FROM courses
        WHERE status = 'published'
          AND category = $1
        ORDER BY published_at DESC NULLS LAST, id DESC
        LIMIT 50
        """,
        category,
    )


async def get_course(
    pool: asyncpg.Pool,
    course_id: int,
) -> asyncpg.Record | None:
    """Return a published course only."""
    return await pool.fetchrow(
        """
        SELECT
            id,
            title,
            category,
            price_paise,
            description,
            impact,
            prerequisites,
            zip_file_id,
            zip_size_bytes,
            zip_sha256,
            zip_validation_status,
            published_at
        FROM courses
        WHERE id = $1
          AND status = 'published'
        """,
        course_id,
    )


async def add_to_wishlist(
    pool: asyncpg.Pool,
    telegram_id: int,
    course_id: int,
) -> bool:
    """Save a published course for a registered student.

    Returns True when a new item was saved and False when it was not
    inserted, including if it was already on the wishlist or unavailable.
    """
    row = await pool.fetchrow(
        """
        INSERT INTO wishlists (telegram_id, course_id)
        SELECT $1, c.id
        FROM courses AS c
        JOIN users AS u ON u.telegram_id = $1
        WHERE c.id = $2
          AND c.status = 'published'
        ON CONFLICT (telegram_id, course_id) DO NOTHING
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
        FROM wishlists AS w
        JOIN courses AS c ON c.id = w.course_id
        WHERE w.telegram_id = $1
          AND c.status = 'published'
        ORDER BY w.created_at DESC
        LIMIT 100
        """,
        telegram_id,
    )


async def write_audit_log(
    pool: asyncpg.Pool,
    actor_telegram_id: int,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit event.

    Do not put passwords, tokens, payment secrets, full payment payloads,
    or other sensitive data in details.
    """
    if not action or len(action) > 100:
        raise ValueError("Audit action must be 1-100 characters")

    if not entity_type or len(entity_type) > 100:
        raise ValueError("Audit entity_type must be 1-100 characters")

    if entity_id is not None and len(entity_id) > 200:
        raise ValueError("Audit entity_id must be at most 200 characters")

    payload = json.dumps(
        details or {},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )

    if len(payload.encode("utf-8")) > _MAX_AUDIT_DETAILS_BYTES:
        raise ValueError("Audit details exceed the maximum permitted size")

    await pool.execute(
        """
        INSERT INTO audit_logs (
            actor_telegram_id,
            action,
            entity_type,
            entity_id,
            details
        )
        VALUES ($1, $2, $3, $4, $5::JSONB)
        """,
        actor_telegram_id,
        action,
        entity_type,
        entity_id,
        payload,
    )


async def list_course_drafts(
    pool: asyncpg.Pool,
    limit: int = 50,
) -> list[asyncpg.Record]:
    """List drafts for owner-facing review; call only after owner auth."""
    safe_limit = max(1, min(int(limit), 100))

    return await pool.fetch(
        """
        SELECT
            id,
            title,
            category,
            price_paise,
            zip_size_bytes,
            zip_sha256,
            zip_validation_status,
            created_at,
            updated_at
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
    """Publish only a draft whose ZIP has passed the validation workflow.

    This function must only be called after the Telegram owner ID is
    authenticated and the owner has explicitly confirmed publication.

    Returns:
        "published" if successful
        "not_found" if no draft exists
        "zip_not_validated" if the archive is not verified

    The actual ZIP inspection workflow must set validation fields only after
    inspecting the archive safely. Never mark a ZIP as passed merely because
    a filename ends in .zip.
    """
    async with pool.acquire() as connection:
        async with connection.transaction():
            course = await connection.fetchrow(
                """
                SELECT
                    id,
                    title,
                    zip_validation_status,
                    zip_size_bytes,
                    zip_sha256
                FROM courses
                WHERE id = $1
                  AND status = 'draft'
                FOR UPDATE
                """,
                course_id,
            )

            if course is None:
                return "not_found"

            zip_is_validated = (
                course["zip_validation_status"] == "passed"
                and course["zip_size_bytes"] is not None
                and course["zip_sha256"] is not None
            )

            if not zip_is_validated:
                await connection.execute(
                    """
                    INSERT INTO audit_logs (
                        actor_telegram_id,
                        action,
                        entity_type,
                        entity_id,
                        details
                    )
                    VALUES ($1, $2, $3, $4, $5::JSONB)
                    """,
                    actor_telegram_id,
                    "course_publish_blocked",
                    "course",
                    str(course_id),
                    json.dumps(
                        {"reason": "zip_not_validated"},
                        separators=(",", ":"),
                    ),
                )
                return "zip_not_validated"

            await connection.execute(
                """
                UPDATE courses
                SET status = 'published',
                    published_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'draft'
                """,
                course_id,
            )

            await connection.execute(
                """
                INSERT INTO audit_logs (
                    actor_telegram_id,
                    action,
                    entity_type,
                    entity_id,
                    details
                )
                VALUES ($1, $2, $3, $4, $5::JSONB)
                """,
                actor_telegram_id,
                "course_published",
                "course",
                str(course_id),
                json.dumps(
                    {
                        "zip_validation_status": "passed",
                        "zip_size_bytes": course["zip_size_bytes"],
                        "zip_sha256": course["zip_sha256"],
                    },
                    separators=(",", ":"),
                ),
            )

    return "published"
