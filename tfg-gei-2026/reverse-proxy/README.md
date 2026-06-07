# Local HTTPS Proxy

The root `edge-proxy` service exposes friendly local hostnames over HTTPS using
Caddy's internal certificate authority.

Available hostnames:

- `https://issuer.localhost`
- `https://broker.localhost`
- `https://verifier.localhost`
- `https://wallet.localhost`
- `https://demo-wallet.localhost`
- `https://portal.localhost`
- `https://wallet-api.localhost`
- `https://issuer-api.localhost`
- `https://verifier-api.localhost`
- `https://verifier2-api.localhost`
- `https://vc-repo.localhost`

Notes:

- `*.localhost` resolves to your machine automatically in modern browsers, so
  you usually do not need to edit `/etc/hosts`.
- Caddy stores its local CA material under `reverse-proxy/data/`.
- To remove browser certificate warnings, trust
  `reverse-proxy/data/caddy/pki/authorities/local/root.crt` in your OS/browser
  after the proxy starts for the first time.
