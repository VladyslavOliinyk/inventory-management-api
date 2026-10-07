"""add indexes and check constraint to stock_movements

Revision ID: a1bce81b5c76
Revises: b1e786f1232f
Create Date: 2026-10-07 13:22:30.907265

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1bce81b5c76'
down_revision: Union[str, Sequence[str], None] = 'b1e786f1232f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_stock_movements_quantity_positive",
        "stock_movements",
        "quantity > 0",
    )
    op.create_index('ix_stock_movements_product_id', 'stock_movements', ['product_id'], unique=False)
    op.create_index('ix_stock_movements_from_warehouse_id', 'stock_movements', ['from_warehouse_id'], unique=False)
    op.create_index('ix_stock_movements_to_warehouse_id', 'stock_movements', ['to_warehouse_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_stock_movements_to_warehouse_id', table_name='stock_movements')
    op.drop_index('ix_stock_movements_from_warehouse_id', table_name='stock_movements')
    op.drop_index('ix_stock_movements_product_id', table_name='stock_movements')
    op.drop_constraint("ck_stock_movements_quantity_positive", "stock_movements", type_="check")
