from datetime import datetime, timedelta, timezone
from fastapi import Request, HTTPException
from fastapi.responses import Response
import jwt
import os

SESSION_COOKIE_NAME = "issuer_session"
SESSION_EXPIRE_HOURS = int(os.getenv("SESSION_EXPIRE_HOURS", "8"))
SESSION_SECRET = os.getenv("SESSION_SECRET", os.getenv("JWT_SECRET", "super-secret-change-me"))
SESSION_ALGORITHM = os.getenv("SESSION_ALGORITHM", "HS256")
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}


def create_session_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=SESSION_EXPIRE_HOURS)

    payload = {
        "sub": user["email"],
        "email": user["email"],
        "user_id": user.get("user_id"),
        "auth_source": user.get("auth_source", "wallet"),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }

    return jwt.encode(payload, SESSION_SECRET, algorithm=SESSION_ALGORITHM)


def decode_session_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SESSION_SECRET, algorithms=[SESSION_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_EXPIRE_HOURS * 3600,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME)


def get_current_user_from_request(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return decode_session_token(token)

def try_get_current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        return decode_session_token(token)
    except:
        return None
