"""Authentication routes.

Endpoints:
  POST   /api/v1/auth/register          — sign up with email + password
  POST   /api/v1/auth/login             — get access + refresh tokens
  POST   /api/v1/auth/logout            — revoke refresh token
  POST   /api/v1/auth/refresh           — exchange refresh → new access token
  GET    /api/v1/auth/verify-email      — verify email from link
  POST   /api/v1/auth/forgot-password   — send reset email
  POST   /api/v1/auth/reset-password    — set new password via reset token
  GET    /api/v1/auth/me                — current user profile
  PATCH  /api/v1/auth/me                — update name / avatar
  GET    /api/v1/auth/google            — get Google OAuth redirect URL
  GET    /api/v1/auth/google/callback   — handle Google OAuth callback
"""
import httpx
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from app.core.dependencies import DBSession, SettingsDep, get_current_user
from app.core.limiter import limiter
from app.db.models.user import User
from app.exceptions.custom_exceptions import UnauthorizedError
from app.services import auth_service

log = structlog.get_logger()
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── request / response schemas ────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class UpdateProfileRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_verified: bool
    avatar_url: str | None

    @classmethod
    def from_user(cls, u: User) -> "UserResponse":
        return cls(
            id=str(u.id),
            email=u.email,
            name=u.name,
            role=u.role.value,
            is_verified=u.is_verified,
            avatar_url=u.avatar_url,
        )


class AuthTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest, db: DBSession, settings: SettingsDep):
    """Create a new account. Sends a verification email before access is granted."""
    await auth_service.register_user(db, settings, body.email, body.name, body.password)
    return {"message": "registration successful — please check your email to verify your account"}


@router.post("/login")
@limiter.limit("10/minute")
async def login(
    request: Request, body: LoginRequest, db: DBSession, settings: SettingsDep
) -> AuthTokenResponse:
    """Exchange credentials for a JWT access token and a refresh token."""
    access_token, refresh_token, user = await auth_service.login_user(
        db, settings, body.email, body.password
    )
    return AuthTokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserResponse.from_user(user),
    )


@router.post("/logout", status_code=204)
async def logout(body: LogoutRequest, db: DBSession):
    """Revoke the supplied refresh token. The access token stays valid until it expires."""
    await auth_service.logout_user(db, body.refresh_token)


@router.post("/refresh")
@limiter.limit("20/minute")
async def refresh(
    request: Request, body: RefreshRequest, db: DBSession, settings: SettingsDep
):
    """Issue a fresh access token from a valid (non-revoked) refresh token."""
    access_token = await auth_service.refresh_access_token(db, settings, body.refresh_token)
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/verify-email")
async def verify_email(token: str, db: DBSession, settings: SettingsDep):
    """Confirm email ownership via the signed token from the verification email."""
    user = await auth_service.verify_email(db, settings, token)
    if settings.frontend_url:
        return RedirectResponse(url=f"{settings.frontend_url}/auth/verified")
    return {"message": "email verified successfully", "user": UserResponse.from_user(user)}


@router.post("/forgot-password", status_code=202)
@limiter.limit("3/minute")
async def forgot_password(
    request: Request, body: ForgotPasswordRequest, db: DBSession, settings: SettingsDep
):
    """Send a password-reset email. Always returns 202 to avoid leaking email existence."""
    await auth_service.forgot_password(db, settings, body.email)
    return {"message": "if that email is registered, a reset link has been sent"}


@router.post("/reset-password")
@limiter.limit("5/minute")
async def reset_password(
    request: Request, body: ResetPasswordRequest, db: DBSession, settings: SettingsDep
):
    """Set a new password using the signed token from the reset email."""
    await auth_service.reset_password(db, settings, body.token, body.new_password)
    return {"message": "password reset successfully — please log in"}


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return the profile of the currently authenticated user."""
    return UserResponse.from_user(current_user)


@router.patch("/me")
async def update_me(
    body: UpdateProfileRequest,
    db: DBSession,
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Update mutable profile fields (name)."""
    if body.name is not None:
        current_user.name = body.name
        await db.commit()
        await db.refresh(current_user)
    return UserResponse.from_user(current_user)


# ── Google OAuth 2.0 ──────────────────────────────────────────────────────────

@router.get("/google")
async def google_oauth_start(settings: SettingsDep):
    """Return the Google authorization URL. The frontend should redirect the user there."""
    if not settings.google_client_id:
        return {"error": "Google OAuth is not configured on this server"}
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return {"url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}"}


@router.get("/google/callback")
async def google_oauth_callback(
    code: str,
    db: DBSession,
    settings: SettingsDep,
):
    """Handle the OAuth2 callback from Google.

    On success, redirect the browser to the SPA at
    `${frontend_url}/auth/google-callback#access_token=...&refresh_token=...`
    so the SPA can pick up the tokens from the URL fragment.

    Putting tokens in the fragment (not the query string) prevents them from being
    logged by intermediate proxies or sent to the backend. The SPA reads them via
    `window.location.hash` and immediately clears the fragment.
    """
    if not settings.google_client_id:
        raise UnauthorizedError("Google OAuth is not configured on this server")

    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        google_tokens = token_resp.json()

        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {google_tokens['access_token']}"},
        )
        userinfo_resp.raise_for_status()
        info = userinfo_resp.json()

    try:
        access_token, refresh_token, _user = await auth_service.get_or_create_google_user(
            db, settings,
            google_id=info["sub"],
            email=info["email"],
            name=info.get("name", info["email"]),
            avatar_url=info.get("picture"),
        )
    except UnauthorizedError as exc:
        return RedirectResponse(
            url=f"{settings.frontend_url}/login?error={exc.args[0] if exc.args else 'oauth_failed'}"
        )

    fragment = f"access_token={access_token}&refresh_token={refresh_token}"
    return RedirectResponse(url=f"{settings.frontend_url}/auth/google-callback#{fragment}")
