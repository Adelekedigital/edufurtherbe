"""The constraint naming convention on ``Base.metadata``.

Needs no database, so it runs on every machine and inside the pre-commit hook.

This is the cheapest guard against the most expensive failure in the migration
chain. An unnamed constraint takes whatever name the tooling emits at the moment
the migration runs — the database default on older Alembic, the convention here on
current versions. A later migration that hard-codes a name in order to rename it
then cannot run against an empty database, so no new environment can be
provisioned, and nothing reveals it until somebody tries.

The assertions are made against **compiled DDL** rather than against
``constraint.name``, because the DDL is what the database actually receives.
A convention that resolves in Python and not in the emitted statement would pass
an attribute check and still produce the wrong schema.

**There is deliberately no index test.** The ``ix`` key resolves to
``ix_%(column_0_label)s``, which is byte-identical to the name SQLAlchemy gives an
index with no convention at all. A test asserting it passes whether or not
``Base`` carries the convention, so it discriminates nothing — it was written,
observed to pass against a deliberately broken ``Base``, and removed. The other
four keys do discriminate, and they are covered below.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.infra.db.base import Base


def ddl(element: CreateTable) -> str:
    return str(element.compile(dialect=postgresql.dialect()))


@pytest.fixture
def probe_tables() -> Iterator[tuple[sa.Table, sa.Table]]:
    """Two throwaway tables on the real ``Base.metadata``, removed afterwards.

    They go on the real metadata on purpose — a private ``MetaData`` built from
    the same constant would assert the constant, not that ``Base`` carries it,
    which is the thing that can actually be wrong. They are removed again because
    ``Base.metadata`` is what Alembic autogenerate compares against.
    """
    parent = sa.Table(
        "probe_parent",
        Base.metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    child = sa.Table(
        "probe_child",
        Base.metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("probe_parent.id")),
        sa.Column("email", sa.String(320), unique=True),
        sa.Column("age", sa.Integer, index=True),
        sa.CheckConstraint("age >= 0", name="age_non_negative"),
    )
    try:
        yield parent, child
    finally:
        Base.metadata.remove(child)
        Base.metadata.remove(parent)


def test_primary_key_is_named_by_the_convention(probe_tables: tuple[sa.Table, sa.Table]) -> None:
    parent, _ = probe_tables

    assert "CONSTRAINT pk_probe_parent PRIMARY KEY" in ddl(CreateTable(parent))


def test_every_constraint_kind_is_named_by_the_convention(
    probe_tables: tuple[sa.Table, sa.Table],
) -> None:
    """One assertion per convention key, so a failure names which key broke."""
    _, child = probe_tables
    statement = ddl(CreateTable(child))

    assert "CONSTRAINT pk_probe_child PRIMARY KEY" in statement
    assert "CONSTRAINT uq_probe_child_email UNIQUE" in statement
    assert "CONSTRAINT ck_probe_child_age_non_negative CHECK" in statement
    assert "CONSTRAINT fk_probe_child_parent_id_probe_parent FOREIGN KEY" in statement


def test_no_constraint_is_emitted_unnamed(probe_tables: tuple[sa.Table, sa.Table]) -> None:
    """The failure mode itself, rather than a symptom of it.

    Without a convention SQLAlchemy emits bare clauses — ``PRIMARY KEY (id)``,
    ``UNIQUE (email)``, ``FOREIGN KEY(parent_id)`` — and PostgreSQL then invents
    ``probe_child_pkey`` and friends *at execution time*. Those invented names
    are what a later rename migration hard-codes and what makes the chain
    un-runnable from empty.

    Asserting the absence of the invented names would be a test that cannot
    fail: they never appear in compiled DDL, only in the live database. The
    unnamed clause is the thing visible here, so it is the thing asserted.
    """
    _, child = probe_tables
    statement = ddl(CreateTable(child))

    for unnamed in ("\n\tPRIMARY KEY (", "\n\tUNIQUE (", "\n\tFOREIGN KEY("):
        assert unnamed not in statement, f"constraint emitted unnamed: {unnamed.strip()}"
