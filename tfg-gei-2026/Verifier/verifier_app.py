import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
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
from models import Base, GenomicDataset, PendingPresentation
from ui_routes import register_ui_routes
from wallet_client import WaltIdAuthError, WaltIdClient, WaltIdUnexpectedError


load_dotenv()


wallet_client = WaltIdClient()


def normalized_url(name: str, default: str) -> str:
    return os.getenv(name, default).rstrip("/")


def public_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


DATABASE_URL = os.getenv("VERIFIER_DATABASE_URL", "sqlite:///./verifier.db")
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "ga4gh-passport-broker")
PASSPORT_ISSUER = normalized_url("PASSPORT_ISSUER", "https://broker.localhost")
WALTID_VERIFIER_BASE_URL = normalized_url("WALTID_VERIFIER_BASE_URL", "https://verifier-api.localhost")
ISSUER_PUBLIC_URL = normalized_url("ISSUER_PUBLIC_URL", "https://issuer.localhost")
BROKER_PUBLIC_URL = normalized_url("BROKER_PUBLIC_URL", "https://broker.localhost")
VERIFIER_PUBLIC_URL = normalized_url("VERIFIER_PUBLIC_URL", "https://verifier.localhost")
WALLET_PUBLIC_URL = normalized_url("WALLET_PUBLIC_URL", "https://wallet.localhost")
ASYM_JWT_ALGORITHM = os.getenv("ASYM_JWT_ALGORITHM", "RS256")

ISSUER_JWT_PUBLIC_KEY = Path(os.getenv("ISSUER_JWT_PUBLIC_KEY_PATH")).read_text()
BROKER_JWT_PUBLIC_KEY = Path(os.getenv("BROKER_JWT_PUBLIC_KEY_PATH")).read_text()


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def seed_datasets(db):
    datasets = [
        {
            "name": "Red Dataset",
            "file_path": "datasets/ds_red.txt",
            "file_size": "2MB",
            "num_downloads": 0,
            "description": "Mock genomic dataset for red access group",
            "required_visa_value": "https://example.org/datasets/red",
            "color": "red",
        },
        {
            "name": "Yellow Dataset",
            "file_path": "datasets/ds_yellow.txt",
            "file_size": "3MB",
            "num_downloads": 0,
            "description": "Mock genomic dataset for yellow access group",
            "required_visa_value": "https://example.org/datasets/yellow",
            "color": "yellow",
        },
        {
            "name": "Blue Dataset",
            "file_path": "datasets/ds_blue.txt",
            "file_size": "1.5MB",
            "num_downloads": 0,
            "description": "Mock genomic dataset for blue access group",
            "required_visa_value": "https://example.org/datasets/blue",
            "color": "blue",
        },
        {
            "name": "Purple Dataset",
            "file_path": "datasets/ds_purple.txt",
            "file_size": "4MB",
            "num_downloads": 0,
            "description": "Mock genomic dataset for purple access group",
            "required_visa_value": "https://example.org/datasets/purple",
            "color": "purple",
        },
    ]

    for item in datasets:
        existing = (
            db.query(GenomicDataset)
            .filter(GenomicDataset.required_visa_value == item["required_visa_value"])
            .first()
        )

        if not existing:
            db.add(GenomicDataset(**item))

    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        seed_datasets(db)
    finally:
        db.close()

    yield


app = FastAPI(
    title="GA4GH Passport Verifier Service",
    description=(
        "Verifier service for checking GA4GH Visa JWTs, validating GA4GH passports, and "
        "granting access to demo datasets through wallet presentation flows or direct "
        "passport submission."
    ),
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
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
            "session_id": request.query_params.get("id"),
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


def decode_unverified(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
                "verify_iss": False,
            },
            algorithms=[ASYM_JWT_ALGORITHM],
        )
    except Exception:
        return {}


def verify_passport_jwt(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            BROKER_JWT_PUBLIC_KEY,
            algorithms=[ASYM_JWT_ALGORITHM],
            issuer=PASSPORT_ISSUER,
            options={"require": ["sub", "iss", "iat", "exp", "ga4gh_passport_v1"]},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Passport has expired")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid passport issuer")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid passport token: {e}")


def verify_visa_jwt(token: str) -> Dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            ISSUER_JWT_PUBLIC_KEY,
            algorithms=[ASYM_JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            options={"require": ["sub", "iss", "iat", "exp", "ga4gh_visa_v1"]},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Visa has expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid visa audience")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid visa token: {e}")


def summarize_visa_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    ga4gh = payload.get("ga4gh_visa_v1", {})
    return {
        "sub": payload.get("sub"),
        "iss": payload.get("iss"),
        "iat": payload.get("iat"),
        "exp": payload.get("exp"),
        "type": ga4gh.get("type"),
        "value": ga4gh.get("value"),
        "source": ga4gh.get("source"),
        "by": ga4gh.get("by"),
        "visa_name": ga4gh.get("visa_name"),
    }


def create_ga4gh_visa_presentation_request() -> Dict[str, str]:
    url = f"{WALTID_VERIFIER_BASE_URL}/openid4vc/verify"
    headers = {
        "accept": "*/*",
        "authorizeBaseUrl": "openid4vp://authorize",
        "responseMode": "direct_post",
        "successRedirectUri": public_url(VERIFIER_PUBLIC_URL, "/verifier/presentation-success?id=$id"),
        "errorRedirectUri": public_url(VERIFIER_PUBLIC_URL, "/verifier/presentation-error?id=$id"),
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


def create_frontend_ga4gh_visa_presentation_request() -> Dict[str, str]:
    url = f"{WALTID_VERIFIER_BASE_URL}/openid4vc/verify"
    headers = {
        "accept": "*/*",
        "authorizeBaseUrl": "openid4vp://authorize",
        "responseMode": "direct_post",
        "successRedirectUri": public_url(VERIFIER_PUBLIC_URL, "/ui/verifier/presentation-success?id=$id"),
        "errorRedirectUri": public_url(VERIFIER_PUBLIC_URL, "/ui/verifier/presentation-error?id=$id"),
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


def extract_session_id_from_request_url(request_url: str) -> Optional[str]:
    parsed = urlparse(request_url)
    query = parse_qs(parsed.query)
    return query.get("state", [None])[0]


def get_presented_credentials(session_id: str) -> Dict[str, Any]:
    url = f"{WALTID_VERIFIER_BASE_URL}/openid4vc/session/{session_id}/presented-credentials"
    resp = requests.get(url, timeout=30)

    if resp.status_code >= 400:
        raise Exception(f"Verifier error {resp.status_code}: {resp.text}")

    return resp.json()


def extract_ga4gh_visa_jwts_from_presented_credentials(
    presented_credentials: Dict[str, Any]
) -> List[str]:
    extracted_visas: List[str] = []

    credentials_by_format = presented_credentials.get("credentialsByFormat", {})
    jwt_vc_entries = credentials_by_format.get("jwt_vc_json", [])

    for entry in jwt_vc_entries:
        verifiable_credentials = entry.get("verifiableCredentials", [])

        for vc_entry in verifiable_credentials:
            payload = vc_entry.get("payload", {})
            vc = payload.get("vc", {})

            if not isinstance(vc, dict):
                continue

            vc_types = vc.get("type", [])
            if isinstance(vc_types, str):
                vc_types = [vc_types]

            if "Ga4ghVisaCredential" not in vc_types:
                continue

            credential_subject = vc.get("credentialSubject", {})
            if not isinstance(credential_subject, dict):
                continue

            visa_jwt = credential_subject.get("ga4ghVisaJwt")
            if visa_jwt:
                extracted_visas.append(visa_jwt)

    return extracted_visas


def cleanup_old_finished_pending_presentations(db):
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    deleted = (
        db.query(PendingPresentation)
        .filter(
            PendingPresentation.status.in_(["processed", "error", "expired", "denied", "consumed"]),
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


route_context = SimpleNamespace(
    GenomicDataset=GenomicDataset,
    PendingPresentation=PendingPresentation,
    SessionLocal=SessionLocal,
    WaltIdAuthError=WaltIdAuthError,
    WaltIdUnexpectedError=WaltIdUnexpectedError,
    cleanup_old_finished_pending_presentations=cleanup_old_finished_pending_presentations,
    create_frontend_ga4gh_visa_presentation_request=create_frontend_ga4gh_visa_presentation_request,
    create_ga4gh_visa_presentation_request=create_ga4gh_visa_presentation_request,
    decode_unverified=decode_unverified,
    ensure_utc=ensure_utc,
    extract_ga4gh_visa_jwts_from_presented_credentials=extract_ga4gh_visa_jwts_from_presented_credentials,
    extract_session_id_from_request_url=extract_session_id_from_request_url,
    get_presented_credentials=get_presented_credentials,
    summarize_visa_payload=summarize_visa_payload,
    templates=templates,
    verify_passport_jwt=verify_passport_jwt,
    verify_visa_jwt=verify_visa_jwt,
    wallet_client=wallet_client,
)

register_api_routes(app, route_context)
register_ui_routes(app, route_context)
