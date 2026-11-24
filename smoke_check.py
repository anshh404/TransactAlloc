"""Smoke check for TransactAlloc authentication and live-deadlock endpoints.

Assumes a server is already running at http://127.0.0.1:8000.

    python smoke_check.py

Prints one PASS/FAIL line per check to stdout (plain ASCII) and exits with
status 1 if any check failed. A deadlock trigger that reports ok:false is
printed as SKIP, not FAIL: a busy cluster may not have enough free capacity.
"""

import sys
import time

import httpx

BASE_URL = "http://127.0.0.1:8000"
POLL_INTERVAL = 0.25
POLL_TIMEOUT = 10.0

_results = []
_skipped = 0


def record(name, ok, detail=""):
    ok = bool(ok)
    _results.append(ok)
    line = ("PASS  " if ok else "FAIL  ") + name
    if detail:
        line += "  [" + str(detail).replace("\n", " ")[:160] + "]"
    print(line)
    return ok


def skip(name, detail=""):
    global _skipped
    _skipped += 1
    line = "SKIP  " + name
    if detail:
        line += "  [" + str(detail).replace("\n", " ")[:160] + "]"
    print(line)


def as_dict(response):
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def describe(response):
    data = as_dict(response)
    error = data.get("error") or data.get("detail") or response.text
    return "HTTP " + str(response.status_code) + (": " + str(error) if error else "")


def auth_header(token):
    return {"Authorization": "Bearer " + token} if token else {}


def login(client, username, password, expected_role=None):
    payload = {"username": username, "password": password}
    if expected_role is not None:
        payload["expected_role"] = expected_role
    return client.post("/api/auth/login", json=payload)


def get_state(client, token=None):
    return client.get("/api/state", headers=auth_header(token))


def wait_for_status(client, token, wanted):
    """Poll /api/state until deadlock.status == wanted, or time out.

    Returns the full state dict on success, None on timeout.
    """
    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        response = get_state(client, token)
        snapshot = as_dict(response)
        actual = (snapshot.get("deadlock") or {}).get("status")
        if response.status_code == 200 and actual == wanted:
            return snapshot
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL)


def check_login(client):
    response = login(client, "admin", "admin123", "Admin")
    data = as_dict(response)
    user = data.get("user") or {}
    token = data.get("token")
    ok = (
        response.status_code == 200
        and isinstance(token, str)
        and bool(token)
        and user.get("role") == "Admin"
    )
    record("admin login: 200 with token and role Admin", ok,
           "" if ok else describe(response))
    return token if ok else None


def check_rejections(client):
    response = login(client, "admin", "wrong-password", "Admin")
    record("wrong password rejected with 401",
           response.status_code == 401, describe(response))

    response = login(client, "admin", "admin123", "User")
    record("admin credentials with expected_role User rejected with 401",
           response.status_code == 401, describe(response))


def check_user_scoping(client):
    response = login(client, "Archita", "archita123", "User")
    data = as_dict(response)
    token = data.get("token")
    ok = (
        response.status_code == 200
        and isinstance(token, str)
        and bool(token)
        and (data.get("user") or {}).get("role") == "User"
    )
    record("Archita login: 200 with token and role User", ok,
           "" if ok else describe(response))
    if not ok:
        return

    # Make sure there is at least one of Archita's jobs to prove the scoping.
    client.post(
        "/api/jobs",
        headers=auth_header(token),
        json={
            "user": "admin",  # deliberately wrong: the server must force Archita
            "task": "Scoping_Check",
            "cpus": 1,
            "ram_mb": 1024,
            "priority": 5,
            "duration_sec": 3,
        },
    )
    response = get_state(client, token)
    data = as_dict(response)
    viewer = data.get("viewer") or {}
    record("user state: viewer.is_admin is false",
           response.status_code == 200 and viewer.get("is_admin") is False,
           "" if response.status_code == 200 else describe(response))

    jobs = data.get("jobs") or []
    record("user state: every job belongs to Archita",
           response.status_code == 200 and bool(jobs)
           and all(isinstance(j, dict) and j.get("username") == "Archita"
                   for j in jobs),
           "jobs=" + str(len(jobs)))


def check_admin_viewer(client, admin_token):
    if not admin_token:
        record("admin state: viewer.is_admin is true", False, "no admin token")
        return
    response = get_state(client, admin_token)
    viewer = as_dict(response).get("viewer") or {}
    record("admin state: viewer.is_admin is true",
           response.status_code == 200 and viewer.get("is_admin") is True,
           "" if response.status_code == 200 else describe(response))


def check_idle_deadlock(client, admin_token):
    if not admin_token:
        record("idle state: deadlock present with active false", False,
               "no admin token")
        return
    response = get_state(client, admin_token)
    deadlock = as_dict(response).get("deadlock")
    record("idle state: deadlock present with active false",
           response.status_code == 200
           and isinstance(deadlock, dict)
           and deadlock.get("active") is False,
           "" if response.status_code == 200 else describe(response))


def check_live_deadlock(client, admin_token):
    if not admin_token:
        record("live deadlock scenario", False, "no admin token")
        return

    client.post(
        "/api/engine/start",
        headers=auth_header(admin_token),
        params={"workers": 4, "policy": "fcfs"},
    )
    response = client.post("/api/deadlock/trigger", headers=auth_header(admin_token))
    data = as_dict(response)
    if "ok" in data and not data["ok"]:
        skip("live deadlock scenario", "trigger ok:false: " + str(data.get("error")))
        return
    if response.status_code != 200:
        record("deadlock trigger accepted", False, describe(response))
        return
    record("deadlock trigger accepted", True)

    detected_state = wait_for_status(client, admin_token, "detected")
    if detected_state is None:
        record("deadlock reaches detected status", False,
               "timed out after " + str(int(POLL_TIMEOUT)) + "s")
        return
    deadlock = detected_state.get("deadlock") or {}
    record("deadlock reaches detected status",
           deadlock.get("status") == "detected")

    cycle = deadlock.get("cycle") or []
    record("detected cycle closes and has length >= 3",
           len(cycle) >= 3 and cycle[0] == cycle[-1],
           "cycle=" + str(cycle))

    timeline = deadlock.get("timeline") or []
    record("detected state has a non-empty timeline", bool(timeline),
           "events=" + str(len(timeline)))

    jobs = deadlock.get("jobs") or []
    record("detected state has exactly two jobs", len(jobs) == 2,
           "jobs=" + str(len(jobs)))

    job_ids = [j.get("job_id") for j in jobs if isinstance(j, dict)]

    response = client.post("/api/deadlock/resolve", headers=auth_header(admin_token))
    resolve_data = as_dict(response)
    record("deadlock resolve accepted",
           response.status_code == 200 and resolve_data.get("ok") is True,
           "" if response.status_code == 200 and resolve_data.get("ok")
           else describe(response))

    resolved_state = wait_for_status(client, admin_token, "resolved")
    if resolved_state is None:
        record("deadlock reaches resolved status", False,
               "timed out after " + str(int(POLL_TIMEOUT)) + "s")
        return
    resolved = resolved_state.get("deadlock") or {}
    record("deadlock reaches resolved status",
           resolved.get("status") == "resolved")

    record("victim is one of the two deadlock jobs",
           resolved.get("victim") in job_ids,
           "victim=" + str(resolved.get("victim")) + " jobs=" + str(job_ids))

    timeline_text = " ".join(str(item) for item in (resolved.get("timeline") or [])).lower()
    record("resolved timeline mentions rollback", "rollback" in timeline_text)


def main():
    try:
        with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
            admin_token = check_login(client)
            check_rejections(client)
            check_user_scoping(client)
            check_admin_viewer(client, admin_token)
            check_idle_deadlock(client, admin_token)
            check_live_deadlock(client, admin_token)
    except httpx.HTTPError as exc:
        record("server reachable at " + BASE_URL, False, str(exc))
    except OSError as exc:
        record("server reachable at " + BASE_URL, False, str(exc))

    passed = sum(1 for ok in _results if ok)
    failed = len(_results) - passed
    print("Summary: " + str(passed) + " passed, " + str(failed)
          + " failed, " + str(_skipped) + " skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
