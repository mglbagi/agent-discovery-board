import os

from dotenv import load_dotenv

# Needed here explicitly: this file runs before any app.* import (which is where
# .env normally gets loaded, via app/__init__.py), but the checks below need
# DATABASE_URL/TEST_DATABASE_URL already populated.
load_dotenv()

# app/core/discovery's SERVICE_BASE_URL guard requires this at import time.
os.environ.setdefault("SERVICE_BASE_URL", "http://127.0.0.1:8200")

# Deterministic, fast limits for every test unless a test overrides them itself.
os.environ["SIGNATURE_MAX_AGE_SECONDS"] = "300"
os.environ["LISTING_CREATE_RATE_LIMIT_MAX_REQUESTS"] = "1000"
os.environ["LISTING_CREATE_GLOBAL_DAILY_CAP"] = "100000"
os.environ["LISTING_MUTATE_RATE_LIMIT_MAX_REQUESTS"] = "1000"
os.environ["LISTING_MUTATE_GLOBAL_DAILY_CAP"] = "100000"
# Badge lookups default to disabled in tests unless a test explicitly enables them.
os.environ.pop("BOARD_PAYER_PRIVATE_KEY", None)

# Tests wipe the listings table before running, so they must NEVER run against the
# real production database. TEST_DATABASE_URL points at a separate database created
# specifically for this (see README "Independence from the verification service" -
# this is also a separate database from that service's own tests). This check is a
# hard structural guard, not just "use the right variable name": if TEST_DATABASE_URL
# is missing, or is identical to DATABASE_URL (e.g. someone forgets to set it and it
# falls through to production), the whole test session refuses to run rather than
# risk touching real data.
_prod_url = os.environ.get("DATABASE_URL")
_test_url = os.environ.get("TEST_DATABASE_URL")

if not _test_url:
    raise RuntimeError(
        "TEST_DATABASE_URL is not set. Tests must run against a separate database, "
        "never the real DATABASE_URL. Refusing to run rather than risk truncating "
        "production data."
    )

if _test_url == _prod_url:
    raise RuntimeError(
        "TEST_DATABASE_URL is identical to DATABASE_URL. Tests must run against a "
        "separate database. Refusing to run rather than risk truncating production data."
    )

# Redirect DATABASE_URL (what app.core.db actually reads) to the test database for
# the rest of this test session. Production's DATABASE_URL value is never used again
# after this point.
os.environ["DATABASE_URL"] = _test_url

import psycopg  # noqa: E402

with psycopg.connect(_test_url) as _conn:
    _conn.execute("DROP TABLE IF EXISTS listings")
    _conn.commit()
