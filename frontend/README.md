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
