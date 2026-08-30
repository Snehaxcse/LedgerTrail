# LedgerTrail web

React + Tailwind UI over the existing FastAPI app. The frontend never computes
reconciliation — it displays `is_reconciled`, bridge totals, and variance as
returned by the API.

## Run

In the repo root:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In `frontend/`:

```bash
npm install
npm run dev
```

Open http://localhost:5173/. Vite proxies `/api/*` to the FastAPI server.

## Production / Vercel

Set `VITE_API_BASE_URL` to the deployed FastAPI origin (no trailing slash), e.g.
`https://your-api.example.com`. In Vercel, add it under Project → Settings →
Environment Variables (Production). The app defaults to `/api` when the variable
is unset, which is the local Vite proxy.

Point the Vercel project Root Directory at `frontend`.
