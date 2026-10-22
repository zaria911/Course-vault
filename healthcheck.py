
"""CourseVault deployment health check.

Run with: python healthcheck.py
Exit code 0 means all checks passed; non-zero means a check failed.
"""

import asyncio
import os
import sys

import asyncpg


def check_environment() -> bool:
    """Validate required environment variables without printing secrets."""
    required = ("BOT_TOKEN", "OWNER_TELEGRAM_ID", "DATABASE_URL")
    missing = [name for name in required if not os.getenv(name, "").strip()]

    if missing:
        print(f"FAIL: Missing environment variables: {', '.join(missing)}")
        return False

    try:
        owner_id = int(os.environ["OWNER_TELEGRAM_ID"])
        if owner_id <= 0:
            raise ValueError
    except ValueError:
        print("FAIL: OWNER_TELEGRAM_ID must be a positive integer.")
        return False

    token = os.environ["BOT_TOKEN"].strip()
    if ":" not in token:
        print("FAIL: BOT_TOKEN format looks invalid.")
        return False

    database_url = os.environ["DATABASE_URL"].strip()
    if not database_url.startswith(("postgresql://", "postgres://")):
        print("FAIL: DATABASE_URL must use a PostgreSQL connection URL.")
        return False

    print("PASS: Required environment variables are present.")
    return True


async def check_database() -> bool:
    """Check that PostgreSQL accepts a connection and a simple query."""
    database_url = os.environ["DATABASE_URL"].strip()
    connection = None

    try:
        connection = await asyncpg.connect(
            dsn=database_url,
            timeout=10,
            command_timeout=10,
            server_settings={"application_name": "coursevault-healthcheck"},
        )
        result = await connection.fetchval("SELECT 1")

        if result != 1:
            print("FAIL: PostgreSQL returned an unexpected result.")
            return False

        print("PASS: PostgreSQL connection and query succeeded.")
        return True

    except (asyncpg.PostgresError, OSError, TimeoutError, ValueError):
        print(
            "FAIL: Could not connect to PostgreSQL or execute a query. "
            "Check DATABASE_URL, credentials, network access, and database status."
        )
        return False

    finally:
        if connection is not None and not connection.is_closed():
            await connection.close()


async def main() -> int:
    print("CourseVault deployment health check")
    print("-----------------------------------")

    if not check_environment():
        return 1

    if not await check_database():
        return 1

    print("-----------------------------------")
    print("PASS: All health checks passed.")
    print("Note: This does not verify Telegram authentication or payment processing.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nHealth check interrupted.")
        raise SystemExit(130)
