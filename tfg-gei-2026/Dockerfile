FROM python:3.12-slim

ARG SERVICE_DIR

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

RUN pip install --upgrade pip && pip install -r requirements.txt

COPY Broker ./Broker
COPY Issuer ./Issuer
COPY Verifier ./Verifier

WORKDIR /app/${SERVICE_DIR}

CMD ["sh", "-c", "uvicorn ${APP_MODULE}:app --host 0.0.0.0 --port ${APP_PORT}"]
