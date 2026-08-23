"""BudgetSwarm API — BS-013 boot skeleton.
Minimal app so the container starts and compose comes up.
BS-014 replaces this with the real app (CORS, GET /api/health, DB status).
"""
from fastapi import FastAPI

app = FastAPI(title="BudgetSwarm API (skeleton)")


@app.get("/")
def root():
    return {"service": "budgetswarm-backend", "status": "up"}
