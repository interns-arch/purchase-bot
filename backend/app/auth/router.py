from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from backend.app.auth import service as auth_service
from backend.app.auth.dependencies import get_current_user, oauth2_scheme
from backend.app.auth.models import User
from backend.app.auth.schemas import ChangePasswordRequest, TokenResponse, UserOut
from backend.app.auth.security import create_access_token, decode_access_token
from core.logging_setup import get_logger
from backend.app.database.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = get_logger(__name__)


@router.post("/login", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
) -> TokenResponse:
    user = auth_service.authenticate_user(form_data.username, form_data.password, db)
    if user is None:
        logger.warning("Failed login attempt for username=%r", form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, _jti, _expires_at = create_access_token(
        subject=user.username, extra_claims={"role": user.role}
    )
    logger.info("User %r logged in", user.username)
    return TokenResponse(access_token=token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    token: str | None = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    payload = decode_access_token(token)
    expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc).replace(tzinfo=None)
    auth_service.revoke_token(payload["jti"], expires_at, db)
    logger.info("User %r logged out", current_user.username)


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    try:
        auth_service.change_password(
            current_user, payload.current_password, payload.new_password, db
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info("User %r changed their password", current_user.username)
