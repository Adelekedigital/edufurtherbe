"""Settled decision #100, step one: the orphaned ``unlisted_reason`` type leaves.

**No column changes type here, and that is the whole value of the step.** The
conversion from PostgreSQL enums to ``text`` + ``CHECK`` runs to eight migrations
across 21 columns; this one moves no data, so if it fails, it fails on the
harness rather than on a table rewrite. `test_migrations.py` already asserts the
exact set of enum types at head, in both directions, which is what makes a
one-line change a real proof.

``unlisted_reason`` was attached to no column at all — verified against the live
schema, not recalled: ``pg_attribute`` reports zero. It reached head that way
because ``mentor_profiles.unlisted_reason`` was replaced by
``mentor_status_events`` in `f2a8c31b7e45`, and a ``DROP COLUMN`` does not remove
a type. `test_migrations.py` states the rule this drop follows — *a type no table
uses is a schema asserting a choice nobody took*.

**The vocabulary is not going anywhere.** ``UnlistedReason`` is still written and
read: `pause` writes ``mentor_paused``, `decide` writes ``never_approved``, and
`may_self_resume` reads the newest unlisting back and compares it. All of that is
plain ``text`` in ``mentor_status_events.reason`` today and is unaffected.

**And that column deliberately gets no CHECK**, which is the one thing to not
undo later. ``reason`` is not a closed set: it holds free text on a decline, an
``UnlistedReason`` value on a self-pause, and **free admin text on an unlisting**
— ``DeclineRequest.reason`` reaches it through `set_listing`, a thousand
characters of whatever was typed. A ``CHECK`` naming the four values would reject
the admin path outright, and conditioning it on ``status_type = 'unlisted'``
rejects it too, because that is precisely the event the free text lands on.
Constraining it needs the ``reason_code`` / ``reason_text`` split that
``SessionReasonCode`` already documents, and that is a schema change and a
separate decision. Until then the class sits in ``UNCONSTRAINED_ENUMS`` with that
reason written down.

One thing for whoever builds the deferred platform tables:
``schema/08_features_platform.sql`` declares
``search_impressions_suppressed.suppression_reason unlisted_reason NOT NULL``.
Building it verbatim re-creates the type this drops. #100 governs new
vocabularies as well as old ones — that column is ``text`` + ``CHECK``, or it is
a regression with every gate green.
"""

from __future__ import annotations

from alembic import op

revision = "c9d4e2a71f68"
down_revision = "a3c81f7e5b24"
branch_labels = None
depends_on = None

#: Declaration order, transcribed from ``app.domain.enums.UnlistedReason`` and
#: from `8fb6c26ead9d`, which created the type with exactly these labels.
#:
#: **Order is not cosmetic in a downgrade.** PostgreSQL sorts an enum by label
#: declaration order, so recreating this alphabetised would restore a type that
#: compares differently from the one that was dropped. Nothing reads it today —
#: no query in ``infra/db/`` orders by an enum column, re-verified for this step —
#: which makes an alphabetised rebuild silent rather than harmless.
#:
#: Duplicated from the model layer rather than imported, per decision #43: no
#: migration in this chain imports from ``app``, because a later edit to a live
#: constant would silently change what an old revision does. The copy is pinned
#: by ``test_the_unlisted_reason_labels_have_one_meaning``.
UNLISTED_REASON_LABELS = "'mentor_paused', 'admin_review', 'dormant', 'never_approved'"


def upgrade() -> None:
    op.execute("DROP TYPE unlisted_reason")


def downgrade() -> None:
    """Recreates the type, and nothing attaches it — which is correct.

    The type had no column when this migration ran, so a downgrade that wired it
    to one would be inventing schema rather than restoring it.
    """
    op.execute(f"CREATE TYPE unlisted_reason AS ENUM ({UNLISTED_REASON_LABELS})")
