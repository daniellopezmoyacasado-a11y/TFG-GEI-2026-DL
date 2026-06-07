import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jwt
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api_routes import register_api_routes
from auth.session import (
    clear_session_cookie,
    create_session_token,
    set_session_cookie,
    try_get_current_user,
)
from models import Base, PendingPresentation, User, UserVisa, Visa
from ui_routes import register_ui_routes
from wallet_client import WaltIdAuthError, WaltIdClient, WaltIdUnexpectedError


load_dotenv()


app = FastAPI(
    title="GA4GH Visa Broker Service",
    description=(
        "Broker service for storing GA4GH visas, issuing GA4GH passports, and coordinating "
        "presentation flows with walt.id verifier services. It exposes both JSON APIs and "
        "browser-oriented routes for the demo UI."
    ),
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
wallet_client = WaltIdClient()


def normalized_url(name: str, default: str) -> str:
    return os.getenv(name, default).rstrip("/")


def public_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


DATABASE_URL = os.getenv("BROKER_DATABASE_URL", "sqlite:///./visa_broker.db")
PASSPORT_ISSUER = normalized_url("PASSPORT_ISSUER", "https://broker.localhost")
PASSPORT_EXP_HOURS = int(os.getenv("PASSPORT_EXP_HOURS", "24"))
WALTID_VERIFIER_BASE_URL = normalized_url("WALTID_VERIFIER_BASE_URL", "https://verifier-api.localhost")
ISSUER_PUBLIC_URL = normalized_url("ISSUER_PUBLIC_URL", "https://issuer.localhost")
BROKER_PUBLIC_URL = normalized_url("BROKER_PUBLIC_URL", "https://broker.localhost")
VERIFIER_PUBLIC_URL = normalized_url("VERIFIER_PUBLIC_URL", "https://verifier.localhost")
WALLET_PUBLIC_URL = normalized_url("WALLET_PUBLIC_URL", "https://wallet.localhost")
ASYM_JWT_ALGORITHM = os.getenv("ASYM_JWT_ALGORITHM", "RS256")

ISSUER_JWT_PUBLIC_KEY = Path(os.getenv("ISSUER_JWT_PUBLIC_KEY_PATH")).read_text()
BROKER_JWT_PRIVATE_KEY = Path(os.getenv("BROKER_JWT_PRIVATE_KEY_PATH")).read_text()

templates.env.globals.update(
    issuer_public_url=ISSUER_PUBLIC_URL,
    broker_public_url=BROKER_PUBLIC_URL,
    verifier_public_url=VERIFIER_PUBLIC_URL,
    wallet_public_url=WALLET_PUBLIC_URL,
)


def should_render_error_page(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return False

    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept or "*/*" in accept


def render_error_page(request: Request, status_code: int, error: str):
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "error": error,
            "status_code": status_code,
            "back_url": "/dashboard",
        },
        status_code=status_code,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if should_render_error_page(request):
        return render_error_page(request, exc.status_code, str(exc.detail))
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if should_render_error_page(request):
        return render_error_page(request, 422, str(exc))
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if should_render_error_page(request):
        return render_error_page(request, 500, str(exc))
    return JSONResponse(status_code=500, content={"detail": str(exc)})


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def decode_visa_without_verification(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
            algorithms=[ASYM_JWT_ALGORITHM],
        )
    except Exception:
        return {}


def build_contained_visa_label(visa_row: Visa) -> str:
    payload = decode_visa_without_verification(visa_row.visa_jwt)
    ga4gh = payload.get("ga4gh_visa_v1", {})

    visa_name = ga4gh.get("visa_name")
    if visa_name:
        source = ga4gh.get("source") or ga4gh.get("visa_issuer") or "unknown"
        return f"{visa_name}@{source}"

    visa_type = ga4gh.get("type") or visa_row.visa_type or "UnknownVisaType"
    visa_value = ga4gh.get("value") or visa_row.visa_value or visa_row.jti
    source = ga4gh.get("source") or "unknown"
    return f"{visa_type}_{visa_value}@{source}"


def build_passport_payload(user: User, assigned_visas: list[UserVisa], subject: str) -> dict:
    now = datetime.now(timezone.utc)

    included_visa_jwts = []
    contained_visas = []
    expiry_candidates = []

    for assignment in assigned_visas:
        if not assignment.visa:
            continue

        included_visa_jwts.append(assignment.visa.visa_jwt)
        contained_visas.append(build_contained_visa_label(assignment.visa))

        payload = decode_visa_without_verification(assignment.visa.visa_jwt)
        visa_exp = payload.get("exp")
        if visa_exp is not None:
            try:
                expiry_candidates.append(int(visa_exp))
            except (TypeError, ValueError):
                pass

    if not included_visa_jwts:
        raise HTTPException(status_code=404, detail="User has no visas assigned")

    passport_exp = int((now + timedelta(hours=PASSPORT_EXP_HOURS)).timestamp())
    if expiry_candidates:
        passport_exp = min(passport_exp, min(expiry_candidates))

    return {
        "sub": subject,
        "scope": "openid",
        "contained_visas": contained_visas,
        "iss": PASSPORT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": passport_exp,
        "jti": str(uuid.uuid4()),
        "ga4gh_passport_v1": included_visa_jwts,
    }


def decode_and_validate_visa(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            ISSUER_JWT_PUBLIC_KEY,
            algorithms=[ASYM_JWT_ALGORITHM],
            audience=os.getenv("JWT_AUDIENCE", "ga4gh-passport-broker"),
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Visa has expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid visa token: {str(e)}")


def create_ga4gh_visa_presentation_request() -> dict:
    url = f"{WALTID_VERIFIER_BASE_URL}/openid4vc/verify"
    headers = {
        "accept": "*/*",
        "authorizeBaseUrl": "openid4vp://authorize",
        "responseMode": "direct_post",
        "successRedirectUri": public_url(BROKER_PUBLIC_URL, "/broker/presentation-success?id=$id"),
        "errorRedirectUri": public_url(BROKER_PUBLIC_URL, "/broker/presentation-error?id=$id"),
        "Content-Type": "application/json",
    }
    payload = {
        "request_credentials": [
            {
                "type": "Ga4ghVisaCredential",
                "format": "jwt_vc_json",
            }
        ]
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise Exception(f"Verifier error {resp.status_code}: {resp.text}")

    request_url = resp.text.strip()
    if not request_url.startswith("openid4vp://"):
        raise Exception(f"Unexpected verifier response: {request_url}")

    return {"request_url": request_url}


def create_frontend_ga4gh_visa_presentation_request() -> dict:
    url = f"{WALTID_VERIFIER_BASE_URL}/openid4vc/verify"
    headers = {
        "accept": "*/*",
        "authorizeBaseUrl": "openid4vp://authorize",
        "responseMode": "direct_post",
        "successRedirectUri": public_url(BROKER_PUBLIC_URL, "/ui/broker/presentation-success?id=$id"),
        "errorRedirectUri": public_url(BROKER_PUBLIC_URL, "/ui/broker/presentation-error?id=$id"),
        "Content-Type": "application/json",
    }
    payload = {
        "request_credentials": [
            {
                "type": "Ga4ghVisaCredential",
                "format": "jwt_vc_json",
            }
        ]
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise Exception(f"Verifier error {resp.status_code}: {resp.text}")

    request_url = resp.text.strip()
    if not request_url.startswith("openid4vp://"):
        raise Exception(f"Unexpected verifier response: {request_url}")

    return {"request_url": request_url}


def extract_session_id_from_request_url(request_url: str) -> str | None:
    parsed = urlparse(request_url)
    query = parse_qs(parsed.query)
    return query.get("state", [None])[0]


def get_presented_credentials(session_id: str) -> list:
    url = f"{WALTID_VERIFIER_BASE_URL}/openid4vc/session/{session_id}/presented-credentials"
    resp = requests.get(url, timeout=30)
    if resp.status_code >= 400:
        raise Exception(f"Verifier error {resp.status_code}: {resp.text}")
    return resp.json()


def store_visa_jwt_for_user(db, db_user: User, visa_jwt: str) -> dict:
    payload = decode_and_validate_visa(visa_jwt)

    jti = payload.get("jti")
    sub = payload.get("sub")
    ga4gh = payload.get("ga4gh_visa_v1", {})
    exp = payload.get("exp")

    if not jti:
        raise HTTPException(status_code=400, detail="Visa token has no jti")

    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc) if exp is not None else None
    visa_type = ga4gh.get("type")
    visa_value = ga4gh.get("value")

    db_visa = db.query(Visa).filter(Visa.jti == jti).first()
    created_visa = False
    if not db_visa:
        db_visa = Visa(
            jti=jti,
            visa_jwt=visa_jwt,
            sub=sub,
            visa_type=visa_type,
            visa_value=visa_value,
        )
        db.add(db_visa)
        db.commit()
        db.refresh(db_visa)
        created_visa = True

    existing_assignment = (
        db.query(UserVisa)
        .filter(UserVisa.user_id == db_user.id, UserVisa.visa_id == db_visa.id)
        .first()
    )

    assigned = False
    if not existing_assignment:
        assignment = UserVisa(
            user_id=db_user.id,
            visa_id=db_visa.id,
            expires_at=expires_at,
        )
        db.add(assignment)
        db.commit()
        assigned = True

    return {
        "id": db_visa.id,
        "jti": db_visa.jti,
        "sub": db_visa.sub,
        "type": db_visa.visa_type,
        "value": db_visa.visa_value,
        "created_visa": created_visa,
        "assigned": assigned,
    }


def cleanup_old_finished_pending_presentations(db):
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    deleted = (
        db.query(PendingPresentation)
        .filter(
            PendingPresentation.status.in_(["processed", "error", "expired"]),
            PendingPresentation.created_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted


def ensure_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


Base.metadata.create_all(bind=engine)

route_context = SimpleNamespace(
    ASYM_JWT_ALGORITHM=ASYM_JWT_ALGORITHM,
    BROKER_JWT_PRIVATE_KEY=BROKER_JWT_PRIVATE_KEY,
    PendingPresentation=PendingPresentation,
    SessionLocal=SessionLocal,
    User=User,
    UserVisa=UserVisa,
    Visa=Visa,
    WaltIdAuthError=WaltIdAuthError,
    WaltIdUnexpectedError=WaltIdUnexpectedError,
    build_passport_payload=build_passport_payload,
    cleanup_old_finished_pending_presentations=cleanup_old_finished_pending_presentations,
    clear_session_cookie=clear_session_cookie,
    create_frontend_ga4gh_visa_presentation_request=create_frontend_ga4gh_visa_presentation_request,
    create_ga4gh_visa_presentation_request=create_ga4gh_visa_presentation_request,
    create_session_token=create_session_token,
    decode_and_validate_visa=decode_and_validate_visa,
    ensure_utc=ensure_utc,
    extract_session_id_from_request_url=extract_session_id_from_request_url,
    get_presented_credentials=get_presented_credentials,
    jwt=jwt,
    set_session_cookie=set_session_cookie,
    store_visa_jwt_for_user=store_visa_jwt_for_user,
    templates=templates,
    try_get_current_user=try_get_current_user,
    wallet_client=wallet_client,
)

register_api_routes(app, route_context)
register_ui_routes(app, route_context)
