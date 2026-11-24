"""The live deadlock graph is cluster-wide, not owner-scoped.

Regression: while a two-phase deadlock session is active, /api/state reported
``deadlock.status = "detected"`` and a cycle to every viewer but filtered
``wfg.edges`` to the signed-in user's own jobs. Because the scenario's jobs
belong to the operator, a regular user saw "cycle present" over an empty SVG.
"""

import time

from fastapi.testclient import TestClient

import app as app_module


def _login(client, username, password, role):
    r = client.post("/api/auth/login", json={
        "username": username, "password": password, "expected_role": role,
    })
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_live_deadlock_edges_visible_to_regular_user():
    with TestClient(app_module.app) as client:
        client.post("/api/engine/stop")
        client.post("/api/reset")
        admin = _login(client, "admin", "admin123", "Admin")
        headers = {"Authorization": "Bearer " + admin}

        started = client.post(
            "/api/engine/start", headers=headers,
            params={"workers": 4, "policy": "fcfs"},
        )
        assert started.status_code == 200, started.text

        triggered = client.post("/api/deadlock/trigger", headers=headers)
        assert triggered.status_code == 200 and triggered.json().get("ok"), triggered.text

        user_token = _login(client, "Archita", "archita123", "User")
        user_headers = {"Authorization": "Bearer " + user_token}

        state = None
        deadline = time.time() + 12
        while time.time() < deadline:
            state = client.get("/api/state", headers=user_headers).json()
            if state["deadlock"]["status"] == "detected":
                break
            time.sleep(0.3)

        assert state is not None
        assert state["deadlock"]["status"] == "detected", state["deadlock"]["status"]
        edges = {tuple(e) for e in state["wfg"]["edges"]}
        cycle = state["deadlock"]["cycle"]
        assert len(cycle) >= 3 and cycle[0] == cycle[-1], cycle
        for waiter, holder in zip(cycle, cycle[1:]):
            assert (waiter, holder) in edges, f"missing edge {waiter}->{holder}: {edges}"

        client.post("/api/deadlock/resolve", headers=headers)
        client.post("/api/engine/stop", headers=headers)
        client.post("/api/reset", headers=headers)
