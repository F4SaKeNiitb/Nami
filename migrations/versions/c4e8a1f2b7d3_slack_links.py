"""slack links

Revision ID: c4e8a1f2b7d3
Revises: a3c5d7e9b102
Create Date: 2026-10-13 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import app.db.base


revision = 'c4e8a1f2b7d3'
down_revision = 'a3c5d7e9b102'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('slack_links',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('team_id', sa.String(length=32), nullable=False),
    sa.Column('slack_user_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_slack_links_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_slack_links')),
    sa.UniqueConstraint('team_id', 'slack_user_id', name=op.f('uq_slack_links_team_id'))
    )
    with op.batch_alter_table('slack_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_slack_links_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('slack_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_slack_links_user_id'))
    op.drop_table('slack_links')
