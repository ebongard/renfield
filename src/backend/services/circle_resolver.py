"""
CircleResolver — access policy evaluation for circles v1.

Holds the access-check logic for any (asker, atom) pair. The PolicyEvaluator
generalizes over dimension shapes: 'ladder' (depth-ordered, e.g., the home
self/trusted/household/extended/public tier ladder) and 'set' (orthogonal
membership, e.g., enterprise multi-tenant or project-based access).

ASCII access-check flow:

    can_access_atom(asker, atom, ctx):
        │
        ├── asker.id == atom.owner_user_id  → True   (owner sees all)
        ├── tier == public_tier_index       → True   (public visible to everyone)
        ├── explicit grant exists for asker → True   (per-resource exception
        │                                              from atom_explicit_grants;
        │                                              MAX-permissive with tier)
        ├── asker has no membership in owner's circles → False
        └── PolicyEvaluator.satisfies(atom.policy, asker_memberships, dimensions)

    PolicyEvaluator.satisfies:
        For each dimension referenced by atom.policy:
            ladder: asker.value <= atom.policy.value      (depth-ordered)
            set:    asker.value == atom.policy.value      (orthogonal)
        ALL dimensions must pass (AND semantics).

Caching:
    - Per-process LRU keyed by (asker_id, owner_id) for tier resolution.
    - Invalidated on circle_memberships INSERT/UPDATE/DELETE for that pair.
    - Invalidated on atom tier changes (which may have been cached as
      part of can_access_atom rejection).
    - Single-process cache is sufficient for v1 (Renfield runs one backend
      container per docker-compose.yml). Multi-worker deployments will need
      Redis pub/sub invalidation in a future phase.
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Atom as AtomModel,
    AtomExplicitGrant,
    Circle,
    CircleMembership,
    User,
)
from services.atom_types import AccessContext, Atom, DimensionSpec


# Sentinel for "asker is not in any of this owner's circles".
# Stored in the cache to differentiate from "cache miss".
_NOT_A_MEMBER = object()


class PolicyEvaluator:
    """Stateless policy evaluation — separated from CircleResolver for testability."""

    @staticmethod
    def satisfies(
        atom_policy: dict[str, Any],
        asker_memberships: dict[str, Any],
        dimensions: dict[str, DimensionSpec],
    ) -> bool:
        """
        True iff the asker's memberships satisfy ALL dimensions in atom_policy.

        For each dimension referenced by atom_policy:
          - ladder: asker's value (depth index) MUST be <= atom's value
                    (smaller index = deeper/inner; deeper-placed members can
                    reach atoms that are at their depth or wider)
          - set:    asker's value MUST equal atom's value (set membership)

        Dimensions referenced by the atom but not by the asker's memberships
        fail closed (no membership in that dimension = no access).
        Dimensions in asker.memberships but NOT referenced by atom.policy
        are ignored (atom doesn't restrict on that dimension).

        EMPTY POLICY ({}) fails closed — an atom with no access dimensions
        is meaningless (the contract is "policy MUST specify at least one
        dimension"); treat as data corruption + deny access.
        """
        if not atom_policy:
            logger.warning(
                "PolicyEvaluator.satisfies received empty atom_policy — failing closed. "
                "This indicates data corruption or a writer that bypassed AtomService."
            )
            return False

        for dim_name, atom_value in atom_policy.items():
            if dim_name not in dimensions:
                # atom references a dimension this deployment doesn't have.
                # Fail closed for safety — could indicate misconfiguration.
                logger.warning(
                    f"Atom policy references unknown dimension '{dim_name}' — "
                    f"failing closed. Check circles.dimension_config."
                )
                return False

            spec = dimensions[dim_name]
            asker_value = asker_memberships.get(dim_name)
            if asker_value is None:
                return False  # not in this dimension = no access

            if spec.shape == "ladder":
                # Both values are integer depth indices
                if not isinstance(asker_value, int) or not isinstance(atom_value, int):
                    logger.warning(
                        f"Ladder dimension '{dim_name}' got non-int values: "
                        f"asker={asker_value!r}, atom={atom_value!r}"
                    )
                    return False
                if asker_value > atom_value:
                    return False  # asker placed too far out to reach this atom
            elif spec.shape == "set":
                # Set membership: must match exactly
                if asker_value != atom_value:
                    return False
            else:
                logger.warning(f"Unknown dimension shape '{spec.shape}' — failing closed")
                return False

        return True


class CircleResolver:
    """
    Per-process access resolver with class-level membership + grant caches.

    The caches live on the class (not the instance) so writes in one request
    immediately invalidate cached entries used by other in-flight requests
    within the same process. Renfield ships one backend container per
    docker-compose, so process-wide invalidation is sufficient for v1;
    multi-worker deployments will need Redis pub/sub in a future phase.

    Build a fresh CircleResolver per AsyncSession; the instance is just a
    convenience handle (db reference + sugar for `cls.invalidate_*`).
    """

    # Class-level caches — shared across all instances in this process.
    _membership_cache: dict[tuple[int, int], dict[str, Any] | object] = {}
    # owner_id, member_id -> dict[dimension, value]  OR  _NOT_A_MEMBER sentinel
    _explicit_grant_cache: dict[tuple[str, int], bool] = {}
    # (atom_id, asker_id) -> bool (True = explicit grant exists for asker on atom)

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_dimensions(self, owner_user_id: int) -> dict[str, DimensionSpec]:
        """
        Read the owner's dimension_config from circles row + parse to DimensionSpecs.

        Returns the home default (single 'tier' ladder) if no circles row exists
        for the owner — covers fresh users + AUTH_ENABLED=false single-user mode.
        """
        result = await self.db.execute(
            select(Circle).where(Circle.owner_user_id == owner_user_id)
        )
        circle = result.scalar_one_or_none()
        if circle is None:
            return _DEFAULT_HOME_DIMENSIONS

        config = circle.dimension_config or {}
        dims: dict[str, DimensionSpec] = {}
        for dim_name, spec_dict in config.items():
            shape = spec_dict.get("shape", "ladder")
            values = spec_dict.get("values")
            dims[dim_name] = DimensionSpec(shape=shape, values=values)
        return dims or _DEFAULT_HOME_DIMENSIONS

    async def get_memberships(
        self,
        circle_owner_id: int,
        member_user_id: int,
    ) -> dict[str, Any] | None:
        """
        Returns {dimension: value} for the member in the owner's circles,
        or None if the member is not in any of the owner's circles.
        """
        cache_key = (circle_owner_id, member_user_id)
        cached = self._membership_cache.get(cache_key)
        if cached is _NOT_A_MEMBER:
            return None
        if cached is not None:
            return cached  # type: ignore[return-value]

        result = await self.db.execute(
            select(CircleMembership).where(
                CircleMembership.circle_owner_id == circle_owner_id,
                CircleMembership.member_user_id == member_user_id,
            )
        )
        rows = result.scalars().all()
        if not rows:
            self._membership_cache[cache_key] = _NOT_A_MEMBER
            return None

        memberships = {row.dimension: row.value for row in rows}
        self._membership_cache[cache_key] = memberships
        return memberships

    async def has_explicit_grant(self, atom_id: str, asker_id: int) -> bool:
        """Per-resource exception grant check (atom_explicit_grants table)."""
        cache_key = (atom_id, asker_id)
        if cache_key in self._explicit_grant_cache:
            return self._explicit_grant_cache[cache_key]

        result = await self.db.execute(
            select(AtomExplicitGrant).where(
                AtomExplicitGrant.atom_id == atom_id,
                AtomExplicitGrant.granted_to_user_id == asker_id,
            )
        )
        has_grant = result.scalar_one_or_none() is not None
        self._explicit_grant_cache[cache_key] = has_grant
        return has_grant

    async def can_access_atom(
        self,
        asker: User | int,
        atom: Atom,
    ) -> bool:
        """
        Authoritative access check for a single (asker, atom) pair.

        See module docstring for the access-check flow.
        """
        asker_id = asker.id if isinstance(asker, User) else asker

        # Owner sees everything they own.
        if asker_id == atom.owner_user_id:
            return True

        # Public atoms visible to anyone (paired or not).
        # 'Public' is defined as the highest index in the owner's ladder dimension
        # (or any explicit max-tier indicator in policy).
        dimensions = await self.get_dimensions(atom.owner_user_id)
        tier_spec = dimensions.get("tier")
        if tier_spec is not None and tier_spec.shape == "ladder":
            public_idx = tier_spec.public_index
            if public_idx is not None and atom.policy.get("tier") == public_idx:
                return True

        # Per-resource explicit grant trumps tier (MAX-permissive with circles).
        if await self.has_explicit_grant(atom.atom_id, asker_id):
            return True

        # Tier / dimension membership check.
        memberships = await self.get_memberships(
            circle_owner_id=atom.owner_user_id,
            member_user_id=asker_id,
        )
        if memberships is None:
            return False  # not in any of owner's circles → only public visible (handled above)

        return PolicyEvaluator.satisfies(atom.policy, memberships, dimensions)

    async def get_max_visible_tier(
        self,
        asker_id: int,
        owner_id: int,
    ) -> int | None:
        """
        Convenience for SQL-side filter pushdown.

        Returns the smallest tier index the asker can reach in this owner's
        circles, or None if the asker is not a member of any owner circle
        (in which case the asker can only see public atoms; the caller should
        emit a `WHERE circle_tier = :public_idx` clause separately).
        """
        memberships = await self.get_memberships(owner_id, asker_id)
        if memberships is None:
            return None
        tier_value = memberships.get("tier")
        if tier_value is None:
            return None
        return int(tier_value) if isinstance(tier_value, int) else None

    @classmethod
    def invalidate_for_atom(cls, atom_id: str) -> None:
        """
        Clear cached explicit-grant entries for this atom across ALL askers
        in ALL in-flight requests within this process.
        Call when an atom's tier changes or an explicit grant is added/removed.
        """
        keys = [k for k in cls._explicit_grant_cache if k[0] == atom_id]
        for k in keys:
            cls._explicit_grant_cache.pop(k, None)

    @classmethod
    def invalidate_for_membership(cls, circle_owner_id: int, member_user_id: int) -> None:
        """
        Clear cached membership for this owner-member pair across the process.
        Call when CircleMembership is added/updated/deleted.
        """
        cls._membership_cache.pop((circle_owner_id, member_user_id), None)


# Default home dimension config: single 'tier' ladder with the standard 5 tiers.
# Used when an owner has no circles row yet (fresh user, AUTH_ENABLED=false, etc.).
_DEFAULT_HOME_DIMENSIONS: dict[str, DimensionSpec] = {
    "tier": DimensionSpec(
        shape="ladder",
        values=["self", "trusted", "household", "extended", "public"],
    ),
}


def atom_from_orm(atom_orm: AtomModel) -> Atom:
    """Convert an ORM Atom row to the immutable Atom dataclass."""
    return Atom(
        atom_id=atom_orm.atom_id,
        atom_type=atom_orm.atom_type,
        owner_user_id=atom_orm.owner_user_id,
        policy=dict(atom_orm.policy or {"tier": 0}),
        created_at=atom_orm.created_at,
        updated_at=atom_orm.updated_at,
        payload={},  # populated by AtomService.get_atom from source row when needed
    )
