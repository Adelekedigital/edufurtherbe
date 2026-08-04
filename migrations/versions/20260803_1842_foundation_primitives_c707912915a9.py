"""foundation primitives

Extensions and the two functions every later table depends on. No tables, no
enums, no data — those arrive with the phases that use them.

Revision ID: c707912915a9
Revises:
Create Date: 2026-08-03 18:42:43.444729

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c707912915a9"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# `gen_random_uuid()` has been core since PostgreSQL 13, so this extension is not
# strictly required today. It is declared anyway: uuid_generate_v7() below calls
# that function, every row in the database will depend on it, and an implicit
# dependency on a built-in that a restricted search_path or a future managed
# platform could move is not worth saving one idempotent statement over.
CREATE_EXTENSIONS = "CREATE EXTENSION IF NOT EXISTS pgcrypto;"

# UUIDv7 per RFC 9562: a 48-bit big-endian millisecond timestamp, then the
# version and variant bits, then randomness. Time-ordered, so inserts land at the
# right-hand edge of the index instead of scattering across it, and it does not
# leak row counts the way a sequence does.
#
# PostgreSQL 18 ships uuidv7() natively. On 18+, replace the body of this
# function with `SELECT uuidv7()` rather than changing every table's DEFAULT —
# the indirection is the point.
#
# Application code generates ids with Python's stdlib `uuid.uuid7()` (3.14+).
# This DEFAULT is the safety net for rows written outside the ORM: the ETL,
# psql, and admin fixes.
CREATE_UUID_V7 = """
CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
  unix_ts_ms bytea;
  uuid_bytes bytea;
BEGIN
  unix_ts_ms = substring(
    int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3
  );
  uuid_bytes = uuid_send(gen_random_uuid());
  uuid_bytes = overlay(uuid_bytes PLACING unix_ts_ms FROM 1 FOR 6);
  -- version 7 in the high nibble of byte 6
  uuid_bytes = set_byte(
    uuid_bytes, 6, (b'0111' || get_byte(uuid_bytes, 6)::bit(4))::bit(8)::int
  );
  -- RFC 9562 variant: the top two bits of byte 8 are 10
  uuid_bytes = set_byte(
    uuid_bytes, 8, (b'10' || get_byte(uuid_bytes, 8)::bit(6))::bit(8)::int
  );
  RETURN encode(uuid_bytes, 'hex')::uuid;
END
$$;
"""

# Unconditional, matching the source DDL in docs/edufurther-migration/. One rule:
# `updated_at` is when *our* row last changed, and no caller can forge it.
#
# A conditional version was written first and withdrawn, and the reasoning is
# recorded here because the problem it addressed is real and someone will
# rediscover it.
#
# THE PROBLEM. The ETL must be idempotent on legacy_bubble_id, so re-running an
# importer issues UPDATEs, and this trigger overwrites whatever Bubble recorded
# as the row's modified date with the importer's clock — silently, with row
# counts and null-rate reconciliation both still passing.
#
# THE FIX THAT DOES NOT WORK: "stamp now() only when the caller did not itself
# change updated_at". An idempotent importer writes the *same* legacy timestamp
# the row already holds, so NEW and OLD are equal, the guard cannot distinguish
# that from a caller who never mentioned the column, and it stamps anyway. It
# passes a hand-written probe that changes the value and fails the real access
# pattern — a guard that looks correct and protects nothing.
#
# THE FIX THAT DOES: keep the source system's timestamps as their own columns on
# migrated tables — legacy_created_at / legacy_modified_at alongside
# legacy_bubble_id — and let created_at/updated_at mean what they mean
# everywhere else. Bubble's modified date is source data, not our row's metadata,
# and giving it its own home is this schema's stated principle rather than a
# workaround. Those columns land with the first migrated table, in M1.
CREATE_SET_UPDATED_AT = """
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END
$$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(CREATE_EXTENSIONS)
    op.execute(CREATE_UUID_V7)
    op.execute(CREATE_SET_UPDATED_AT)


def downgrade() -> None:
    """Downgrade schema.

    Drops both functions. **Deliberately does not drop the pgcrypto extension.**

    Dropping an extension is not symmetric with creating it: other objects may
    have come to depend on it, `DROP EXTENSION` cascades or fails depending on
    that, and the re-upgrade is idempotent either way because the CREATE is
    `IF NOT EXISTS`. Leaving it is the honest asymmetry rather than a forgotten
    statement.

    Nothing is lost here — there is no data at this revision.
    """
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    op.execute("DROP FUNCTION IF EXISTS uuid_generate_v7();")
