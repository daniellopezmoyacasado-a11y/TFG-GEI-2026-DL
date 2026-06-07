from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Form, Request
from fastapi.responses import RedirectResponse


def register_ui_routes(app, ctx) -> None:
    @app.get("/")
    def base_endpoint(request: Request):
        """Redirect authenticated browser users to the dashboard and everyone else to login."""
        user = ctx.try_get_current_user(request)
        if user:
            return RedirectResponse(url="/dashboard", status_code=303)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/login")
    def login_page(request: Request):
        """Render the broker login page for browser users."""
        return ctx.templates.TemplateResponse(
            "login.html",
            {"request": request},
        )

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        """Authenticate a wallet user and create a broker browser session."""
        try:
            user = ctx.wallet_client.verify_and_get_user(email, password)
        except ctx.WaltIdAuthError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except ctx.WaltIdUnexpectedError as e:
            raise HTTPException(status_code=502, detail=f"Wallet auth error: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Unexpected wallet error: {e}")

        session_user = {
            "email": user["email"],
            "user_id": user["user_id"],
            "auth_source": "wallet",
        }
        token = ctx.create_session_token(session_user)

        response = RedirectResponse(url="/dashboard", status_code=303)
        ctx.set_session_cookie(response, token)
        return response

    @app.get("/register")
    def register_page(request: Request):
        """Render the broker registration page for new wallet users."""
        return ctx.templates.TemplateResponse(
            "register.html",
            {"request": request},
        )

    @app.post("/register")
    def register(
        request: Request,
        name: str = Form(...),
        email: str = Form(...),
        password: str = Form(...),
    ):
        """Register a wallet user and create the matching local broker user record if needed."""
        db = ctx.SessionLocal()
        try:
            ctx.wallet_client.register_user(name, email, password)
            user = ctx.wallet_client.verify_and_get_user(email, password)

            existing_user = (
                db.query(ctx.User)
                .filter(ctx.User.user_wallet_id == user["user_id"])
                .first()
            )
            if existing_user:
                return ctx.templates.TemplateResponse(
                    "register.html",
                    {
                        "request": request,
                        "error": "This wallet user is already registered in the broker database.",
                    },
                    status_code=409,
                )

            db_user = ctx.User(user_wallet_id=user["user_id"])
            db.add(db_user)
            db.commit()

            session_user = {
                "email": user["email"],
                "user_id": user["user_id"],
                "auth_source": "wallet",
            }
            token = ctx.create_session_token(session_user)

            response = RedirectResponse(url="/dashboard", status_code=303)
            ctx.set_session_cookie(response, token)
            return response
        except ctx.WaltIdAuthError as e:
            return ctx.templates.TemplateResponse(
                "register.html",
                {
                    "request": request,
                    "error": str(e),
                },
                status_code=400,
            )
        except ctx.WaltIdUnexpectedError as e:
            return ctx.templates.TemplateResponse(
                "register.html",
                {
                    "request": request,
                    "error": f"Wallet registration error: {e}",
                },
                status_code=502,
            )
        except Exception as e:
            db.rollback()
            return ctx.templates.TemplateResponse(
                "register.html",
                {
                    "request": request,
                    "error": f"Unexpected wallet error: {e}",
                },
                status_code=502,
            )
        finally:
            db.close()

    @app.post("/logout")
    def logout():
        """Clear the broker session cookie and redirect to the login page."""
        response = RedirectResponse(url="/login", status_code=303)
        ctx.clear_session_cookie(response)
        return response

    @app.get("/dashboard")
    def dashboard(request: Request):
        """Render the broker dashboard with the logged-in user's current visa assignments."""
        db = ctx.SessionLocal()
        try:
            session_user = ctx.try_get_current_user(request)
            if not session_user:
                return RedirectResponse(url="/login", status_code=303)

            db_user = (
                db.query(ctx.User)
                .filter(ctx.User.user_wallet_id == session_user["user_id"])
                .first()
            )
            if not db_user:
                db_user = ctx.User(user_wallet_id=session_user["user_id"])
                db.add(db_user)
                db.commit()
                db.refresh(db_user)

            assignments = (
                db.query(ctx.UserVisa, ctx.Visa)
                .join(ctx.Visa, ctx.UserVisa.visa_id == ctx.Visa.id)
                .filter(ctx.UserVisa.user_id == db_user.id)
                .all()
            )

            now = datetime.now(timezone.utc)
            user_visas = []
            for assignment, visa in assignments:
                expires_at = assignment.expires_at
                is_expired = False

                if expires_at:
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    is_expired = expires_at < now

                user_visas.append(
                    {
                        "assignment_id": assignment.id,
                        "visa_id": visa.id,
                        "jti": visa.jti,
                        "sub": visa.sub,
                        "type": visa.visa_type,
                        "value": visa.visa_value,
                        "expires_at": assignment.expires_at,
                        "is_expired": is_expired,
                    }
                )

            return ctx.templates.TemplateResponse(
                "dashboard.html",
                {
                    "request": request,
                    "user": session_user,
                    "user_visas": user_visas,
                    "user_id": db_user.id,
                    "error": None,
                },
            )
        finally:
            db.close()

    @app.get("/input-visa")
    def input_visa(request: Request):
        """Render the manual visa submission page for the authenticated browser user."""
        session_user = ctx.try_get_current_user(request)
        if not session_user:
            return RedirectResponse(url="/login", status_code=303)
        return ctx.templates.TemplateResponse(
            "inputvisa.html",
            {"request": request},
        )

    @app.post("/ui/assignments/{assignment_id}/delete")
    def delete_user_assignment_from_dashboard(assignment_id: int):
        """Remove an assigned visa from the authenticated browser user's dashboard."""
        db = ctx.SessionLocal()
        try:
            assignment = db.query(ctx.UserVisa).filter(ctx.UserVisa.id == assignment_id).first()
            if not assignment:
                raise HTTPException(status_code=404, detail="Assignment not found")

            visa_id = assignment.visa_id
            db.delete(assignment)
            db.commit()

            remaining_links = db.query(ctx.UserVisa).filter(ctx.UserVisa.visa_id == visa_id).count()
            if remaining_links == 0:
                visa = db.query(ctx.Visa).filter(ctx.Visa.id == visa_id).first()
                if visa:
                    db.delete(visa)
                    db.commit()

            return RedirectResponse(url="/dashboard", status_code=303)
        finally:
            db.close()

    @app.post("/ui/broker/presentation-request")
    def ui_create_presentation_request(request: Request):
        """Create a browser-facing presentation request and store it as a pending broker session."""
        user = ctx.try_get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=303)

        db = ctx.SessionLocal()
        try:
            db_user = db.query(ctx.User).filter(ctx.User.user_wallet_id == user["user_id"]).first()
            if not db_user:
                db_user = ctx.User(user_wallet_id=user["user_id"])
                db.add(db_user)
                db.commit()
                db.refresh(db_user)

            try:
                result = ctx.create_frontend_ga4gh_visa_presentation_request()
            except Exception as e:
                return ctx.templates.TemplateResponse(
                    "result.html",
                    {
                        "request": request,
                        "error": f"Could not create presentation request: {e}",
                        "result": None,
                    },
                    status_code=502,
                )

            request_url = result["request_url"]
            session_id = ctx.extract_session_id_from_request_url(request_url)
            if not session_id:
                raise HTTPException(status_code=502, detail="Could not extract session_id from verifier request URL")

            ctx.cleanup_old_finished_pending_presentations(db)

            pending = ctx.PendingPresentation(
                session_id=session_id,
                user_wallet_id=user["user_id"],
                email=user["email"],
                request_url=request_url,
                status="pending",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            )
            db.add(pending)
            db.commit()

            return ctx.templates.TemplateResponse(
                "result.html",
                {
                    "request": request,
                    "error": None,
                    "result": {
                        "message": "Presentation request created successfully",
                        "user_id": user["user_id"],
                        "email": user["email"],
                        "presentation_request_url": request_url,
                        "session_id": session_id,
                    },
                },
            )
        finally:
            db.close()

    @app.get("/ui/broker/presentation-success")
    def ui_presentation_success(id: str):
        """Redirect a successful browser wallet callback to the broker presentation result page."""
        return RedirectResponse(url=f"/ui/broker/presentation-result/{id}", status_code=303)

    @app.get("/ui/broker/presentation-error")
    def ui_presentation_error(request: Request, id: str):
        """Mark a browser presentation session as failed and render the broker error page."""
        db = ctx.SessionLocal()
        try:
            pending = None
            if id:
                pending = (
                    db.query(ctx.PendingPresentation)
                    .filter(ctx.PendingPresentation.session_id == id)
                    .first()
                )
                if pending and pending.status == "pending":
                    pending.status = "error"
                    db.commit()

            ctx.cleanup_old_finished_pending_presentations(db)

            return ctx.templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "error": "Presentation failed",
                    "session_id": id,
                },
            )
        finally:
            db.close()

    @app.get("/ui/broker/presentation-result/{session_id}")
    def ui_process_presentation_result(session_id: str):
        """Process a browser wallet presentation callback and store any received visas."""
        db = ctx.SessionLocal()
        try:
            pending = (
                db.query(ctx.PendingPresentation)
                .filter(ctx.PendingPresentation.session_id == session_id)
                .first()
            )
            if not pending:
                raise HTTPException(status_code=404, detail="Unknown presentation session")

            now = datetime.now(timezone.utc)
            expires_at = ctx.ensure_utc(pending.expires_at)

            if pending.status == "processed":
                return RedirectResponse(url="/dashboard", status_code=303)
            if pending.status in ["error", "expired"]:
                raise HTTPException(status_code=410, detail="Presentation session expired")
            if expires_at < now:
                pending.status = "expired"
                db.commit()
                raise HTTPException(status_code=410, detail="Presentation session expired")

            ctx.cleanup_old_finished_pending_presentations(db)

            db_user = db.query(ctx.User).filter(ctx.User.user_wallet_id == pending.user_wallet_id).first()
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

            return RedirectResponse(url="/dashboard", status_code=303)
        finally:
            db.close()

    @app.post("/ui/broker/visa")
    def ui_broker_store_visa(request: Request, visa: str = Form(...)):
        """Validate and store a manually submitted GA4GH Visa JWT from the broker UI."""
        user = ctx.try_get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=303)

        payload = ctx.decode_and_validate_visa(visa)
        if payload.get("sub") != user["email"]:
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
            if not db_visa:
                db_visa = ctx.Visa(
                    jti=jti,
                    visa_jwt=visa,
                    sub=sub,
                    visa_type=visa_type,
                    visa_value=visa_value,
                )
                db.add(db_visa)
                db.commit()
                db.refresh(db_visa)

            existing_assignment = (
                db.query(ctx.UserVisa)
                .filter(ctx.UserVisa.user_id == db_user.id, ctx.UserVisa.visa_id == db_visa.id)
                .first()
            )
            if not existing_assignment:
                assignment = ctx.UserVisa(user_id=db_user.id, visa_id=db_visa.id, expires_at=expires_at)
                db.add(assignment)
                db.commit()

            return RedirectResponse(url="/dashboard", status_code=303)
        finally:
            db.close()

    @app.post("/ui/issue-passport")
    def ui_issue_passport(request: Request):
        """Issue a GA4GH passport for the logged-in browser user and render the result page."""
        user = ctx.try_get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=303)

        db = ctx.SessionLocal()
        try:
            db_user = db.query(ctx.User).filter(ctx.User.user_wallet_id == user["user_id"]).first()
            if not db_user:
                return ctx.templates.TemplateResponse(
                    "result.html",
                    {
                        "request": request,
                        "error": "Authenticated user not found in local visa DB",
                    },
                )

            assignments = (
                db.query(ctx.UserVisa)
                .filter(ctx.UserVisa.user_id == db_user.id)
                .all()
            )
            if not assignments:
                return ctx.templates.TemplateResponse(
                    "result.html",
                    {
                        "request": request,
                        "error": "User has no visa assignments",
                    },
                )

            now = datetime.now(timezone.utc)
            valid_assignments = [
                a
                for a in assignments
                if ctx.ensure_utc(a.expires_at) is None or ctx.ensure_utc(a.expires_at) >= now
            ]
            if not valid_assignments:
                return ctx.templates.TemplateResponse(
                    "result.html",
                    {
                        "request": request,
                        "error": "User has no active visa assignments",
                    },
                )

            user_email = user.get("email")
            passport_payload = ctx.build_passport_payload(db_user, valid_assignments, user_email)
            passport_jwt = ctx.jwt.encode(
                passport_payload,
                ctx.BROKER_JWT_PRIVATE_KEY,
                algorithm=ctx.ASYM_JWT_ALGORITHM,
            )

            result = {
                "passport_issued": True,
                "message": "Passport issued successfully",
                "user_id": db_user.id,
                "user_wallet_id": db_user.user_wallet_id,
                "passport": passport_jwt,
                "payload": passport_payload,
                "visa_count": len(passport_payload["ga4gh_passport_v1"]),
            }

            return ctx.templates.TemplateResponse(
                "result.html",
                {
                    "request": request,
                    "result": result,
                },
            )
        finally:
            db.close()
