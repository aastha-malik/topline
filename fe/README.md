# Topline frontend

Responsive React control panel for the existing FastAPI service.

## Run locally

```bash
npm install
npm run dev
```

The Vite server runs at `http://localhost:5173` and proxies `/api` to the backend at `http://localhost:8000`. Set `VITE_API_PROXY_TARGET` when the backend runs elsewhere; keep `VITE_API_BASE_URL=/api/v1` so browser requests stay same-origin and the owner session cookie is first-party.

Every screen but `/signin` requires a signed-in owner. Sign in with "Continue with Google" — the backend's OAuth callback sets the session cookie. In production a Vercel rewrite (`vercel.json`) proxies `/api` to the backend so the cookie stays first-party there too; set its `destination` to the deployed backend URL.

For an explicit frontend-only preview using illustrative records (no backend, no sign-in):

```bash
VITE_DEMO_MODE=true npm run dev
```

Demo values are visibly labelled in the interface. Live mode is the default.

## Environment

Copy `.env.example` to `.env.local` only when the defaults do not fit. Never place Supabase service-role keys, Google client secrets, Razorpay secrets, Gemini keys, or database credentials in a Vite environment variable; every `VITE_*` value is visible to the browser.

## Checks

```bash
npm run lint
npm test
npm run build
```
