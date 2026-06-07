from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class BrokerPassportRequest(BaseModel):
    """Credentials used to authenticate a wallet user before passport issuance."""

    email: str = Field(..., description="Wallet account email used to authenticate the requester.")
    password: str = Field(..., description="Wallet account password.")


class BrokerVisaRequest(BaseModel):
    """Payload used to store a GA4GH Visa JWT for an authenticated wallet user."""

    email: str = Field(..., description="Wallet account email used to authenticate the requester.")
    password: str = Field(..., description="Wallet account password.")
    visa: str = Field(..., description="Signed GA4GH Visa JWT to validate and store.")


class BrokerPresentationRequest(BaseModel):
    """Credentials used to create a wallet presentation request for visa import."""

    email: str = Field(..., description="Wallet account email used to authenticate the requester.")
    password: str = Field(..., description="Wallet account password.")


def register_api_routes(app, ctx) -> None:
    @app.get("/api/users")
    def get_users():
        """List broker users known in the local visa database."""
        db = ctx.SessionLocal()
        try:
            users = db.query(ctx.User).all()
            return {
                "count": len(users),
                "users": [
                    {
                        "id": user.id,
                        "user_wallet_id": user.user_wallet_id,
                    }
                    for user in users
                ],
            }
        finally:
            db.close()

    @app.get("/api/visas")
    def get_visas():
        """List all visas stored in the broker database."""
        db = ctx.SessionLocal()
        try:
            visas = db.query(ctx.Visa).all()
            return {
                "count": len(visas),
                "visas": [
                    {
                        "id": visa.id,
                        "jti": visa.jti,
                        "sub": visa.sub,
                        "type": visa.visa_type,
                        "value": visa.visa_value,
                        "visa_jwt": visa.visa_jwt,
                    }
                    for visa in visas
                ],
            }
        finally:
            db.close()

    @app.get("/api/assignments")
    def get_assignments():
        """List all user-to-visa assignments stored by the broker."""
        db = ctx.SessionLocal()
        try:
            assignments = db.query(ctx.UserVisa).all()
            return {
                "count": len(assignments),
                "assignments": [
                    {
                        "id": assignment.id,
                        "user_id": assignment.user_id,
                        "visa_id": assignment.visa_id,
                        "expires_at": assignment.expires_at.isoformat() if assignment.expires_at else None,
                    }
                    for assignment in assignments
                ],
            }
        finally:
            db.close()

    @app.delete("/api/visas")
    def delete_visas():
        """Delete every stored visa record from the broker database."""
        db = ctx.SessionLocal()
        try:
            deleted = db.query(ctx.Visa).delete()
            db.commit()
            return {
                "deleted_count": deleted,
                "message": "All visas deleted",
            }
        finally:
            db.close()

    @app.delete("/api/assignments")
    def delete_assignments():
        """Delete every user-to-visa assignment from the broker database."""
        db = ctx.SessionLocal()
        try:
            deleted = db.query(ctx.UserVisa).delete()
            db.commit()
            return {
                "deleted_count": deleted,
                "message": "All assignments deleted",
            }
        finally:
            db.close()

    @app.delete("/api/visas/{visa_id}")
    def delete_visa(visa_id: int):
        """Delete a single stored visa by its broker database identifier."""
        db = ctx.SessionLocal()
        try:
            visa = db.query(ctx.Visa).filter(ctx.Visa.id == visa_id).first()
            if not visa:
                raise HTTPException(status_code=404, detail="Visa not found")

            db.delete(visa)
            db.commit()
            return {"message": f"Visa {visa_id} deleted"}
        finally:
            db.close()

    @app.delete("/api/assignments/{assignment_id}")
    def delete_assignment(assignment_id: int):
        """Delete a single user-to-visa assignment by its broker database identifier."""
        db = ctx.SessionLocal()
        try:
            assignment = db.query(ctx.UserVisa).filter(ctx.UserVisa.id == assignment_id).first()
            if not assignment:
                raise HTTPException(status_code=404, detail="Assignment not found")

            db.delete(assignment)
            db.commit()
            return {"message": f"Assignment {assignment_id} deleted"}
        finally:
            db.close()

    @app.get("/api/users/{user_id}/visas")
    def get_user_visas(user_id: int):
        """Return the visas currently assigned to a specific broker user."""
        db = ctx.SessionLocal()
        try:
            user = db.query(ctx.User).filter(ctx.User.id == user_id).first()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            assignments = (
                db.query(ctx.UserVisa, ctx.Visa)
                .join(ctx.Visa, ctx.UserVisa.visa_id == ctx.Visa.id)
                .filter(ctx.UserVisa.user_id == user_id)
                .all()
            )

            now = datetime.now(timezone.utc)
            visas = []
            for assignment, visa in assignments:
                expires_at = assignment.expires_at
                is_expired = False

                if expires_at:
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    is_expired = expires_at < now

                visas.append(
                    {
                        "assignment_id": assignment.id,
                        "visa_id": visa.id,
                        "jti": visa.jti,
                        "sub": visa.sub,
                        "type": visa.visa_type,
                        "value": visa.visa_value,
                        "visa_jwt": visa.visa_jwt,
                        "expires_at": assignment.expires_at.isoformat() if assignment.expires_at else None,
                        "is_expired": is_expired,
                    }
                )

            return {
                "user": {
                    "id": user.id,
                    "email": getattr(user, "email", None),
                    "user_wallet_id": user.user_wallet_id,
                },
                "count": len(visas),
                "visas": visas,
            }
        finally:
            db.close()

    @app.get("/api/pending-presentations")
    def get_pending_presentations():
        """List tracked wallet presentation sessions stored by the broker."""
        db = ctx.SessionLocal()
        try:
            sessions = db.query(ctx.PendingPresentation).all()
            return {
                "count": len(sessions),
                "pending_presentations": [
                    {
                        "id": session.id,
                        "session_id": session.session_id,
                        "user_wallet_id": session.user_wallet_id,
                        "email": session.email,
                        "status": session.status,
                        "request_url": session.request_url,
                        "created_at": session.created_at.isoformat() if session.created_at else None,
                        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
                        "processed_at": session.processed_at.isoformat() if session.processed_at else None,
                    }
                    for session in sessions
                ],
            }
        finally:
            db.close()

    @app.get("/api/issue-passport")
    def issue_passport(req: BrokerPassportRequest):
        """Issue a signed GA4GH passport for an authenticated user with active stored visas."""
        try:
            user = ctx.wallet_client.verify_and_get_user(req.email, req.password)
        except ctx.WaltIdAuthError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except ctx.WaltIdUnexpectedError as e:
            raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")

        db = ctx.SessionLocal()
        try:
            db_user = db.query(ctx.User).filter(ctx.User.user_wallet_id == user["user_id"]).first()
            if not db_user:
                raise HTTPException(status_code=404, detail="Authenticated user not found in local visa DB")

            assignments = (
                db.query(ctx.UserVisa)
                .filter(ctx.UserVisa.user_id == db_user.id)
                .all()
            )
            if not assignments:
                raise HTTPException(status_code=404, detail="User has no visa assignments")

            now = datetime.utcnow()
            valid_assignments = [a for a in assignments if a.expires_at is None or a.expires_at >= now]
            if not valid_assignments:
                raise HTTPException(status_code=404, detail="User has no active visa assignments")

            passport_payload = ctx.build_passport_payload(db_user, valid_assignments, req.email)
            passport_jwt = ctx.jwt.encode(
                passport_payload,
                ctx.BROKER_JWT_PRIVATE_KEY,
                algorithm=ctx.ASYM_JWT_ALGORITHM,
            )

            return {
                "message": "Passport issued successfully",
                "user": {
                    "id": db_user.id,
                    "user_wallet_id": db_user.user_wallet_id,
                },
                "passport": passport_jwt,
                "payload": passport_payload,
                "visa_count": len(passport_payload["ga4gh_passport_v1"]),
            }
        finally:
            db.close()

    @app.post("/api/broker/visa")
    def broker_store_visa(req: BrokerVisaRequest):
        """Validate a GA4GH Visa JWT and store it for the authenticated broker user."""
        try:
            user = ctx.wallet_client.verify_and_get_user(req.email, req.password)
        except ctx.WaltIdAuthError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except ctx.WaltIdUnexpectedError as e:
            raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")

        payload = ctx.decode_and_validate_visa(req.visa)
        if payload.get("sub") != user["user_id"]:
            raise HTTPException(status_code=403, detail="Visa subject does not match authenticated user")

        db = ctx.SessionLocal()
        try:
            user_wallet_id = user["user_id"]

            db_user = db.query(ctx.User).filter(ctx.User.user_wallet_id == user_wallet_id).first()
            if not db_user:
                db_user = ctx.User(user_wallet_id=user_wallet_id)
                db.add(db_user)
                db.commit()
                db.refresh(db_user)

            jti = payload.get("jti")
            sub = payload.get("sub")
            ga4gh = payload.get("ga4gh_visa_v1", {})
            exp = payload.get("exp")
            expires_at = datetime.fromtimestamp(exp, tz=timezone.utc) if exp is not None else None

            visa_type = ga4gh.get("type")
            visa_value = ga4gh.get("value")
            if not jti:
                raise HTTPException(status_code=400, detail="Visa token has no jti")

            db_visa = db.query(ctx.Visa).filter(ctx.Visa.jti == jti).first()
            created_visa = False
            if not db_visa:
                db_visa = ctx.Visa(
                    jti=jti,
                    visa_jwt=req.visa,
                    sub=sub,
                    visa_type=visa_type,
                    visa_value=visa_value,
                )
                db.add(db_visa)
                db.commit()
                db.refresh(db_visa)
                created_visa = True

            existing_assignment = (
                db.query(ctx.UserVisa)
                .filter(ctx.UserVisa.user_id == db_user.id, ctx.UserVisa.visa_id == db_visa.id)
                .first()
            )

            assigned = False
            if not existing_assignment:
                assignment = ctx.UserVisa(user_id=db_user.id, visa_id=db_visa.id, expires_at=expires_at)
                db.add(assignment)
                db.commit()
                assigned = True

            return {
                "message": "Visa sync completed",
                "user": {
                    "id": db_user.id,
                    "user_wallet_id": db_user.user_wallet_id,
                },
                "visa": {
                    "id": db_visa.id,
                    "jti": db_visa.jti,
                    "sub": db_visa.sub,
                    "type": db_visa.visa_type,
                    "value": db_visa.visa_value,
                },
                "created_visa": created_visa,
                "assigned": assigned,
            }
        finally:
            db.close()

    @app.post("/api/broker/presentation-request")
    def create_presentation_request(req: BrokerPresentationRequest):
        """Create an OpenID4VP presentation request URL for importing visas from a wallet."""
        try:
            user = ctx.wallet_client.verify_and_get_user(req.email, req.password)
        except ctx.WaltIdAuthError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except ctx.WaltIdUnexpectedError as e:
            raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")

        db = ctx.SessionLocal()
        try:
            db_user = db.query(ctx.User).filter(ctx.User.user_wallet_id == user["user_id"]).first()
            if not db_user:
                db_user = ctx.User(user_wallet_id=user["user_id"])
                db.add(db_user)
                db.commit()
                db.refresh(db_user)

            result = ctx.create_ga4gh_visa_presentation_request()
            request_url = result["request_url"]
            session_id = ctx.extract_session_id_from_request_url(request_url)

            return JSONResponse(
                content={
                    "message": "Presentation request created successfully",
                    "user_id": user["user_id"],
                    "email": user["email"],
                    "presentation_request_url": request_url,
                    "session_id": session_id,
                }
            )
        finally:
            db.close()

    @app.get("/broker/presentation-result/{session_id}")
    def process_presentation_result(session_id: str, user_id: str):
        """Fetch a completed presentation session and persist any presented GA4GH visas."""
        db = ctx.SessionLocal()
        try:
            db_user = db.query(ctx.User).filter(ctx.User.user_wallet_id == user_id).first()
            if not db_user:
                raise HTTPException(status_code=404, detail="User not found in broker DB")

            try:
                presented_credentials = ctx.get_presented_credentials(session_id)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Could not fetch presented credentials: {e}")

            stored = []
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
                    visa_jwt = credential_subject.get("ga4ghVisaJwt")
                    if not visa_jwt:
                        continue

                    stored.append(ctx.store_visa_jwt_for_user(db, db_user, visa_jwt))

            return {
                "message": "Presentation processed",
                "session_id": session_id,
                "stored_visa_count": len(stored),
                "stored_visas": stored,
            }
        finally:
            db.close()

    @app.get("/broker/presentation-success")
    def presentation_success(id: str):
        """Return a simple success payload for non-UI presentation callbacks."""
        return {
            "message": "Presentation submitted successfully",
            "session_id": id,
        }

    @app.get("/broker/presentation-error")
    def presentation_error(id: str):
        """Return a simple error payload for non-UI presentation callbacks."""
        return {
            "message": "Presentation failed",
            "session_id": id,
        }
