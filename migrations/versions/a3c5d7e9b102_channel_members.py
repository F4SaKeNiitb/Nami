"""channel members

Revision ID: a3c5d7e9b102
Revises: f6777e56fb04
Create Date: 2026-10-07 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
import app.db.base


revision = 'a3c5d7e9b102'
down_revision = 'f6777e56fb04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('channel_members',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('org_id', sa.String(length=32), nullable=False),
    sa.Column('channel_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.String(length=32), nullable=False),
    sa.Column('created_at', app.db.base.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['channel_id'], ['channels.id'], name=op.f('fk_channel_members_channel_id_channels'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['orgs.id'], name=op.f('fk_channel_members_org_id_orgs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_channel_members_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_channel_members')),
    sa.UniqueConstraint('channel_id', 'user_id', name=op.f('uq_channel_members_channel_id'))
    )
    with op.batch_alter_table('channel_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_channel_members_channel_id'), ['channel_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_channel_members_org_id'), ['org_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_channel_members_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('channel_members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_channel_members_user_id'))
        batch_op.drop_index(batch_op.f('ix_channel_members_org_id'))
        batch_op.drop_index(batch_op.f('ix_channel_members_channel_id'))

    op.drop_table('channel_members')
