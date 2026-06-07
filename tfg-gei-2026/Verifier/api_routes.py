from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field


class VerifyPassportRequest(BaseModel):
    """Passport verification payload."""

    passport: str = Field(..., description="Signed GA4GH passport JWT to validate.")


class VerifyVisaRequest(BaseModel):
    """Visa verification payload."""

    visa: str = Field(..., description="Signed GA4GH Visa JWT to validate.")


class VerifierPresentationRequest(BaseModel):
    """Credentials used to create a non-UI wallet presentation request."""

    email: str = Field(..., description="Wallet account email used to authenticate the requester.")
    password: str = Field(..., description="Wallet account password.")


def register_api_routes(app, ctx) -> None:
    @app.get("/health")
    def health():
        """Return a simple health status for container and service checks."""
        return {"status": "ok"}

    @app.post("/api/verify-visa")
    def verify_visa(request: VerifyVisaRequest):
        """Validate a GA4GH Visa JWT and return both full and summarized claims."""
        payload = ctx.verify_visa_jwt(request.visa)

        return {
            "valid": True,
            "message": "Visa is valid",
            "payload": payload,
            "summary": ctx.summarize_visa_payload(payload),
        }

    @app.post("/api/verify-passport")
    def verify_passport(request: VerifyPassportRequest):
        """Validate a GA4GH passport and verify each contained GA4GH Visa JWT."""
        passport_payload = ctx.verify_passport_jwt(request.passport)

        contained_visas = passport_payload.get("ga4gh_passport_v1", [])
        if not isinstance(contained_visas, list):
            raise HTTPException(status_code=400, detail="ga4gh_passport_v1 must be a list of visa JWTs")

        passport_sub = passport_payload.get("sub")
        visa_results: list[dict[str, Any]] = []
        all_visas_valid = True
        all_subjects_match = True

        for idx, visa_jwt in enumerate(contained_visas, start=1):
            subject_matches = False
            try:
                visa_payload = ctx.verify_visa_jwt(visa_jwt)
                subject_matches = visa_payload.get("sub") == passport_sub

                if not subject_matches:
                    all_subjects_match = False

                visa_results.append(
                    {
                        "index": idx,
                        "valid": subject_matches,
                        "subject_matches_passport": subject_matches,
                        "summary": ctx.summarize_visa_payload(visa_payload),
                        "payload": visa_payload,
                    }
                )
            except HTTPException as e:
                all_visas_valid = False
                visa_results.append(
                    {
                        "index": idx,
                        "valid": False,
                        "subject_matches_passport": subject_matches,
                        "error": e.detail,
                        "unverified_payload": ctx.decode_unverified(visa_jwt),
                    }
                )

        contained_labels = passport_payload.get("contained_visas", [])

        return {
            "valid": all_visas_valid,
            "message": (
                "Passport verified"
                if all_subjects_match
                else "Passport verified, but one or more contained visas are invalid"
            ),
            "passport": {
                "sub": passport_payload.get("sub"),
                "iss": passport_payload.get("iss"),
                "iat": passport_payload.get("iat"),
                "exp": passport_payload.get("exp"),
                "contained_visas": contained_labels,
                "visa_count": len(contained_visas),
            },
            "visas": visa_results,
        }

    @app.post("/verifier/presentation-request")
    def create_presentation_request(req: VerifierPresentationRequest):
        """Create a non-UI wallet presentation request for submitting GA4GH visas."""
        try:
            user = ctx.wallet_client.verify_and_get_user(req.email, req.password)
        except ctx.WaltIdAuthError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except ctx.WaltIdUnexpectedError as e:
            raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")

        try:
            result = ctx.create_ga4gh_visa_presentation_request()
            request_url = result["request_url"]
            session_id = ctx.extract_session_id_from_request_url(request_url)

            return {
                "message": "Presentation request created successfully",
                "user_id": user["user_id"],
                "email": user["email"],
                "presentation_request_url": request_url,
                "session_id": session_id,
            }
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not create presentation request: {e}")

    @app.get("/verifier/presentation-result/{session_id}")
    def process_presentation_result(session_id: str):
        """Fetch a completed non-UI presentation session and verify the submitted visas."""
        try:
            presented_credentials = ctx.get_presented_credentials(session_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Could not fetch presented credentials: {e}")

        extracted_visa_jwts = ctx.extract_ga4gh_visa_jwts_from_presented_credentials(presented_credentials)

        verified_visas: list[dict[str, Any]] = []
        all_visas_valid = True

        for idx, visa_jwt in enumerate(extracted_visa_jwts, start=1):
            try:
                payload = ctx.verify_visa_jwt(visa_jwt)
                verified_visas.append(
                    {
                        "index": idx,
                        "valid": True,
                        "summary": ctx.summarize_visa_payload(payload),
                        "payload": payload,
                    }
                )
            except HTTPException as e:
                all_visas_valid = False
                verified_visas.append(
                    {
                        "index": idx,
                        "valid": False,
                        "error": e.detail,
                        "unverified_payload": ctx.decode_unverified(visa_jwt),
                    }
                )

        return {
            "message": "Presentation processed",
            "session_id": session_id,
            "valid": all_visas_valid,
            "presented_visa_count": len(extracted_visa_jwts),
            "verified_visas": verified_visas,
        }

    @app.get("/verifier/presentation-success")
    def presentation_success(id: str):
        """Return a simple success payload for non-UI wallet presentation callbacks."""
        return {
            "message": "Presentation submitted successfully",
            "session_id": id,
        }

    @app.get("/verifier/presentation-error")
    def presentation_error(id: str):
        """Return a simple error payload for non-UI wallet presentation callbacks."""
        return {
            "message": "Presentation failed",
            "session_id": id,
        }

    @app.get("/datasets")
    def get_datasets():
        """List the demo genomic datasets exposed by the verifier."""
        db = ctx.SessionLocal()
        try:
            datasets = db.query(ctx.GenomicDataset).order_by(ctx.GenomicDataset.id.asc()).all()
            return {
                "count": len(datasets),
                "datasets": [
                    {
                        "id": ds.id,
                        "name": ds.name,
                        "file_path": ds.file_path,
                        "file_size": ds.file_size,
                        "num_downloads": ds.num_downloads,
                        "description": ds.description,
                        "required_visa_value": ds.required_visa_value,
                    }
                    for ds in datasets
                ],
            }
        finally:
            db.close()
