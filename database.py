import asyncpg


SCHEMA = """
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

CREATE INDEX IF NOT EXISTS courses_status_category_idx
    ON courses(status, category);

CREATE TABLE IF NOT EXISTS wishlists (
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id)
        ON DELETE CASCADE,
    course_id BIGINT NOT NULL REFERENCES courses(id)
        ON DELETE CASCADE,
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
    paid_at TIMESTAMPTZ,
    UNIQUE (telegram_id, course_id, id)
);

CREATE TABLE IF NOT EXISTS entitlements (
    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    course_id BIGINT NOT NULL REFERENCES courses(id),
    order_id UUID NOT NULL UNIQUE REFERENCES orders(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (telegram_id, course_id)
);
"""


async def create_pool(database_url: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )

    async with pool.acquire() as connection:
        await connection.execute(SCHEMA)

    return pool


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
        ON CONFLICT (telegram_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
        """,
        telegram_id,
        username,
        first_name,
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
        SELECT *
        FROM courses
        WHERE id = $1 AND status = 'published'
        """,
        course_id,
    )


async def add_to_wishlist(
    pool: asyncpg.Pool,
    telegram_id: int,
    course_id: int,
) -> None:
    await pool.execute(
        """
        INSERT INTO wishlists (telegram_id, course_id)
        SELECT $1, $2
        WHERE EXISTS (
            SELECT 1 FROM courses
            WHERE id = $2 AND status = 'published'
        )
        ON CONFLICT DO NOTHING
        """,
        telegram_id,
        course_id,
    )


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
        """,
        telegram_id,
    )
