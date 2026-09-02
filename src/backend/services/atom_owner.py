"""Shared atoms-owner resolver (#448).

`_resolve_owner_user_id` was copy-pasted identically into three atom-owning
services (rag / conversation_memory / knowledge_graph). This mixin is the single
canonical implementation of the `atoms.owner_user_id` back-fill policy, so a
fourth atom-owning service can't drift a fourth copy.

The host class must expose `self.db` (an `AsyncSession`). The per-instance
fallback cache lives on `_fallback_owner_id` (class-level `None` default → each
instance reads `None` until it resolves + caches its own).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class AtomOwnerResolverMixin:
    """Resolve a non-null `atoms.owner_user_id`, matching the migration back-fill.

    Policy (identical to `pc20260420_circles_v1_schema.py`): prefer the explicit
    `user_id`; else the first user by id (the bootstrap admin); else `None` only
    in empty-users fresh-DB dev setups, so callers skip atom registration
    (source row written with `atom_id=None`). Production always has the admin
    from bootstrap, so this is never `None` in real deploys.
    """

    # Host classes hold an AsyncSession here; declared for type-checkers only.
    db: AsyncSession

    _fallback_owner_id: int | None = None

    async def _resolve_owner_user_id(self, user_id: int | None) -> int | None:
        if user_id is not None:
            return user_id
        if self._fallback_owner_id is not None:
            return self._fallback_owner_id
        from models.database import User

        result = await self.db.execute(
            select(User.id).order_by(User.id.asc()).limit(1)
        )
        fallback = result.scalar()
        if fallback is None:
            return None
        self._fallback_owner_id = int(fallback)
        return self._fallback_owner_id
