"""add owner role

Revision ID: 02281b725044
Revises: 0e7b7b308de4
Create Date: 2025-06-19 19:06:55.947959

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = '02281b725044'
down_revision = '0e7b7b308de4'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    conn.execute(text('INSERT INTO "role" (id, name, weight) VALUES (5, \'Owner\', 4) ON CONFLICT DO NOTHING'))
    conn.execute(text('UPDATE "user_role" SET role = 5 WHERE user_id = 1'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'approve registrations\') ON CONFLICT DO NOTHING'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'change user roles\') ON CONFLICT DO NOTHING'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'ban users\') ON CONFLICT DO NOTHING'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'manage users\') ON CONFLICT DO NOTHING'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'change instance settings\') ON CONFLICT DO NOTHING'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'administer all communities\') ON CONFLICT DO NOTHING'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'administer all users\') ON CONFLICT DO NOTHING'))
    conn.execute(text('INSERT INTO "role_permission" (role_id, permission) VALUES (5, \'edit cms pages\') ON CONFLICT DO NOTHING'))
    pass


def downgrade():
    conn = op.get_bind()
    conn.execute(text('DELETE FROM "role_permission" WHERE role_id = 5'))
    conn.execute(text('UPDATE "user_role" SET role_id = 4 WHERE role_id = 5'))
    conn.execute(text('DELETE FROM "role" WHERE id = 5'))
    pass
