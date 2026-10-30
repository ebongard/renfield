"""The preconditions of the second factor -- in ONE place.

🛑 THE DEFECT THIS MODULE IS WRITTEN AGAINST
============================================
The hurdle was raised on ONE condition and cleared on FIVE.

`/auth/login` raised it as soon as `voice_auth_enabled` was on. To redeem it,
`/auth/voice` additionally demanded: speaker recognition switched on, a linked
profile, at least one embedding -- and an embedding path that an older P0 guard
keeps shut in production. **Every gap between the two sets is a lockout**,
because there is deliberately no fallback to the password alone.

The comment at the login site already stated the rule -- "the hurdle only
applies when there is also a door" -- and then checked only one of its leaves.
That is why the check now lives here, and all three callers run through it:

* `POST /auth/login` -- raises the hurdle ONLY when it also falls.
* `POST /users/{id}/voice-second-factor` -- 409 instead of an armed consent
  that grasps at nothing.
* `POST /auth/voice` -- checks the same thing again before redeeming, because
  something may have changed between the password and the recording.

🛑 WHY THE CONSENT STAYS IN PLACE
=================================
When the door is missing, the hurdle RESTS -- the column is not deleted. Two
reasons:

1. It is exactly the behaviour already documented and intended for
   `voice_auth_enabled=false` (`docs/ENVIRONMENT_VARIABLES.md`): without a
   voice path nobody could redeem, so the requirement rests instead of locking
   the account out. One rule, not two.
2. The column IS the consent (Art. 9 GDPR). Deleting it silently when the
   profile is withdrawn would destroy the record of a declaration the person
   made. When the profile comes back, the requirement applies again by itself.

An attacker gains nothing from this: to put the hurdle to rest they have to
remove the profile, and that requires `users.manage` or `speakers.all` --
the same permission with which they could switch the consent off directly
anyway. No capability is new, only a log entry: the resting is written as a
WARNING, otherwise it would be a silent lowering of the protection.
"""

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import SpeakerEmbedding, User
from utils.config import settings

# Reasons, machine-readable. They NEVER go out to whoever is logging in (a
# failure answer distinguishes nothing), only into the log and into the 409
# answer of the administration route, where the person looks at their own
# account.
PATH_OFF = "voice_path_off"
RECOGNITION_OFF = "recognition_off"
NO_PROFILE = "no_profile"
NO_EMBEDDINGS = "no_embeddings"

# 🛑 ONLY for the 409 answer when switching on, never from
# `voice_factor_blocker()`: a device account does not log in through
# `/auth/login`, so it cannot read its own state at all. The code still stands
# HERE, because it answers the same question -- "why can this account not carry
# the factor" -- and because otherwise it goes out as prose again. That is
# exactly what it was: in the 2026-09-29 rework the three named English
# sentences were replaced and this fourth one, FOUR LINES above them in the
# same function, was left standing.
DEVICE_ACCOUNT = "device_account"


def voice_path_blocker() -> str | None:
    """What blocks the redemption path for ALL accounts, or ``None``.

    Instance switches only. Deliberately separate from the profile checker:
    when **switching on** the consent, `voice_auth_enabled=false` must not be
    an obstacle, otherwise the cutover order (collect consents first, then flip
    the flag) would not be workable.
    """
    if not settings.voice_auth_enabled:
        return PATH_OFF
    # A voiceprint is biometric data. With recognition off, none is computed
    # for the login either -- the same principle as in the resolver.
    if not settings.speaker_recognition_enabled:
        return RECOGNITION_OFF
    return None


async def voice_profile_blocker(db: AsyncSession, user: User) -> str | None:
    """What stops THIS account from redeeming, or ``None``.

    A linked profile without embeddings is the same case as none at all: there
    would be nothing to compare against.
    """
    if not user.speaker_id:
        return NO_PROFILE
    count = (await db.execute(
        select(func.count()).select_from(SpeakerEmbedding)
        .where(SpeakerEmbedding.speaker_id == user.speaker_id)
    )).scalar() or 0
    return None if count else NO_EMBEDDINGS


async def voice_factor_blocker(db: AsyncSession, user: User) -> str | None:
    """What blocks the second factor for THIS account today, or ``None``.

    The full truth: first the instance switches, then the profile. Separate
    from the SWITCH-ON check in `POST /users/{id}/voice-second-factor`, and
    that is deliberate -- there `PATH_OFF` must not block, otherwise the
    cutover order (collect consents first, then flip the flag) would not be
    workable. For DISPLAY, by contrast, `PATH_OFF` counts: a hurdle that rests
    because the voice path is off should be named as such to the person too.

    🛑 The reason is INDEPENDENT of the consent. The "Mein Konto" page
    previously showed it only in the "armed" state -- so never for somebody who
    is just considering consenting. Measured on 2026-09-29 in the household:
    `NO_PROFILE` applies to 6 of 7 accounts, and exactly those saw a bare "off"
    with a button that was bound to fail.
    """
    return voice_path_blocker() or await voice_profile_blocker(db, user)


async def second_factor_applies(db: AsyncSession, user: User) -> bool:
    """May `/auth/login` withhold the tokens from this account?

    Only when the consent is on record AND the way out is open. Otherwise the
    hurdle rests -- with a WARNING, because a resting hurdle is a protection
    that is currently not in effect, and the operator should see that in the
    log.
    """
    if not user.voice_second_factor_enabled:
        return False

    blocker = await voice_factor_blocker(db, user)
    if blocker is None:
        return True

    if blocker != PATH_OFF:
        # `PATH_OFF` is the documented normal state of both instances and
        # would flood the log on every login. The other three are deviations
        # somebody should fix.
        logger.warning(
            f"Voice second factor RESTS for user {user.id}: {blocker}. "
            f"The consent is recorded but cannot be redeemed, so the password "
            f"path stays open — restore the profile or revoke the consent."
        )
    return False


def voice_factor_lock_id(user_id: int) -> str:
    """The id under which the failed attempts of the SECOND factor are counted.

    🛑 Deliberately a different namespace from the user name: a failed voice
    attempt must NOT lock the password path as well, otherwise the second
    factor would be a way to push somebody out of their account using
    somebody else's means.

    The finding behind why the id lives here instead of as a string in the
    route: the administration interface queried the lock under the USER NAME.
    The two never met -- the list reported "not locked", and the unlock button
    cleared nothing (`cleared_keys=0`), while the person really could not get
    in. On the very screen that is the recovery path. Whoever sets the lock and
    whoever displays or clears it now reads the id from here.
    """
    return f"voice2fa:{user_id}"
