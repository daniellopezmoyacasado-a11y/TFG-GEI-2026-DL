import os
from datetime import datetime, timedelta, timezone

from fastapi import Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse


def register_ui_routes(app, ctx) -> None:
    @app.get("/")
    def root():
        """Redirect the browser landing page to the verifier dashboard."""
        return RedirectResponse(url="/dashboard", status_code=303)

    @app.get("/dashboard")
    def dashboard(request: Request):
        """Render the verifier dashboard with datasets and recent presentation sessions."""
        db = ctx.SessionLocal()
        try:
            datasets = db.query(ctx.GenomicDataset).order_by(ctx.GenomicDataset.id.asc()).all()
            pending_presentations = (
                db.query(ctx.PendingPresentation)
                .order_by(ctx.PendingPresentation.created_at.desc())
                .limit(10)
                .all()
            )
        finally:
            db.close()

        return ctx.templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "datasets": datasets,
                "pending_presentations": pending_presentations,
                "error": None,
            },
        )

    @app.post("/ui/verifier/presentation-request")
    def ui_create_presentation_request(request: Request, dataset_id: int = Form(...)):
        """Create a browser-facing presentation request for accessing a selected dataset."""
        db = ctx.SessionLocal()

        try:
            dataset = db.query(ctx.GenomicDataset).filter(ctx.GenomicDataset.id == dataset_id).first()
            if not dataset:
                raise HTTPException(status_code=404, detail="Dataset not found")

            try:
                result = ctx.create_frontend_ga4gh_visa_presentation_request()
            except Exception as e:
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": f"Could not create presentation request: {e}",
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
                dataset_id=dataset.id,
                request_url=request_url,
                status="pending",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                processed_at=datetime.now(timezone.utc),
            )

            db.add(pending)
            db.commit()

            return ctx.templates.TemplateResponse(
                "result.html",
                {
                    "request": request,
                    "selected_dataset_id": dataset_id,
                    "error": None,
                    "result": {
                        "message": "Presentation request created successfully",
                        "dataset_id": dataset.id,
                        "dataset_name": dataset.name,
                        "presentation_request_url": request_url,
                        "session_id": session_id,
                    },
                },
            )
        finally:
            db.close()

    @app.get("/ui/verifier/presentation-success")
    def presentation_success(id: str):
        """Redirect a successful browser wallet callback to the verifier result page."""
        return RedirectResponse(url=f"/ui/verifier/presentation-result/{id}", status_code=303)

    @app.get("/ui/verifier/presentation-error")
    def ui_presentation_error(request: Request, id: str):
        """Mark a browser presentation session as failed and render an error result."""
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
                "result.html",
                {
                    "request": request,
                    "error_message": "Presentation failed",
                    "session_id": id,
                },
            )
        finally:
            db.close()

    @app.get("/ui/verifier/presentation-result/{session_id}")
    def ui_process_presentation_result(request: Request, session_id: str):
        """Process a browser wallet presentation and grant dataset access when a valid visa matches."""
        db = ctx.SessionLocal()
        try:
            pending = (
                db.query(ctx.PendingPresentation)
                .filter(ctx.PendingPresentation.session_id == session_id)
                .first()
            )
            if not pending:
                raise HTTPException(status_code=404, detail="Presentation session not found")

            dataset = (
                db.query(ctx.GenomicDataset)
                .filter(ctx.GenomicDataset.id == pending.dataset_id)
                .first()
            )
            if not dataset:
                pending.status = "error"
                pending.processed_at = datetime.now(timezone.utc)
                db.commit()
                raise HTTPException(status_code=404, detail="Dataset not found")

            now = datetime.now(timezone.utc)
            expires_at = ctx.ensure_utc(pending.expires_at)

            if pending.status == "processed":
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "Presentation session already processed",
                        "session_id": session_id,
                    },
                )

            if pending.status in ["error", "expired", "denied"]:
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "Presentation session expired or failed",
                        "session_id": session_id,
                    },
                )

            if expires_at < now:
                pending.status = "expired"
                pending.processed_at = now
                db.commit()
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "Presentation session expired or failed",
                        "session_id": session_id,
                    },
                )

            ctx.cleanup_old_finished_pending_presentations(db)

            try:
                presented_credentials = ctx.get_presented_credentials(session_id)
            except Exception as e:
                pending.status = "error"
                pending.processed_at = datetime.now(timezone.utc)
                db.commit()
                raise HTTPException(status_code=502, detail=f"Could not fetch presented credentials: {e}")

            extracted_visa_jwts = ctx.extract_ga4gh_visa_jwts_from_presented_credentials(presented_credentials)

            if not extracted_visa_jwts:
                pending.status = "denied"
                pending.processed_at = datetime.now(timezone.utc)
                db.commit()

                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "No GA4GH visas were presented",
                        "session_id": session_id,
                    },
                    status_code=403,
                )

            verified_visas = []
            access_granted = False

            for idx, visa_jwt in enumerate(extracted_visa_jwts, start=1):
                try:
                    visa_payload = ctx.verify_visa_jwt(visa_jwt)
                    ga4gh_visa = visa_payload.get("ga4gh_visa_v1", {})
                    visa_value = ga4gh_visa.get("value")
                    matches_dataset = visa_value == dataset.required_visa_value

                    if matches_dataset:
                        access_granted = True

                    verified_visas.append(
                        {
                            "index": idx,
                            "valid": True,
                            "value": visa_value,
                            "matches_required_value": matches_dataset,
                            "payload": visa_payload,
                        }
                    )
                except HTTPException as e:
                    verified_visas.append(
                        {
                            "index": idx,
                            "valid": False,
                            "matches_required_value": False,
                            "error": e.detail,
                            "unverified_payload": ctx.decode_unverified(visa_jwt),
                        }
                    )

            pending.status = "processed" if access_granted else "denied"
            pending.processed_at = datetime.now(timezone.utc)
            db.commit()

            if access_granted:
                return ctx.templates.TemplateResponse(
                    "download.html",
                    {
                        "request": request,
                        "session_id": session_id,
                        "dataset_name": dataset.name,
                        "download_url": f"/ui/verifier/download/{session_id}",
                        "required_visa_value": dataset.required_visa_value,
                        "verified_visas": verified_visas,
                    },
                    status_code=200,
                )

            return ctx.templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "error": "No valid GA4GH visas granting access were presented",
                    "session_id": session_id,
                },
                status_code=403,
            )
        finally:
            db.close()

    @app.get("/ui/verifier/download/{session_id}")
    def ui_download_dataset(request: Request, session_id: str):
        """Download the dataset associated with a successful session."""
        db = ctx.SessionLocal()
        try:
            pending = (
                db.query(ctx.PendingPresentation)
                .filter(ctx.PendingPresentation.session_id == session_id)
                .first()
            )
            if not pending:
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "session not found",
                        "session_id": session_id,
                    },
                    status_code=404,
                )

            if pending.status != "processed":
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "Download not available for invalid session",
                        "session_id": session_id,
                    },
                    status_code=403,
                )

            dataset = (
                db.query(ctx.GenomicDataset)
                .filter(ctx.GenomicDataset.id == pending.dataset_id)
                .first()
            )
            if not dataset:
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "Dataset not found",
                        "session_id": session_id,
                    },
                    status_code=404,
                )

            if not os.path.exists(dataset.file_path):
                pending.status = "error"
                pending.processed_at = datetime.now(timezone.utc)
                db.commit()
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "File not found",
                        "session_id": session_id,
                    },
                    status_code=404,
                )

            dataset.num_downloads += 1
            pending.status = "consumed"
            pending.processed_at = datetime.now(timezone.utc)
            db.commit()

            return FileResponse(
                path=dataset.file_path,
                filename=os.path.basename(dataset.file_path),
                media_type="application/octet-stream",
            )
        finally:
            db.close()

    @app.get("/ui/verifier/input-passport/{dataset_id}")
    def ui_input_passport(request: Request, dataset_id: int):
        """Render the form for pasting a GA4GH passport to access a dataset directly."""
        db = ctx.SessionLocal()
        try:
            dataset = db.query(ctx.GenomicDataset).filter(ctx.GenomicDataset.id == dataset_id).first()
            if not dataset:
                raise HTTPException(status_code=404, detail="Dataset not found")

            return ctx.templates.TemplateResponse(
                "inputpassport.html",
                {
                    "request": request,
                    "dataset_id": dataset.id,
                    "dataset_name": dataset.name,
                    "required_visa_value": dataset.required_visa_value,
                },
            )
        finally:
            db.close()

    @app.post("/ui/verifier/input-passport/{dataset_id}")
    def ui_process_passport(request: Request, dataset_id: int, passport: str = Form(...)):
        """Validate a pasted passport and, if authorized, prepare a dataset download session."""
        db = ctx.SessionLocal()
        try:
            dataset = db.query(ctx.GenomicDataset).filter(ctx.GenomicDataset.id == dataset_id).first()
            if not dataset:
                raise HTTPException(status_code=404, detail="Dataset not found")

            try:
                passport_payload = ctx.verify_passport_jwt(passport)
            except HTTPException as e:
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": e.detail,
                    },
                    status_code=e.status_code,
                )

            contained_visas = passport_payload.get("ga4gh_passport_v1", [])
            if not isinstance(contained_visas, list):
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "ga4gh_passport_v1 must be a list of visa JWTs",
                    },
                    status_code=400,
                )

            passport_sub = passport_payload.get("sub")
            verified_visas = []
            access_granted = False

            for idx, visa_jwt in enumerate(contained_visas, start=1):
                subject_matches = False
                try:
                    visa_payload = ctx.verify_visa_jwt(visa_jwt)
                    subject_matches = visa_payload.get("sub") == passport_sub

                    summary = ctx.summarize_visa_payload(visa_payload)
                    visa_value = summary.get("value")
                    matches_dataset = visa_value == dataset.required_visa_value

                    usable_for_access = subject_matches and matches_dataset
                    if usable_for_access:
                        access_granted = True

                    verified_visas.append(
                        {
                            "index": idx,
                            "valid": True,
                            "subject_matches_passport": subject_matches,
                            "value": visa_value,
                            "matches_required_value": matches_dataset,
                            "usable_for_access": usable_for_access,
                            "summary": summary,
                        }
                    )
                except HTTPException as e:
                    verified_visas.append(
                        {
                            "index": idx,
                            "valid": False,
                            "subject_matches_passport": subject_matches,
                            "matches_required_value": False,
                            "usable_for_access": False,
                            "error": e.detail,
                            "unverified_payload": ctx.decode_unverified(visa_jwt),
                        }
                    )

            if not access_granted:
                return ctx.templates.TemplateResponse(
                    "error.html",
                    {
                        "request": request,
                        "error": "No valid GA4GH visas in the passport grant access to this dataset",
                    },
                    status_code=403,
                )

            ctx.cleanup_old_finished_pending_presentations(db)

            session_id = os.urandom(12).hex()
            pending = ctx.PendingPresentation(
                session_id=session_id,
                dataset_id=dataset.id,
                request_url=None,
                status="processed",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                processed_at=datetime.now(timezone.utc),
            )

            db.add(pending)
            db.commit()

            return ctx.templates.TemplateResponse(
                "download.html",
                {
                    "request": request,
                    "session_id": session_id,
                    "dataset_name": dataset.name,
                    "download_url": f"/ui/verifier/download/{session_id}",
                },
                status_code=200,
            )
        finally:
            db.close()
