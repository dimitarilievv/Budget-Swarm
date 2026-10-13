# HTTPS for local dev

The Docker stack serves the app over **HTTPS at `https://localhost`** via a Caddy
reverse proxy (`Caddyfile` + the `proxy` service in `docker-compose.yml`). Caddy
terminates TLS with its **internal (self-signed) CA**, routes `/api/*` to the backend
and everything else to the Vite frontend, and redirects plain HTTP to HTTPS.

This is dev-only (self-signed, `localhost`). Production HTTPS would point a real domain at
Caddy and let it fetch a trusted Let's Encrypt cert — not covered here.

## Run

```bash
cp .env.example .env     # fill in JWT_SECRET etc. (first time only)
docker compose up --build
```

Then open **https://localhost**. The API is at **https://localhost/api** (same origin as
the app, so no CORS in the normal flow).

- `http://localhost` → redirected to `https://localhost`.
- `http://localhost:5173` (Vite directly) and `http://localhost:8000` (backend directly)
  still work, bypassing the proxy — handy for debugging.

## The certificate warning

The cert is issued by Caddy's **local CA**, which your OS/browser doesn't trust yet, so
you'll see a "not secure" warning the first time. Two options:

1. **Just accept it** — click through the warning. Fine for day-to-day dev.
2. **Trust the CA** for a green lock. Copy Caddy's root out of the container and add it to
   your OS trust store:

   ```bash
   docker cp budgetswarm-proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
   ```

   - **Windows:** double-click `caddy-root.crt` → *Install Certificate* → *Local Machine* →
     place in *Trusted Root Certification Authorities*. Restart the browser.
   - **macOS:** `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain caddy-root.crt`
   - **Linux:** copy to `/usr/local/share/ca-certificates/` and run `sudo update-ca-certificates`.

## How it's wired

- `Caddyfile` — `localhost` site, `local_certs`, `/api/*` → `backend:8000`, else → `frontend:5173`.
- `docker-compose.yml` — the `proxy` service publishes `443`/`80`; the frontend gets
  `VITE_API_BASE_URL=https://localhost` (API calls go through the proxy) and
  `VITE_HMR_CLIENT_PORT=443` (Vite's HMR websocket reaches the dev server through Caddy);
  the backend's `CORS_ORIGINS` includes `https://localhost`.
- `vite.config.ts` — sets `server.hmr.clientPort` from `VITE_HMR_CLIENT_PORT` when present.

To bypass the proxy entirely, set `VITE_API_BASE_URL=http://localhost:8000` and use
`http://localhost:5173`.
