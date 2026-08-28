from fastapi import FastAPI

app = FastAPI(title="LedgerTrail")


@app.get("/health")
def health():
    return {"status": "ok"}
