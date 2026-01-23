"""add auth tables

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2026-01-22 10:00:00.000000

Adds Role, User, and KBPermission tables for RPBAC authentication system.
Also adds owner_id to knowledge_bases and user_id to conversations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6g7h8i9'
down_revision: Union[str, None] = 'c3d4e5f6g7h8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # === Create roles table ===
    op.create_table(
        'roles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('permissions', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('is_system', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_roles_id', 'roles', ['id'])
    op.create_index('ix_roles_name', 'roles', ['name'], unique=True)

    # === Create users table ===
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(100), nullable=False),
        sa.Column('email', sa.String(255), nullable=True),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('speaker_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id']),
        sa.ForeignKeyConstraint(['speaker_id'], ['speakers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('speaker_id')
    )
    op.create_index('ix_users_id', 'users', ['id'])
    op.create_index('ix_users_username', 'users', ['username'], unique=True)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    # === Create kb_permissions table ===
    op.create_table(
        'kb_permissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('knowledge_base_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('permission', sa.String(20), nullable=False, server_default='read'),
        sa.Column('granted_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['granted_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_kb_permissions_id', 'kb_permissions', ['id'])
    op.create_index('ix_kb_permissions_kb_id', 'kb_permissions', ['knowledge_base_id'])
    op.create_index('ix_kb_permissions_user_id', 'kb_permissions', ['user_id'])
    op.create_index(
        'idx_kb_permissions_kb_user',
        'kb_permissions',
        ['knowledge_base_id', 'user_id'],
        unique=True
    )

    # === Add owner_id and is_public to knowledge_bases ===
    op.add_column(
        'knowledge_bases',
        sa.Column('owner_id', sa.Integer(), nullable=True)
    )
    op.add_column(
        'knowledge_bases',
        sa.Column('is_public', sa.Boolean(), nullable=False, server_default='false')
    )
    op.create_foreign_key(
        'fk_knowledge_bases_owner_id',
        'knowledge_bases', 'users',
        ['owner_id'], ['id']
    )
    op.create_index('ix_knowledge_bases_owner_id', 'knowledge_bases', ['owner_id'])

    # === Add user_id to conversations ===
    op.add_column(
        'conversations',
        sa.Column('user_id', sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        'fk_conversations_user_id',
        'conversations', 'users',
        ['user_id'], ['id']
    )
    op.create_index('ix_conversations_user_id', 'conversations', ['user_id'])


def downgrade() -> None:
    # === Remove user_id from conversations ===
    op.drop_index('ix_conversations_user_id', 'conversations')
    op.drop_constraint('fk_conversations_user_id', 'conversations', type_='foreignkey')
    op.drop_column('conversations', 'user_id')

    # === Remove owner_id and is_public from knowledge_bases ===
    op.drop_index('ix_knowledge_bases_owner_id', 'knowledge_bases')
    op.drop_constraint('fk_knowledge_bases_owner_id', 'knowledge_bases', type_='foreignkey')
    op.drop_column('knowledge_bases', 'is_public')
    op.drop_column('knowledge_bases', 'owner_id')

    # === Drop kb_permissions table ===
    op.drop_index('idx_kb_permissions_kb_user', 'kb_permissions')
    op.drop_index('ix_kb_permissions_user_id', 'kb_permissions')
    op.drop_index('ix_kb_permissions_kb_id', 'kb_permissions')
    op.drop_index('ix_kb_permissions_id', 'kb_permissions')
    op.drop_table('kb_permissions')

    # === Drop users table ===
    op.drop_index('ix_users_email', 'users')
    op.drop_index('ix_users_username', 'users')
    op.drop_index('ix_users_id', 'users')
    op.drop_table('users')

    # === Drop roles table ===
    op.drop_index('ix_roles_name', 'roles')
    op.drop_index('ix_roles_id', 'roles')
    op.drop_table('roles')
