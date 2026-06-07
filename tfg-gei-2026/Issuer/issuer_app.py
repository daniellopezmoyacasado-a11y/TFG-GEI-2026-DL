from fastapi import FastAPI, HTTPException, Query, Request, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
import os
import uuid
import requests
import json
import jwt


from auth.session import (
    create_session_token,
    set_session_cookie,
    clear_session_cookie,
    get_current_user_from_request,
    try_get_current_user,
)

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from wallet_client import WaltIdClient, WaltIdAuthError, WaltIdUnexpectedError

app = FastAPI(
    title="GA4GH Visa Issuer Service",
    description=(
        "Issuer service for GA4GH visas. It provides browser-based login and registration, "
        "creates signed GA4GH Visa JWTs, and can generate wallet credential offers through "
        "the walt.id issuer API."
    ),
)

from dotenv import load_dotenv
load_dotenv()

wallet_client = WaltIdClient()

def normalized_url(name: str, default: str) -> str:
    return os.getenv(name, default).rstrip("/")


JWT_ISSUER = normalized_url("JWT_ISSUER", "https://issuer.localhost")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "ga4gh-passport-broker")
VISA_EXP_HOURS = int(os.getenv("VISA_EXP_HOURS", "24"))
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-change-me")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256") #current version changed to assymetric, this jwt is still used for the session token however

# walt.id Issuer API access
WALTID_ISSUER_URL = normalized_url("WALTID_ISSUER_URL", "https://issuer-api.localhost")
WALTID_ISSUER_DID = os.getenv("WALTID_ISSUER_DID", "")
WALTID_ISSUER_KEY_PATH = os.getenv("WALTID_ISSUER_KEY_PATH", "./issuer-secrets/issuer-key.json")
WALTID_ISSUER_API_KEY = os.getenv("WALTID_ISSUER_API_KEY", "")
ISSUER_PUBLIC_URL = normalized_url("ISSUER_PUBLIC_URL", JWT_ISSUER)
BROKER_PUBLIC_URL = normalized_url("BROKER_PUBLIC_URL", "https://broker.localhost")
VERIFIER_PUBLIC_URL = normalized_url("VERIFIER_PUBLIC_URL", "https://verifier.localhost")
WALLET_PUBLIC_URL = normalized_url("WALLET_PUBLIC_URL", "https://wallet.localhost")

from pathlib import Path #used to access key files

ASYM_JWT_ALGORITHM = os.getenv("ASYM_JWT_ALGORITHM", "RS256")

ISSUER_JWT_PRIVATE_KEY = Path(os.getenv("ISSUER_JWT_PRIVATE_KEY_PATH")).read_text()
ISSUER_JWT_PUBLIC_KEY = Path(os.getenv("ISSUER_JWT_PUBLIC_KEY_PATH")).read_text()

#
# BASE MODELS
#
class LoginRequest(BaseModel):
    """Credentials used to authenticate a wallet user before issuing a visa."""

    email: str = Field(..., description="Wallet account email used for authentication.")
    password: str = Field(..., description="Wallet account password.")
    
class IssueVisaRequestTerminal(BaseModel):
    """Requested GA4GH visa type to encode in the issued credential from the terminal."""
    email: str = Field(..., description="Wallet account email used for authentication.")
    password: str = Field(..., description="Wallet account password.")
    type: str = Field(..., description="Visa type to issue, such as red, yellow, blue, or purple.")

class IssueVisaRequest(BaseModel):
    """Requested GA4GH visa type to encode in the issued credential."""

    type: str = Field(..., description="Visa type to issue, such as red, yellow, blue, or purple.")


#
# Web Page Functions
#

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
templates.env.globals.update(
    issuer_public_url=ISSUER_PUBLIC_URL,
    broker_public_url=BROKER_PUBLIC_URL,
    verifier_public_url=VERIFIER_PUBLIC_URL,
    wallet_public_url=WALLET_PUBLIC_URL,
)

#func that allows error pages only to be returned in webpage
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

#
#EXCEPTION HANDLERS
#

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



@app.get("/")
def base_endpoint(request:Request):
    """Redirect users to the dashboard and everyone else to the login page."""
    user = try_get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login")
def login_page(request: Request):
    """Render the issuer login form for browser users."""
    return templates.TemplateResponse(
        "login.html",
        {"request": request}
    )

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    """Authenticate a user and create an issuer session cookie."""
    try:
        user = wallet_client.verify_and_get_user(email, password)
    except WaltIdAuthError:
        raise HTTPException(status_code=401, detail="Invalid credentials") #redirect or return error message
    except WaltIdUnexpectedError as e:
        raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")

    session_user = {
        "email": user["email"],
        "user_id": user["user_id"],
        "auth_source": "wallet",
    }

    token = create_session_token(session_user)

    response = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(response, token)
    return response

@app.get("/register")
def register_page(request: Request):
    """Render the issuer registration form for new wallet users."""
    return templates.TemplateResponse(
        "register.html",
        {"request": request}
    )

@app.post("/register")
def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    """Register a wallet user through walt.id and immediately start an issuer session."""
    try:
        wallet_client.register_user(name, email, password)
        user = wallet_client.verify_and_get_user(email, password)
    except WaltIdAuthError as e:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": str(e),
            },
            status_code=400,
        )
    except WaltIdUnexpectedError as e:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": f"Wallet registration error: {e}",
            },
            status_code=502,
        )
    except Exception as e:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "error": f"Unexpected wallet error: {e}",
            },
            status_code=502,
        )

    session_user = {
        "email": user["email"],
        "user_id": user["user_id"],
        "auth_source": "wallet",
    }

    token = create_session_token(session_user)

    response = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(response, token)
    return response

@app.post("/logout")
def logout():
    """Clear the issuer session cookie and return the user to the login page."""
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response

@app.get("/dashboard") #main functionalities are accessed from this page
def dashboard(request: Request):
    """Render the issuer dashboard."""
    user = try_get_current_user(request)
    if user:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
            }
        )
    return RedirectResponse(url="/login", status_code=303)
    

#
# Visa Creation
#
 
def build_ga4gh_visa_payload(email: str, visa_type: str) -> dict:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=VISA_EXP_HOURS)


    return {
        "iss": JWT_ISSUER,
        "sub": email,
        "aud": JWT_AUDIENCE,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "ga4gh_visa_v1": {
            "type": "ControlledAccessGrants", 
            "asserted": int(now.timestamp()),
            "value": f"https://example.org/datasets/{visa_type}",  
            "source": JWT_ISSUER,
            "by": "co",
        },
    }

@app.post("/api/issue-visa") 
def issue_visa(req: LoginRequest, visa_type: str): 
    """Issue a signed GA4GH Visa JWT after authenticating the provided wallet credentials."""
    try:
        user = wallet_client.verify_and_get_user(req.email, req.password)
    except WaltIdAuthError:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except WaltIdUnexpectedError as e:
        raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")

    
    payload = build_ga4gh_visa_payload(user["email"], visa_type) 
    token = jwt.encode(payload, ISSUER_JWT_PRIVATE_KEY, algorithm=ASYM_JWT_ALGORITHM) 
    
    return JSONResponse( 
        content={ 
            "message": "Visa issued successfully", 
            "user_id": user["user_id"], 
            "visa": token, 
            "payload": payload, 
        } 
    )

@app.post("/ui/issue-visa") 
def ui_issue_visa(request: Request, visa_type: str =  Form(...)): 
    """Issue a signed GA4GH Visa JWT for the logged-in browser user and render the result page."""
    user = try_get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = build_ga4gh_visa_payload(user["email"], visa_type) 
        token = jwt.encode(payload, ISSUER_JWT_PRIVATE_KEY, algorithm=ASYM_JWT_ALGORITHM) 
    except Exception as e:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "error": f"Issuance error: {e}",
            },
            status_code=502,
        )
    response_data = {
        "message": "Visa JWT created successfully",
        "user_id": user["user_id"],
        "email": user["email"],
        "subject": user["email"],
        "token": token,
    }


    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "result": response_data,
        }
    )
    


#                 #
# Walt.id Section #
#                 #


#couple of helpers:
def ts_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

def sign_ga4gh_visa_jwt(payload: dict) -> str:
    return jwt.encode(payload, ISSUER_JWT_PRIVATE_KEY, algorithm=ASYM_JWT_ALGORITHM)

def load_issuer_key() -> dict:
    try:
        with open(WALTID_ISSUER_KEY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Failed to load issuer key from {WALTID_ISSUER_KEY_PATH}: {e}")


def issue_visa_offer(email: str, wallet_user_id: str, visa_type: str) -> dict:
    issuer_key = load_issuer_key()
    
    visa_payload = build_ga4gh_visa_payload(email,visa_type)
    visa_jwt = sign_ga4gh_visa_jwt(visa_payload)

    issued_at = ts_to_iso(visa_payload["iat"])
    not_before = ts_to_iso(visa_payload["nbf"])
    expires_at = ts_to_iso(visa_payload["exp"])

    background_color = "#919191" #default color
    dataset_id = "Default-Dataset"
    vc_id = f"urn:uuid:{uuid.uuid4()}"

    if (visa_type == "red"):
        background_color = "#fc6363"
        dataset_id = "Red-Dataset"
    elif (visa_type == "blue"):
        background_color = "#4157d4"
        dataset_id = "Blue-Dataset"
    elif (visa_type == "yellow"):
        background_color = "#e9c62c"
        dataset_id = "Yellow-Dataset"
    elif (visa_type == "purple"):
        background_color = "#bd33e7"
        dataset_id = "Purple-Dataset"

    headers = {
        "Content-Type": "application/json"
    }

    #not necessary on current build although an Issuer could be configured to require an API Key
    if WALTID_ISSUER_API_KEY:
        headers["Authorization"] = f"Bearer {WALTID_ISSUER_API_KEY}" 

    body = {
        "issuerKey": {
            "jwk": issuer_key,
            "type": "jwk"
        },
        "issuerDid": WALTID_ISSUER_DID,
        "credentialConfigurationId": "Ga4ghVisaCredential_jwt_vc_json",
        "credentialData": {
            "id": vc_id,
            "type": ["VerifiableCredential", "Ga4ghVisaCredential"],
            "issuer": {
                "id": WALTID_ISSUER_DID
            },
            
            "name": f"GA4GH Visa - {dataset_id}",
            "description": "Credential containing a GA4GH Visa JWT for controlled dataset access.",

            "issuanceDate": issued_at,
            "expirationDate": expires_at,
            "validFrom": not_before,
            "validUntil": expires_at,

            "credentialSubject": {
                "User-email": email,
                "walletUserId": wallet_user_id,
                "ga4ghVisaJwt": visa_jwt,
                "ga4ghVisaPayload": visa_payload     
            }, #careful a colon isn't missing here
        },
        "mapping": {
            "display": [
            {
            "backgroundColor": background_color,
            "textColor": "#000000",
            }
  ]
        }
        
    }

    r = requests.post(
        f"{WALTID_ISSUER_URL.rstrip('/')}/openid4vc/jwt/issue",
        json=body,
        headers=headers,
        timeout=20,
    )

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Issuer API error: {r.status_code} {r.text}")

    offer_url = r.text.strip()
    #wiggle room for different offer names
    if not offer_url.startswith("openid-credential-offer://"):
        try:
            data = r.json()
            offer_url = (
                data.get("credentialOffer")
                or data.get("credential_offer")
                or data.get("offerUrl")
                or data.get("offer_url")
                or str(data)
            )
        except Exception:
            pass

    return {
        "offer_url": offer_url
    }


@app.post("/api/issue-visa-for-wallet")
def issue_visa(req: IssueVisaRequestTerminal):
    """Create a wallet credential offer containing a GA4GH visa."""
    try:
        user = wallet_client.verify_and_get_user(req.email, req.password)
    except WaltIdAuthError:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except WaltIdUnexpectedError as e:
        raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")
    type_of_visa = req.type
    try:
        result = issue_visa_offer(
            email=user["email"],
            wallet_user_id=user["user_id"],
            visa_type = type_of_visa
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Issuance error: {e}")

    return JSONResponse(
        content={
            "message": "Visa VC offer created successfully",
            "user_id": user["user_id"],
            "email": user["email"],
            "subject": user["email"],
            "offer_url": result["offer_url"]
        }
    )



@app.post("/ui/issue-visa-for-wallet")
def ui_issue_visa(request: Request, visa_type: str =  Form(...)):
    """Create a browser-visible wallet credential offer for the logged-in user."""
    user = try_get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        result = issue_visa_offer(
            email=user["email"],
            wallet_user_id=user["user_id"],
            visa_type = visa_type
        )
    except Exception as e:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "error": f"Issuance error: {e}",
            },
            status_code=502,
        )
    #general result html so both methods of issuance work on the same page.
    response_data = {
        "message": "Visa VC offer created successfully",
        "user_id": user["user_id"],
        "email": user["email"],
        "subject": user["email"],
        "offer_url": result["offer_url"],
    }


    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "result": response_data,
        }
    )
