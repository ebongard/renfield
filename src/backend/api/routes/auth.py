"""
Authentication API Routes

Provides endpoints for user authentication:
- Login (username/password → JWT tokens)
- Register (create new user account)
- Refresh (get new access token using refresh token)
- Me (get current user info)
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.security import OAuth2PasswordRequestForm
from loguru import logger
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import Role, User
from models.permissions import get_all_permissions
from services.api_rate_limiter import limiter
from services.auth_service import (
    create_access_token,
    create_refresh_token,
    create_user,
    decode_token,
    get_current_user,
    get_optional_user,
    get_role_by_name,
    get_user_by_id,
    oauth2_scheme,
    require_auth,
    validate_password,
)
from services.database import get_db
from utils.config import settings

router = APIRouter()


# =============================================================================
# Request/Response Models
# =============================================================================

class TokenResponse(BaseModel):
    """Response model for successful authentication."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    # #694: tells the client the user must rotate their password before the
    # token unlocks anything beyond /change-password. Enforced server-side in
    # get_current_user; this flag lets the SPA redirect straight to the form.
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    """Request model for token refresh."""
    refresh_token: str


class SsoExchangeRequest(BaseModel):
    """Exchange a one-time SSO hand-off code for the session tokens.

    Replaces the URL-fragment token hand-off: the SPA receives an opaque
    ``code`` (+ ``state``) in the callback URL and POSTs it here with the PKCE
    ``code_verifier`` it generated when starting the login. See
    docs/design/sso-token-handoff-hardening.md."""
    code: str
    code_verifier: str
    state: str


class RegisterRequest(BaseModel):
    """Request model for user registration."""
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=8)
    email: EmailStr | None = None


class UserResponse(BaseModel):
    """Response model for user information."""
    id: int
    username: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None
    role: str
    role_id: int
    permissions: list[str]
    is_active: bool
    personality_style: str = "freundlich"
    personality_prompt: str | None = None
    created_at: datetime
    last_login: datetime | None
    speaker_id: int | None
    # Security (review M5): surface the forced-rotation flag so /auth/me and the
    # login response are actionable (the frontend can force a password change).
    must_change_password: bool = False

    class Config:
        from_attributes = True


class ChangePasswordRequest(BaseModel):
    """Request model for password change."""
    current_password: str
    new_password: str = Field(..., min_length=8)


class AuthStatusResponse(BaseModel):
    """Response model for authentication status."""
    auth_enabled: bool
    allow_registration: bool
    authenticated: bool
    user: UserResponse | None = None
    features: dict[str, bool] = {}


# =============================================================================
# Endpoints
# =============================================================================

@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.api_rate_limit_auth)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    """
    Authenticate user and return JWT tokens.

    Uses OAuth2 password flow (username + password in form data).
    Returns access token (short-lived) and refresh token (long-lived).
    """
    # Pluggable auth provider registry (ebongard/renfield#591). This
    # generalizes the pre-registry "authenticate hook → bcrypt fallback"
    # two-step without breaking it: the legacy `authenticate` hook is still
    # honored first; the DB/LDAP/social providers run the credential walk;
    # `post_authenticate` is fired exactly once before the JWT is minted.
    # See auth/login_flow.py for the full resolution + standalone-fallback
    # contract.
    from auth.login_flow import resolve_login
    from services.login_lockout import login_lockout
    from utils.metrics import record_login_failure

    # Account lockout (#693): a locked username is rejected BEFORE the credential
    # walk (so a lockout also stops password-guessing that happens to be
    # correct). Response is the SAME opaque 401 as bad credentials — never a
    # distinct status — so it is not a username-enumeration oracle. The event is
    # observable via the log + metric, not the response.
    if await login_lockout.is_locked(form_data.username):
        record_login_failure("locked_out")
        logger.warning(
            f"Login rejected: account locked out (username={form_data.username!r})"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    outcome = await resolve_login(
        db=db,
        username=form_data.username,
        password=form_data.password,
        channel="web",
    )

    if outcome is None:
        # Bad credentials OR a registered post_authenticate consumer declined
        # to resolve — both are an opaque 401 (do not leak which).
        record_login_failure("bad_credentials")
        tripped = await login_lockout.record_failure(form_data.username)
        logger.warning(
            f"Login failed: bad credentials (username={form_data.username!r})"
            + (" — account now locked out" if tripped else "")
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Re-load on the request session to mint tokens / update last_login, and
    # enforce is_active uniformly. NOTE: pre-registry, a legacy `authenticate`
    # hook returning a *disabled* User would still get tokens (only the bcrypt
    # path checked is_active). The registry path now rejects inactive users on
    # every path (intended hardening). The response is the SAME opaque 401 as
    # bad credentials — never a distinct 403 — so login does not leak whether a
    # disabled account exists (no user-enumeration oracle).
    user = await get_user_by_id(db, outcome.user_id)
    if not user or not user.is_active:
        # A disabled/vanished account is the SAME opaque 401 (no 403) so login
        # never leaks that the account exists. A failed attempt here still counts
        # toward lockout (a valid-username-but-disabled probe is still a probe).
        record_login_failure("inactive")
        await login_lockout.record_failure(form_data.username)
        logger.warning(
            f"Login failed: account missing or inactive (username={form_data.username!r})"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Successful auth — clear any accumulated lockout state for this username.
    await login_lockout.clear(form_data.username)

    # Update last login time
    user.last_login = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()

    # Create tokens. `sub` (= renfield user id) is the only consumed identity
    # claim; the cosmetic `username` claim now carries display_name (no
    # consumer reads it — verified design decision #6).
    access_token = create_access_token(
        data={"sub": str(user.id), "username": outcome.display_name}
    )
    refresh_token = create_refresh_token(user.id)

    logger.info(
        f"User logged in: {user.username} "
        f"(provider={outcome.provider_id})"
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        must_change_password=user.must_change_password,
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.api_rate_limit_auth)
async def refresh_token(
    request: Request,
    refresh_request: RefreshRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Get a new access token using a refresh token.

    Refresh tokens are long-lived and can only be used to get new access tokens.
    """
    payload = decode_token(refresh_request.refresh_token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await get_user_by_id(db, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # #698: refresh-token rotation WITH reuse-detection. A refresh token is
    # single-use. (1) If its jti is already blacklisted it was spent (rotated) or
    # revoked → this is a replay of a stolen/old token → reject. (2) Otherwise
    # blacklist it now so it can never be replayed. NB: decode_token() only
    # validates signature/exp and does NOT consult the blacklist (that check lives
    # in get_current_user, for access tokens), so /refresh must do it explicitly.
    # is_blacklisted() fails CLOSED — a Redis outage rejects refresh (re-login)
    # rather than silently honoring a possibly-revoked token. Without this a
    # stolen refresh token stayed valid its full 30-day life, revocable only by
    # rotating SECRET_KEY.
    old_jti = payload.get("jti")
    old_exp = payload.get("exp")
    if old_jti:
        from services.token_blacklist import token_blacklist
        if await token_blacklist.is_blacklisted(old_jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token already used or revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if old_exp:
            import time
            ttl = int(old_exp - time.time())
            if ttl > 0:
                await token_blacklist.add(old_jti, ttl)

    # Create new tokens
    access_token = create_access_token(
        data={"sub": str(user.id), "username": user.username}
    )
    new_refresh_token = create_refresh_token(user.id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        # #694: a token refresh must carry the still-current flag, else the SPA
        # would think rotation is done and stop redirecting to /change-password
        # (while the server keeps returning opaque 403s).
        must_change_password=user.must_change_password,
    )


@router.post("/sso/exchange", response_model=TokenResponse)
@limiter.limit(settings.api_rate_limit_auth)
async def sso_exchange(
    request: Request,
    exchange: SsoExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a one-time SSO hand-off code for the session tokens.

    The token-in-URL replacement: a federated login redirects the SPA to
    ``…/auth/callback?code=&state=`` (opaque code only); the SPA POSTs the code +
    its PKCE ``code_verifier`` + ``state`` here and gets the tokens in the body.
    The code is single-use (atomic GETDEL) and PKCE/state-bound, so a leaked code
    is worthless. Every failure returns the SAME opaque 400 — no oracle for
    which check failed. Gated by ``sso_handoff_enabled`` (404 when off). See
    docs/design/sso-token-handoff-hardening.md.
    """
    if not settings.sso_handoff_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    from services.sso_handoff_store import consume_handoff_code, verify_pkce_s256

    bad = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired authorization code",
    )
    # Consume first (single-use): even a wrong verifier/state burns the code, so
    # an attacker who raced the victim to a leaked code can't retry it.
    session = await consume_handoff_code(exchange.code)
    if session is None:
        raise bad
    if not verify_pkce_s256(exchange.code_verifier, session.code_challenge):
        raise bad
    # constant-time state compare (CSRF binding to the initiating tab)
    import hmac
    if not hmac.compare_digest(exchange.state, session.state):
        raise bad

    # The session was minted at callback time; re-check the user is still valid.
    user = await get_user_by_id(db, session.user_id)
    if not user or not user.is_active:
        raise bad

    logger.info(f"SSO hand-off exchanged: user={user.username} provider={session.provider!r}")
    return TokenResponse(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        token_type="bearer",
        expires_in=session.expires_in,
        must_change_password=session.must_change_password,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(settings.api_rate_limit_auth)
async def register(
    request: Request,
    register_request: RegisterRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Register a new user account.

    New users are assigned the "Gast" (Guest) role by default.
    Registration can be disabled via settings.
    """
    if not settings.allow_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled"
        )

    # Validate password
    is_valid, error = validate_password(register_request.password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error
        )

    # Get default role (Gast)
    default_role = await get_role_by_name(db, "Gast")
    if not default_role:
        # Fallback: get any non-admin role
        result = await db.execute(
            select(Role).where(Role.name != "Admin").limit(1)
        )
        default_role = result.scalar_one_or_none()

        if not default_role:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No default role available"
            )

    # Create user
    user = await create_user(
        db=db,
        username=register_request.username,
        password=register_request.password,
        role_id=default_role.id,
        email=register_request.email
    )

    logger.info(f"New user registered: {user.username}")

    return UserResponse(
        id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        role=user.role.name,
        role_id=user.role_id,
        permissions=user.get_permissions(),
        is_active=user.is_active,
        personality_style=user.personality_style,
        personality_prompt=user.personality_prompt,
        created_at=user.created_at,
        last_login=user.last_login,
        speaker_id=user.speaker_id
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    user: User = Depends(require_auth)
):
    """
    Get information about the currently authenticated user.

    Returns user details including role and permissions.
    """
    return UserResponse(
        id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        role=user.role.name,
        role_id=user.role_id,
        permissions=user.get_permissions(),
        is_active=user.is_active,
        personality_style=user.personality_style,
        personality_prompt=user.personality_prompt,
        created_at=user.created_at,
        last_login=user.last_login,
        speaker_id=user.speaker_id
    )


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db)
):
    """
    Change the current user's password.

    Requires the current password for verification.
    """
    from services.auth_service import get_password_hash, verify_password

    # Verify current password
    if not verify_password(request.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )

    # Validate new password
    is_valid, error = validate_password(request.new_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error
        )

    # Update password and clear the forced-rotation flag (review M5): the flag
    # was set (e.g. for an auto-generated admin password) but never cleared, so
    # the control was inert. Clearing it here makes the rotation gate functional.
    user.password_hash = get_password_hash(request.new_password)
    user.must_change_password = False
    await db.commit()

    logger.info(f"Password changed for user: {user.username}")

    return {"message": "Password changed successfully"}


@router.post("/logout")
async def logout(
    user: User = Depends(require_auth),
    token: str = Depends(oauth2_scheme),
):
    """
    Logout the current user by revoking their access token.

    The token's JTI is added to a Redis blacklist with TTL matching
    the token's remaining lifetime.
    """
    from services.token_blacklist import token_blacklist

    if token:
        payload = decode_token(token)
        if payload:
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                import time
                ttl = int(exp - time.time())
                if ttl > 0:
                    await token_blacklist.add(jti, ttl)

    logger.info(f"User logged out: {user.username}")
    return {"message": "Successfully logged out"}


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status(
    user: User | None = Depends(get_optional_user)
):
    """
    Get authentication status and settings.

    Returns whether auth is enabled, if user is authenticated, etc.
    Useful for frontend to determine what to show. This endpoint MUST
    be reachable without credentials — the frontend calls it before
    the user has logged in, precisely to decide whether to show a
    login page. Depends on ``get_optional_user`` (returns None when
    no token is present) instead of ``get_current_user`` (which
    raises 401 when auth is enabled but no token is supplied).
    """
    user_response = None
    if user:
        user_response = UserResponse(
            id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            email=user.email,
            role=user.role.name,
            role_id=user.role_id,
            permissions=user.get_permissions(),
            is_active=user.is_active,
            personality_style=user.personality_style,
            personality_prompt=user.personality_prompt,
            created_at=user.created_at,
            last_login=user.last_login,
            speaker_id=user.speaker_id
        )

    return AuthStatusResponse(
        auth_enabled=settings.auth_enabled,
        allow_registration=settings.allow_registration,
        authenticated=user is not None,
        user=user_response,
        features=settings.features
    )


@router.get("/permissions")
async def list_all_permissions():
    """
    List all available permissions in the system.

    Useful for admin UIs when creating/editing roles.
    """
    return get_all_permissions()


# =============================================================================
# Voice Authentication Endpoints
# =============================================================================

class VoiceAuthResponse(BaseModel):
    """Response model for voice authentication."""
    success: bool
    speaker_id: int | None = None
    speaker_name: str | None = None
    confidence: float = 0.0
    user_id: int | None = None
    username: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    message: str


@router.post("/voice", response_model=VoiceAuthResponse)
@limiter.limit(settings.api_rate_limit_auth)
async def voice_authenticate(
    request: Request,
    audio_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Authenticate using voice (speaker recognition).

    Requires:
    - voice_auth_enabled in settings
    - Speaker profile linked to a User account

    Process:
    1. Receive audio file
    2. Run speaker recognition
    3. If speaker identified with confidence >= threshold:
       - Check if speaker is linked to a User
       - If linked, return JWT tokens
    4. Otherwise, return identification result without tokens
    """
    from services.speaker_service import SpeakerService

    if not settings.voice_auth_enabled:
        return VoiceAuthResponse(
            success=False,
            message="Voice authentication is disabled"
        )

    # Read audio file
    audio_bytes = await audio_file.read()

    if len(audio_bytes) == 0:
        return VoiceAuthResponse(
            success=False,
            message="Empty audio file"
        )

    try:
        # Get speaker service
        speaker_service = SpeakerService()

        # Identify speaker
        result = speaker_service.identify_speaker(audio_bytes)

        if not result or not result.get("speaker_id"):
            return VoiceAuthResponse(
                success=False,
                confidence=result.get("confidence", 0.0) if result else 0.0,
                message="Speaker not recognized"
            )

        speaker_id = result["speaker_id"]
        confidence = result.get("confidence", 0.0)
        speaker_name = result.get("name", "Unknown")

        # Check confidence threshold
        if confidence < settings.voice_auth_min_confidence:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                message=f"Confidence too low ({confidence:.2f} < {settings.voice_auth_min_confidence})"
            )

        # Check if speaker is linked to a user
        from sqlalchemy.orm import selectinload

        from models.database import Speaker, User

        speaker_result = await db.execute(
            select(Speaker).where(Speaker.id == speaker_id)
        )
        speaker = speaker_result.scalar_one_or_none()

        if not speaker:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                message="Speaker profile not found in database"
            )

        # Check if speaker is linked to a user
        user_result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(User.speaker_id == speaker_id)
        )
        user = user_result.scalar_one_or_none()

        if not user:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                message="Speaker is not linked to a user account"
            )

        if not user.is_active:
            return VoiceAuthResponse(
                success=False,
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                confidence=confidence,
                user_id=user.id,
                username=user.username,
                message="User account is disabled"
            )

        # Success! Generate tokens
        user.last_login = datetime.now(UTC).replace(tzinfo=None)
        await db.commit()

        access_token = create_access_token(
            data={"sub": str(user.id), "username": user.username}
        )
        refresh_token = create_refresh_token(user.id)

        logger.info(f"Voice authentication successful: {user.username} (speaker: {speaker_name}, confidence: {confidence:.2f})")

        return VoiceAuthResponse(
            success=True,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            confidence=confidence,
            user_id=user.id,
            username=user.username,
            access_token=access_token,
            refresh_token=refresh_token,
            message="Voice authentication successful"
        )

    except Exception as e:
        logger.error(f"Voice authentication error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return VoiceAuthResponse(
            success=False,
            message="Voice authentication failed"
        )
