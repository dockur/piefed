"""Fix null values in total_subscriptions_count column

Revision ID: 7b8bf43fa079
Revises: 46b2b16d498b
Create Date: 2026-09-01 18:35:32.469136

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7b8bf43fa079'
down_revision = '46b2b16d498b'
branch_labels = None
depends_on = None


def upgrade():
    # Some old installs might have NULL values left in total_subscriptions_count column
    # Update these to be 0 instead to avoid messing up sorting on that column
    op.execute("UPDATE community SET total_subscriptions_count = 0 WHERE total_subscriptions_count IS NULL")


def downgrade():
    # Nothing to do for downgrading here
    pass
