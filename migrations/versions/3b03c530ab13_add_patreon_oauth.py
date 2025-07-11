"""Add Patreon OAuth

Revision ID: 3b03c530ab13
Revises: 1e80c8767811
Create Date: 2025-07-10 18:58:50.751310

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3b03c530ab13'
down_revision = '1e80c8767811'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('patreon_oauth_id', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_user_patreon_oauth_id'), ['patreon_oauth_id'], unique=True)


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_patreon_oauth_id'))
        batch_op.drop_column('patreon_oauth_id')
