# TFG-GEI-2026

Repository for the Bachelor Thesis (TFG): **Use of Verifiable Credentials in GA4GH Passports**.

This project is a practical implementation of **GA4GH Visa Exchange using verifiable credentials**. It demonstrates how an Issuer, a Broker, and a Verifier can use **walt.id wallets and OpenID4VC-style credential flows** to issue, transfer, store, and verify GA4GH Visas while preserving the GA4GH Passport model.

## Background

### What is a GA4GH Visa?

A **GA4GH Visa** is a signed JWT that carries a `ga4gh_visa_v1` claim. In the GA4GH Passport specification, that claim contains a structured assertion such as the visa `type`, `value`, `source`, and timestamps. In practice, a Visa is the signed authorization statement that says a researcher has some attribute or permission relevant to genomic data access.

Official references:

- GA4GH Passport specification: https://ga4gh.github.io/data-security/ga4gh-passport
- GA4GH Passports product page: https://www.ga4gh.org/product/ga4gh-passports/

### What is a GA4GH Passport?

A **GA4GH Passport** is the container that bundles one or more Visas for downstream authorization. In the GA4GH specifications, the `ga4gh_passport_v1` claim is a list of Visas, and the AAI profile describes a Passport as a **signed and verifiable JWT that contains Visas**. A Passport Clearinghouse or verifier can then evaluate those Visas to decide whether access to data should be granted.

Official references:

- GA4GH Passport specification: https://ga4gh.github.io/data-security/ga4gh-passport
- GA4GH AAI OIDC profile: https://ga4gh.github.io/data-security/aai-openid-connect-profile

### Where verifiable credentials fit in

The GA4GH AAI profile defines the roles of **Visa Issuer**, **Broker**, and **Passport Clearinghouse**, but it also leaves the mechanism used to move Visa data between the assertion sources and the Broker **unspecified**. That gap is exactly what this thesis explores.

This repository implements that missing exchange layer with:

- **walt.id** as wallet and VC infrastructure
- **Verifiable Credentials** as the transport and storage format for exchanged Visas
- **wallet presentation flows** to let the user move credentials between services

The goal is to show that Visa exchange can be implemented in a decentralized way without tightly coupling the Issuer and Broker. The W3C Verifiable Credentials model provides the cryptographically secure, privacy-respecting, machine-verifiable building block used for that exchange:

- W3C Verifiable Credentials Data Model: https://www.w3.org/TR/vc-data-model/all/

## Demo Architecture

At a high level, the repository contains three Python/FastAPI services plus the supporting walt.id stack:

- **Issuer**: decides whether a user should receive a Visa and issues it
- **Broker**: receives Visas, validates and stores them, builds Passports
- **Verifier**: validates Passports or presented Visas and grants dataset access
- **walt.id stack**: wallet frontend, wallet API, issuer API, verifier API, portal, and VC repository
- **Caddy reverse proxy**: exposes friendly local HTTPS hostnames such as `https://issuer.localhost`

A simplified flow is:

1. A user authenticates with the Issuer and requests a Visa.
2. The Issuer creates a GA4GH Visa and offers it as a verifiable credential through walt.id.
3. The user stores that credential in the wallet.
4. The Broker creates a wallet presentation request.
5. The user presents the Visa credential from the wallet to the Broker.
6. The Broker validates the Visa and stores it locally.
7. The Broker generates a GA4GH Passport containing the user’s valid Visas.
8. The Verifier accepts either a wallet presentation or a Passport and grants dataset access if the claims match.

## Installation

### Prerequisites

- Docker and Docker Compose
- Internet access on first startup so Docker can pull base images and any missing `waltid/*` images
- A modern browser for the wallet and local HTTPS services

### Environment configuration

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Review `.env` if you want to change public URLs, secrets, or internal service URLs.

For the default local setup, the checked-in values are already aligned to the Caddy proxy hostnames such as `https://issuer.localhost`, `https://broker.localhost`, and `https://wallet.localhost`.

### Startup

There are two compose stacks:

- `waltid-identity/docker-compose/docker-compose.yaml` starts the vendored walt.id services
- `docker-compose.yml` starts the local Issuer, Broker, Verifier, and edge proxy

The easiest way to start everything is:

```bash
./startup.sh
```

That script runs:

```bash
docker compose -f waltid-identity/docker-compose/docker-compose.yaml up -d
docker compose -f docker-compose.yml up -d --build
```

If you prefer to do it manually, start the walt.id stack first and then the local stack:

```bash
docker compose -f waltid-identity/docker-compose/docker-compose.yaml up -d
docker compose up -d --build
```

### Stop the stack

```bash
docker compose down
docker compose -f waltid-identity/docker-compose/docker-compose.yaml down
```

## Access URLs

The root `edge-proxy` service exposes all local services over HTTPS through Caddy:

- `https://issuer.localhost` — Issuer UI
- `https://broker.localhost` — Broker UI
- `https://verifier.localhost` — Verifier UI
- `https://wallet.localhost` — walt.id wallet frontend
- `https://demo-wallet.localhost` — demo wallet frontend
- `https://portal.localhost` — walt.id portal
- `https://wallet-api.localhost` — wallet API
- `https://issuer-api.localhost` — walt.id issuer API
- `https://verifier-api.localhost` — walt.id verifier API
- `https://verifier2-api.localhost` — secondary verifier API
- `https://vc-repo.localhost` — VC repository

Useful FastAPI documentation pages:

- `https://issuer.localhost/docs`
- `https://broker.localhost/docs`
- `https://verifier.localhost/docs`

## Local HTTPS certificate

The Caddy proxy uses `tls internal`, so your browser will warn about the certificate until you trust Caddy’s local CA.

After the proxy has started at least once, extract the CA certificate with:

```bash
docker compose cp edge-proxy:/data/caddy/pki/authorities/local/root.crt /tmp/caddy-root.crt
```

Then import `/tmp/caddy-root.crt` into your OS trust store or directly into your browser.

For Firefox:

1. Open `Settings`
2. Go to `Privacy & Security`
3. Open `Certificates`
4. Select `View Certificates`
5. Import `/tmp/caddy-root.crt` into the `Authorities` tab
6. Trust it for websites

Notes:

- Modern browsers already resolve `*.localhost` to your own machine, so editing `/etc/hosts` is usually unnecessary.
- Only import `root.crt`. Do not export or trust any private key.

## Project Structure

### Root

- `README.md`: project overview and setup instructions
- `.env.example`: reference configuration for public URLs, JWT keys, and service integration
- `docker-compose.yml`: compose file for the local Issuer, Broker, Verifier, and Caddy edge proxy
- `Dockerfile`: shared Python image build used by the three FastAPI services
- `startup.sh`: convenience script that starts both the vendored walt.id stack and the local stack
- `wallet_client.py`: reusable client for the wallet login, register, and logout flows
- `requirements.txt`: Python dependencies for the local FastAPI services
- `jwt-keys/`: shared issuer and broker signing keys used across services

### `Issuer/`

Implements the Visa issuing service.

- `issuer_app.py`: FastAPI entrypoint and Issuer logic
- `auth/`: session helpers for the browser login flow
- `issuer-secrets/`: walt.id issuer key material
- `jwt-keys/`: issuer signing keys used for GA4GH Visa JWTs
- `templates/`: Jinja templates for login, register, dashboard, result, and error pages
- `static/`: CSS and image assets
- `wallet_client.py`: local copy of the wallet integration helper
- `.env`: service-specific local environment overrides

### `Broker/`

Implements the Passport Broker and the Visa import flow.

- `broker_app.py`: FastAPI entrypoint, shared helpers, DB setup, and route registration
- `models.py`: SQLAlchemy models for broker users, visas, assignments, and pending presentations
- `api_routes.py`: JSON/API endpoints, meant for terminal testing
- `ui_routes.py`: browser-facing routes such as login, register, dashboard, and wallet presentation flow
- `auth/`: session cookie and browser auth helpers
- `jwt-keys/`: broker signing key and issuer public key
- `templates/`: Jinja templates for the Broker UI
- `static/`: CSS and image assets
- `wallet_client.py`: local wallet integration helper
- `visa_broker.db`: local SQLite database used in non-containerized runs
- `.env`: service-specific local environment overrides

### `Verifier/`

Implements dataset access verification using Passports or wallet-presented Visa credentials.

- `verifier_app.py`: FastAPI entrypoint, shared helpers, DB setup, and route registration
- `models.py`: SQLAlchemy models for datasets and pending presentation sessions
- `api_routes.py`: JSON/API endpoints for Visa and Passport verification, meant for terminal testing
- `ui_routes.py`: browser-facing dataset selection, presentation handling, download, and direct passport input flows
- `datasets/`: mock genomic dataset files used in the demo
- `jwt-keys/`: broker and issuer public keys used for validation
- `templates/`: dashboard, result, download, passport input, and error pages
- `static/`: CSS and image assets
- `wallet_client.py`: local wallet integration helper
- `verifier.db`: local SQLite database used in non-containerized runs
- `.env`: service-specific local environment overrides

### `reverse-proxy/`

Contains the root Caddy configuration used to expose all services with friendly local HTTPS hostnames.

- `Caddyfile`: reverse proxy rules for `issuer.localhost`, `broker.localhost`, `verifier.localhost`, and the walt.id services
- `data/`: persisted Caddy CA and runtime data
- `config/`: persisted Caddy config state
- `README.md`: short note about local HTTPS and hostnames

### `waltid-identity/`

Vendored copy of the upstream walt.id identity stack used by this thesis as the VC and wallet layer.

Relevant subareas:

- `docker-compose/`: the compose-based walt.id deployment used by this repository
- `waltid-services/`: wallet API, issuer API, verifier API, and related backend services
- `waltid-applications/`: wallet frontends, portal, and other web apps
- `waltid-libraries/`: shared walt.id libraries and protocol implementations

## Notes

- The local project uses Caddy-hosted HTTPS URLs by default. Browser-facing URLs should therefore use `https://*.localhost`, not the internal container ports.
- Internal container-to-container calls use the in-network Caddy aliases such as `http://caddy:7001/wallet-api`, `http://caddy:7002`, and `http://caddy:7003`.
- The vendored `waltid-identity` copy had its `.git` directory removed so this repository can keep thesis-specific configuration changes without nesting another Git repository.

If you want to see the original walt.id repository, see:

- https://github.com/walt-id/waltid-identity

## Standards and References

- GA4GH Passport specification: https://ga4gh.github.io/data-security/ga4gh-passport
- GA4GH AAI OIDC profile: https://ga4gh.github.io/data-security/aai-openid-connect-profile
- GA4GH Passports product page: https://www.ga4gh.org/product/ga4gh-passports/
- W3C Verifiable Credentials Data Model: https://www.w3.org/TR/vc-data-model/all/
