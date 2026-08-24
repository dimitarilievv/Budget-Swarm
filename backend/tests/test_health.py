"""Unit test for GET /api/health (BS-014).

The DB connectivity check is patched so the test does not require a running
PostgreSQL instance — it verifies the endpoint contract, not the database.
"""

import api.health as health_module
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_health_ok_when_db_up(monkeypatch):
    monkeypatch.setattr(health_module, "ping", lambda: True)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "up"}


def test_health_reports_db_down(monkeypatch):
    monkeypatch.setattr(health_module, "ping", lambda: False)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "down"}
