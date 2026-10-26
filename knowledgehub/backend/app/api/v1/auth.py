import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import get_current_user
from app.core import clients
from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.repositories import users as users_repo
from app.schemas.auth import LoginIn, RegisterIn

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterIn) -> dict:
    existing = await users_repo.get_by_email(payload.email)
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    user = await users_repo.create(payload.email, hash_password(payload.password))
    return users_repo.public_view(user)


@router.post("/login")
async def login(payload: LoginIn) -> dict:
    user = await users_repo.get_by_email(payload.email)
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    token, expires_at = create_access_token(user["_id"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    _user: dict = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    payload = decode_access_token(credentials.credentials)
    jti = payload.get("jti", "")
    exp = payload.get("exp")
    if jti and exp:
        ttl = max(int(exp - time.time()), 1)
        await clients.redis_client().set(f"blacklist:{jti}", "1", ex=ttl)
