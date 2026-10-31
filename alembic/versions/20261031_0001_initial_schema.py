"""Create the ChocoMarket persistence schema without destroying users.

Revision ID: 20261031_0001
Revises:
Create Date: 2026-10-31

This is a guarded baseline migration.  It can initialize an empty database and
adopt the database created by the pre-Alembic application.  Existing tables
are never dropped or rebuilt, so valid user rows and their credentials remain
unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20261031_0001"
down_revision = None
branch_labels = None
depends_on = None


USER_REQUIRED_COLUMNS = {"id", "name", "email", "password"}
SAFE_USER_COLUMNS = {
    "role": (sa.String(20), sa.text("'customer'")),
    "status": (sa.String(20), sa.text("'active'")),
    # SQLite only permits constant defaults when adding a column in place.  The
    # rows are immediately backfilled to CURRENT_TIMESTAMP below.
    "created_at": (sa.DateTime(timezone=False), sa.text("'1970-01-01 00:00:00'")),
    "updated_at": (sa.DateTime(timezone=False), sa.text("'1970-01-01 00:00:00'")),
}


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _table_columns(table_name: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table_name)}


def _ensure_columns(table_name: str, required_columns: Iterable[str]) -> None:
    """Refuse to guess when an existing table cannot safely be adopted."""
    missing = set(required_columns) - _table_columns(table_name)
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(
            f"Existing table {table_name!r} is missing required columns: {names}. "
            "No destructive migration was attempted."
        )


def _ensure_index(
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    indexes = {index["name"] for index in _inspector().get_indexes(table_name)}
    if index_name not in indexes:
        op.create_index(index_name, table_name, columns, unique=unique)


def _create_users_table() -> None:
    if not _table_exists("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("password", sa.String(255), nullable=False),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
            sa.CheckConstraint(
                "role IN ('customer', 'admin')",
                name="user_role",
            ),
            sa.CheckConstraint(
                "status IN ('active', 'inactive')",
                name="user_status",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        return

    # The application previously created users without Alembic.  Identity,
    # email, and credential columns cannot be inferred safely, so abort rather
    # than rewrite the table or invent values.  Metadata fields have explicit,
    # backward-compatible defaults and can be added in place.
    _ensure_columns("users", USER_REQUIRED_COLUMNS)
    for name, (column_type, default) in SAFE_USER_COLUMNS.items():
        if name not in _table_columns("users"):
            op.add_column(
                "users",
                sa.Column(
                    name,
                    column_type,
                    nullable=False,
                    server_default=default,
                ),
            )
            if name in {"created_at", "updated_at"}:
                # Use the actual migration time for legacy rows instead of the
                # compatibility default required by SQLite's ALTER TABLE.
                op.execute(
                    sa.text(
                        f"UPDATE users SET {name} = CURRENT_TIMESTAMP "
                        f"WHERE {name} = '1970-01-01 00:00:00'"
                    )
                )


def _create_products_table() -> None:
    if _table_exists("products"):
        _ensure_columns(
            "products",
            {
                "id",
                "name",
                "description",
                "price_cents",
                "stock_quantity",
                "status",
                "created_at",
                "updated_at",
            },
        )
        return

    op.create_table(
        "products",
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False, server_default=""),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("stock_quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 160",
            name="ck_products_name_length",
        ),
        sa.CheckConstraint(
            "length(description) <= 2000",
            name="ck_products_description_length",
        ),
        sa.CheckConstraint(
            "price_cents BETWEEN 1 AND 1000000",
            name="ck_products_price_cents_range",
        ),
        sa.CheckConstraint(
            "stock_quantity BETWEEN 0 AND 1000000",
            name="ck_products_stock_quantity_range",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive', 'out_of_stock')",
            name="product_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_orders_table() -> None:
    if _table_exists("orders"):
        _ensure_columns(
            "orders",
            {"id", "user_id", "status", "total_cents", "created_at", "updated_at"},
        )
        return

    op.create_table(
        "orders",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.CheckConstraint(
            "total_cents >= 1",
            name="ck_orders_total_cents_positive",
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'canceled')",
            name="order_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_orders_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_order_items_table() -> None:
    if _table_exists("order_items"):
        _ensure_columns(
            "order_items",
            {
                "id",
                "order_id",
                "product_id",
                "quantity",
                "unit_price_cents",
                "subtotal_cents",
                "created_at",
                "updated_at",
            },
        )
        return

    op.create_table(
        "order_items",
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 100",
            name="ck_order_items_quantity_range",
        ),
        sa.CheckConstraint(
            "unit_price_cents BETWEEN 1 AND 1000000",
            name="ck_order_items_unit_price_cents_range",
        ),
        sa.CheckConstraint(
            "subtotal_cents >= 1",
            name="ck_order_items_subtotal_cents_positive",
        ),
        sa.CheckConstraint(
            "subtotal_cents = quantity * unit_price_cents",
            name="ck_order_items_subtotal_matches_quantity_price",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_items_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_order_items_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_id",
            "product_id",
            name="uq_order_items_order_product",
        ),
    )


def _create_idempotency_table() -> None:
    if _table_exists("idempotency_records"):
        _ensure_columns(
            "idempotency_records",
            {
                "id",
                "scope",
                "key",
                "request_fingerprint",
                "status_code",
                "response_body",
                "created_at",
                "expires_at",
            },
        )
        return

    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(512), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint(
            "length(scope) > 0",
            name="ck_idempotency_records_scope_nonempty",
        ),
        sa.CheckConstraint(
            "length(key) BETWEEN 16 AND 128",
            name="ck_idempotency_records_key_length",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) > 0",
            name="ck_idempotency_records_fingerprint_nonempty",
        ),
        sa.CheckConstraint(
            "status_code BETWEEN 100 AND 599",
            name="ck_idempotency_records_status_code_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "key",
            name="uq_idempotency_records_scope_key",
        ),
    )


def upgrade() -> None:
    """Initialize missing schema pieces while preserving existing rows."""
    _create_users_table()
    _create_products_table()
    _create_orders_table()
    _create_order_items_table()
    _create_idempotency_table()

    # Index creation is also guarded because the pre-Alembic database may
    # already contain some of these indexes.
    _ensure_index(
        "ix_users_email",
        "users",
        ["email"],
        unique=True,
    )
    _ensure_index(
        "ix_users_status_created_at_id",
        "users",
        ["status", "created_at", "id"],
    )
    _ensure_index("ix_users_role", "users", ["role"])
    _ensure_index("ix_users_name", "users", ["name"])

    _ensure_index("ix_products_status", "products", ["status"])
    _ensure_index(
        "ix_products_status_created_at_id",
        "products",
        ["status", "created_at", "id"],
    )
    _ensure_index("ix_products_price_cents", "products", ["price_cents"])

    _ensure_index(
        "ix_orders_user_id_created_at_id",
        "orders",
        ["user_id", "created_at", "id"],
    )
    _ensure_index(
        "ix_orders_status_created_at_id",
        "orders",
        ["status", "created_at", "id"],
    )
    _ensure_index("ix_orders_created_at_id", "orders", ["created_at", "id"])

    _ensure_index("ix_order_items_order_id", "order_items", ["order_id"])
    _ensure_index("ix_order_items_product_id", "order_items", ["product_id"])
    _ensure_index(
        "ix_idempotency_records_expires_at",
        "idempotency_records",
        ["expires_at"],
    )


def downgrade() -> None:
    """Prevent an accidental destructive rollback of application data."""
    raise RuntimeError(
        "The baseline migration is non-destructive by design and cannot be downgraded."
    )
