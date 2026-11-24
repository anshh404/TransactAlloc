"""FastAPI routes for TransactAlloc.

Every read opens its own connection with dict rows so the payload can be
serialised straight to JSON; writes stay explicit and transactional.
"""

import random
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

import auth
import db
import defrag as defrag_mod
import engine as engine_mod
import wfg as wfg_mod

router = APIRouter()
_engine: Optional[engine_mod.Engine] = None

TASKS = ("Matrix_Mult", "Monte_Carlo", "Image_Filter", "PageRank",
         "KMeans_Fit", "FFT_Batch", "Ray_Trace", "Nbody_Sim")


class SubmitJob(BaseModel):
    user: str = "Archita"
    task: str = "Task"
    cpus: int = Field(ge=1)
    ram_mb: int = Field(ge=1)
    priority: int = Field(ge=1, le=10, default=5)
    duration_sec: int = Field(ge=1, default=6)


class Batch(BaseModel):
    count: int = Field(ge=1, le=500, default=20)
    seed: int = 7


class Policy(BaseModel):
    mode: str = Field(pattern=r"^(auto|fcfs|priority|round-robin)$")


class DefragReq(BaseModel):
    cluster: int | str = "all"


class LoginBody(BaseModel):
    username: str
    password: str
    expected_role: str = Field(pattern=r"^(Admin|User)$")


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)
    role: str = Field(pattern=r"^(Admin|User)$")
    quota_cpus: int = Field(ge=0, default=0)
    quota_ram_mb: int = Field(ge=0, default=0)


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


def _dict_conn(autocommit=False):
    return db.connect(autocommit=autocommit, row_factory=dict_row)


def get_engine() -> engine_mod.Engine:
    if _engine is None:
        raise HTTPException(400, "engine not started")
    return _engine


def _wfg_state(eng):
    return {
        "edges": eng.wfg.edges_as_list() if eng else [],
        "edge_details": eng.wfg.edge_details() if eng else [],
    }


def _demo_graph_state(graph, cycle):
    edges = graph.edges_as_list()
    nodes = sorted({node for edge in edges for node in edge})
    return {
        "nodes": nodes,
        "edges": edges,
        "edge_details": graph.edge_details(),
        "cycle": list(cycle or []),
    }


def _viewer(request: Request) -> dict:
    return auth.viewer(request)


def _scoped_username(who: dict) -> str | None:
    """Only regular users are scoped; operators and legacy callers see all."""
    if who.get("authenticated") and not auth.is_admin(who):
        return who.get("username")
    return None


def _viewer_payload(who: dict) -> dict:
    return {
        "username": who.get("username"),
        "role": who.get("role"),
        "is_admin": auth.is_admin(who),
        "authenticated": bool(who.get("authenticated")),
    }


def _deadlock_payload(eng):
    if eng and hasattr(eng, "live_deadlock"):
        return eng.live_deadlock.status()
    return {
        "active": False, "status": "idle", "cycle": [], "jobs": [],
        "victim": None, "triggered_at": None, "detected_at": None,
        "resolved_at": None, "timeline": [],
    }


# ----------------------------------------------------------------------
# authentication
# ----------------------------------------------------------------------
@router.post("/auth/login")
def auth_login(body: LoginBody):
    result = auth.login(body.username, body.password, body.expected_role)
    if not result:
        raise HTTPException(401, "invalid credentials or role")
    return {"ok": True, **result}


@router.post("/auth/logout")
def auth_logout(request: Request):
    auth.logout(auth.bearer_token(request))
    return {"ok": True}


@router.get("/auth/me")
def auth_me(request: Request):
    who = _viewer(request)
    if not who.get("authenticated"):
        raise HTTPException(401, "not authenticated")
    return {"ok": True, "user": _viewer_payload(who)}


@router.post("/auth/password")
def change_password(request: Request, body: PasswordChange):
    """Change the signed-in account's password; other sessions are dropped."""
    who = _viewer(request)
    if not who.get("authenticated") or who.get("user_id") is None:
        raise HTTPException(401, "not authenticated")
    ok = auth.change_password(int(who["user_id"]), body.old_password,
                              body.new_password, auth.bearer_token(request))
    if not ok:
        raise HTTPException(403, "current password is incorrect")
    return {"ok": True}


# ----------------------------------------------------------------------
# state
# ----------------------------------------------------------------------
@router.get("/state")
def state(request: Request):
    who = _viewer(request)
    username = _scoped_username(who)
    with _dict_conn() as c:
        clusters = c.execute(
            """
            SELECT machine_id, hostname, HOST(ip_address) AS ip_address, status,
                   total_cores, allocated_cores, free_cores, clock_speed_ghz,
                   total_ram_mb, allocated_ram_mb, free_ram_mb,
                   holes, largest_free_mb, frag_index
              FROM v_cluster_status
             ORDER BY machine_id
            """
        ).fetchall()

        now = time.time()
        jobs = c.execute(
            """
            SELECT j.job_id, u.username, j.task_name, j.req_cpus, j.req_ram_mb,
                   j.priority, j.remaining_sec, j.status,
                   EXTRACT(EPOCH FROM j.submitted_at) AS submitted_epoch,
                   EXTRACT(EPOCH FROM j.started_at)   AS started_epoch,
                   EXTRACT(EPOCH FROM j.completed_at) AS completed_epoch,
                   (SELECT a.machine_id FROM Allocations a
                     WHERE a.job_id = j.job_id AND a.released_at IS NULL
                     ORDER BY a.alloc_id LIMIT 1) AS machine_id
              FROM Jobs j JOIN Users u ON u.user_id = j.user_id
             WHERE (%s::text IS NULL OR u.username = %s)
             ORDER BY j.job_id DESC LIMIT 150
            """,
            (username, username),
        ).fetchall()
        for j in jobs:
            submitted = float(j.pop("submitted_epoch") or now)
            started = j.pop("started_epoch")
            completed = j.pop("completed_epoch")
            j["wait_sec"] = round(max(0.0, (float(started) if started else now) - submitted), 2)
            j["turnaround_sec"] = round(max(0.0, (float(completed) if completed else now) - submitted), 2)
            for k in ("machine_id", "remaining_sec", "priority", "req_cpus", "req_ram_mb"):
                if j[k] is not None:
                    j[k] = int(j[k])

        counts = c.execute(
            """
            SELECT COUNT(*) FILTER (WHERE j.status='QUEUED')    AS queued,
                   COUNT(*) FILTER (WHERE j.status='RUNNING')   AS running,
                   COUNT(*) FILTER (WHERE j.status='COMPLETED') AS completed,
                   COUNT(*) FILTER (WHERE j.status='FAILED')    AS failed
              FROM Jobs j JOIN Users u ON u.user_id = j.user_id
             WHERE (%s::text IS NULL OR u.username = %s)
            """,
            (username, username),
        ).fetchone()

        job_logs = c.execute(
            """
            SELECT l.log_id, l.job_id, l.ts, l.log_level, l.message
              FROM Execution_Logs l
              JOIN Jobs j ON j.job_id = l.job_id
              JOIN Users u ON u.user_id = j.user_id
             WHERE (%s::text IS NULL OR u.username = %s)
             ORDER BY l.ts DESC LIMIT 60
            """,
            (username, username),
        ).fetchall()
        sys_events = c.execute(
            """
            SELECT event_id, ts, kind, severity, message
              FROM System_Events ORDER BY event_id DESC LIMIT 60
            """
        ).fetchall()
        if username:
            own = {int(r["job_id"]) for r in c.execute(
                "SELECT j.job_id FROM Jobs j JOIN Users u ON u.user_id = j.user_id "
                "WHERE u.username = %s", (username,)
            ).fetchall()}
            sys_events = [
                e for e in sys_events
                if username in e["message"]
                or any(f"#{jid}" in e["message"] for jid in own)
                or (e["kind"] == "DEADLOCK" and any(
                    f"#{jid}" in e["message"] for jid in own))
            ]
        defrags = c.execute(
            """
            SELECT d.event_id, m.hostname, d.triggered_by, d.status, d.reason,
                   d.holes_before, d.holes_after, d.largest_before_mb,
                   d.largest_after_mb, d.frag_before, d.frag_after,
                   d.blocks_moved, d.mb_moved, d.duration_ms, d.started_at
              FROM Defrag_Events d JOIN Machines m USING (machine_id)
             WHERE (%s::text IS NULL OR d.machine_id IN (
                     SELECT DISTINCT a.machine_id FROM Allocations a
                       JOIN Jobs j ON j.job_id = a.job_id
                       JOIN Users u ON u.user_id = j.user_id
                      WHERE u.username = %s))
             ORDER BY d.event_id DESC LIMIT 15
            """,
            (username, username),
        ).fetchall()

        own_ids = {
            int(r["job_id"])
            for r in c.execute(
                "SELECT j.job_id FROM Jobs j JOIN Users u ON u.user_id = j.user_id "
                "WHERE (%s::text IS NULL OR u.username = %s)",
                (username, username),
            ).fetchall()
        }

        # Memory map (block-level layout) so the UI can draw fragmentation as
        # a strip: contiguous runs of FREE / ALLOCATED over [0, total_ram_mb).
        maps: dict[str, list[dict]] = {}
        for b in c.execute(
            "SELECT machine_id, start_mb, size_mb, state FROM Memory_Blocks ORDER BY machine_id, start_mb"
        ).fetchall():
            maps.setdefault(str(b["machine_id"]), []).append(
                {"start_mb": b["start_mb"], "size_mb": b["size_mb"], "state": b["state"]}
            )

    eng = _engine
    wfg_payload = _wfg_state(eng)
    if username:
        wfg_payload = {
            "edges": [e for e in wfg_payload["edges"] if e[0] in own_ids or e[1] in own_ids],
            "edge_details": [
                d for d in wfg_payload["edge_details"]
                if d.get("waiter") in own_ids or d.get("holder") in own_ids
            ],
        }
    return {
        "clusters": clusters,
        "jobs": jobs,
        "counts": counts,
        "job_logs": job_logs,
        "system_events": sys_events,
        "defrags": defrags,
        "maps": maps,
        "policy": {
            "mode": eng.policy_mode if eng else "auto",
            "current": eng.current_policy if eng else "fcfs",
            "reason": eng.current_reason if eng else "engine stopped",
        },
        "engine": {"running": bool(eng and eng.running()), "workers": eng.num_workers if eng else 0},
        "wfg": wfg_payload,
        "viewer": _viewer_payload(who),
        "deadlock": _deadlock_payload(eng),
    }


@router.get("/users")
def users(request: Request):
    """Accounts with quota and live usage.

    An administrator sees every account; a regular user sees only their own.
    Figures are computed from Jobs/Allocations -- nothing is stored twice.
    """
    username = _scoped_username(_viewer(request))
    with _dict_conn() as c:
        rows = c.execute(
            """
            SELECT u.user_id, u.username, u.role, u.quota_cpus, u.quota_ram_mb,
                   COALESCE(SUM(j.req_cpus)   FILTER (WHERE j.status IN ('QUEUED','RUNNING')), 0) AS used_cpus,
                   COALESCE(SUM(j.req_ram_mb) FILTER (WHERE j.status IN ('QUEUED','RUNNING')), 0) AS used_ram_mb,
                   COUNT(j.job_id) FILTER (WHERE j.status='QUEUED')    AS queued,
                   COUNT(j.job_id) FILTER (WHERE j.status='RUNNING')   AS running,
                   COUNT(j.job_id) FILTER (WHERE j.status='COMPLETED') AS completed,
                   COUNT(j.job_id) FILTER (WHERE j.status='FAILED')    AS failed
              FROM Users u
              LEFT JOIN Jobs j ON j.user_id = u.user_id
             WHERE (%s::text IS NULL OR u.username = %s)
             GROUP BY u.user_id, u.username, u.role, u.quota_cpus, u.quota_ram_mb
             ORDER BY u.user_id
            """,
            (username, username),
        ).fetchall()

        active: dict[str, list[int]] = {}
        for r in c.execute(
            """
            SELECT u.username, a.machine_id
              FROM Allocations a
              JOIN Jobs j ON j.job_id = a.job_id
              JOIN Users u ON u.user_id = j.user_id
             WHERE a.released_at IS NULL
             GROUP BY u.username, a.machine_id
            """
        ).fetchall():
            active.setdefault(r["username"], []).append(int(r["machine_id"]))

    for r in rows:
        r["used_cpus"] = int(r["used_cpus"])
        r["used_ram_mb"] = int(r["used_ram_mb"])
        r["free_cpus"] = max(0, r["quota_cpus"] - r["used_cpus"])
        r["free_ram_mb"] = max(0, r["quota_ram_mb"] - r["used_ram_mb"])
        r["cpu_pct"] = round(100.0 * r["used_cpus"] / r["quota_cpus"], 1) if r["quota_cpus"] else 0.0
        r["ram_pct"] = round(100.0 * r["used_ram_mb"] / r["quota_ram_mb"], 1) if r["quota_ram_mb"] else 0.0
        r["active_clusters"] = sorted(active.get(r["username"], []))
    return {"users": rows}


@router.get("/profile")
def profile(request: Request):
    """The signed-in account's own row, with usage computed from Jobs."""
    who = _viewer(request)
    if not who.get("authenticated") or who.get("user_id") is None:
        raise HTTPException(401, "not authenticated")
    uid = int(who["user_id"])
    with _dict_conn() as c:
        row = c.execute(
            """
            SELECT u.user_id, u.username, u.role, u.quota_cpus, u.quota_ram_mb,
                   COALESCE(SUM(j.req_cpus)   FILTER (WHERE j.status IN ('QUEUED','RUNNING')), 0) AS used_cpus,
                   COALESCE(SUM(j.req_ram_mb) FILTER (WHERE j.status IN ('QUEUED','RUNNING')), 0) AS used_ram_mb,
                   COUNT(j.job_id) FILTER (WHERE j.status='QUEUED')    AS queued,
                   COUNT(j.job_id) FILTER (WHERE j.status='RUNNING')   AS running,
                   COUNT(j.job_id) FILTER (WHERE j.status='COMPLETED') AS completed,
                   COUNT(j.job_id) FILTER (WHERE j.status='FAILED')    AS failed
              FROM Users u
              LEFT JOIN Jobs j ON j.user_id = u.user_id
             WHERE u.user_id = %s
             GROUP BY u.user_id, u.username, u.role, u.quota_cpus, u.quota_ram_mb
            """,
            (uid,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "profile not found")
    for key in ("user_id", "quota_cpus", "quota_ram_mb", "used_cpus", "used_ram_mb",
                "queued", "running", "completed", "failed"):
        row[key] = int(row[key])
    row["free_cpus"] = max(0, row["quota_cpus"] - row["used_cpus"])
    row["free_ram_mb"] = max(0, row["quota_ram_mb"] - row["used_ram_mb"])
    row["cpu_pct"] = round(100.0 * row["used_cpus"] / row["quota_cpus"], 1) if row["quota_cpus"] else 0.0
    row["ram_pct"] = round(100.0 * row["used_ram_mb"] / row["quota_ram_mb"], 1) if row["quota_ram_mb"] else 0.0
    return {"ok": True, "profile": row}


@router.post("/users")
def create_user(request: Request, body: NewUser):
    """Admin-only: provision a new account with a role and quotas."""
    auth.require_admin(_viewer(request))
    if not body.username.strip():
        raise HTTPException(400, "username cannot be blank")
    row = auth.create_user(body.username, body.password, body.role,
                           body.quota_cpus, body.quota_ram_mb)
    if row is None:
        raise HTTPException(409, "username already exists")
    with _dict_conn(autocommit=True) as c:
        c.execute(
            "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'ENGINE','INFO',%s)",
            (f"profile created: {row['username']} ({body.role}) "
             f"{body.quota_cpus}c/{body.quota_ram_mb}MB",),
        )
    return {"ok": True, "user": row}


@router.get("/history")
def history(request: Request, minutes: int = 15):
    """Recent time series for the analytics charts.

    Metrics_Samples is written per cluster per sampler tick; Engine_Sample once
    per tick. Both are already ordered by time, so the UI can draw straight
    from these arrays.
    """
    minutes = max(1, min(minutes, 240))
    username = _scoped_username(_viewer(request))
    with _dict_conn() as c:
        metrics = c.execute(
            """
            SELECT EXTRACT(EPOCH FROM s.ts) AS t, s.machine_id, m.hostname,
                   s.used_cores, s.used_ram_mb, s.free_ram_mb,
                   s.holes, s.largest_free_mb, s.frag_index
              FROM Metrics_Samples s JOIN Machines m USING (machine_id)
             WHERE s.ts > NOW() - (%s || ' minutes')::interval
               AND (%s::text IS NULL OR s.machine_id IN (
                     SELECT DISTINCT a.machine_id FROM Allocations a
                       JOIN Jobs j ON j.job_id = a.job_id
                       JOIN Users u ON u.user_id = j.user_id
                      WHERE u.username = %s))
             ORDER BY s.ts
            """,
            (minutes, username, username),
        ).fetchall()
        engine = c.execute(
            """
            SELECT EXTRACT(EPOCH FROM ts) AS t, queue_depth, running_jobs, policy
              FROM Engine_Sample
             WHERE ts > NOW() - (%s || ' minutes')::interval
             ORDER BY ts
            """,
            (minutes,),
        ).fetchall()
    for r in metrics:
        r["t"] = float(r["t"])
        r["frag_index"] = float(r["frag_index"])
    for r in engine:
        r["t"] = float(r["t"])
    return {"minutes": minutes, "metrics": metrics, "engine": engine}


@router.get("/events")
def events(request: Request, since: int = 0):
    username = _scoped_username(_viewer(request))
    with _dict_conn() as c:
        rows = c.execute(
            """
            SELECT event_id, ts, kind, severity, message
              FROM System_Events WHERE event_id > %s
             ORDER BY event_id ASC LIMIT 200
            """,
            (since,),
        ).fetchall()
    if not username:
        return rows
    with _dict_conn() as c:
        own = {
            int(r["job_id"])
            for r in c.execute(
                "SELECT j.job_id FROM Jobs j JOIN Users u ON u.user_id = j.user_id "
                "WHERE u.username = %s", (username,)
            ).fetchall()
        }
    return [
        e for e in rows
        if username in e["message"] or any(f"#{jid}" in e["message"] for jid in own)
    ]


@router.get("/stats")
def stats(request: Request):
    username = _scoped_username(_viewer(request))
    with _dict_conn() as c:
        txn = c.execute(
            """
            SELECT t.op, t.outcome, COUNT(*) AS n, ROUND(AVG(t.duration_ms),2) AS avg_ms,
                   MAX(t.duration_ms) AS max_ms
              FROM Txn_Log t
             WHERE (%s::text IS NULL OR t.job_id IN (
                     SELECT j.job_id FROM Jobs j JOIN Users u ON u.user_id = j.user_id
                      WHERE u.username = %s))
             GROUP BY t.op, t.outcome ORDER BY t.op, t.outcome
            """,
            (username, username),
        ).fetchall()
        util = c.execute(
            """
            SELECT m.hostname,
                   ROUND(AVG(100.0 * s.used_cores / NULLIF(c.total_cores,0)), 2) AS avg_cpu_pct,
                   ROUND(AVG(100.0 * s.used_ram_mb / NULLIF(r.total_ram_mb,0)), 2) AS avg_ram_pct,
                   ROUND(AVG(s.frag_index), 4) AS avg_frag
              FROM Metrics_Samples s
              JOIN Machines m USING (machine_id)
              JOIN CPU_Resources c USING (machine_id)
              JOIN Memory_Resources r USING (machine_id)
             WHERE (%s::text IS NULL OR s.machine_id IN (
                     SELECT DISTINCT a.machine_id FROM Allocations a
                       JOIN Jobs j ON j.job_id = a.job_id
                       JOIN Users u ON u.user_id = j.user_id
                      WHERE u.username = %s))
             GROUP BY m.hostname ORDER BY m.hostname
            """,
            (username, username),
        ).fetchall()
        defrag = c.execute(
            """
            SELECT ROUND(AVG(d.frag_before),4) AS avg_before,
                   ROUND(AVG(d.frag_after),4)  AS avg_after,
                   COUNT(*) AS runs, SUM(d.mb_moved) AS mb_moved
              FROM Defrag_Events d
             WHERE d.status='COMMITTED'
               AND (%s::text IS NULL OR d.machine_id IN (
                     SELECT DISTINCT a.machine_id FROM Allocations a
                       JOIN Jobs j ON j.job_id = a.job_id
                       JOIN Users u ON u.user_id = j.user_id
                      WHERE u.username = %s))
            """,
            (username, username),
        ).fetchone()
        throughput = c.execute(
            """
            SELECT COUNT(*) AS done,
                   ROUND(EXTRACT(EPOCH FROM (MAX(j.completed_at)-MIN(j.completed_at))),1) AS span_sec
              FROM Jobs j JOIN Users u ON u.user_id = j.user_id
             WHERE j.status='COMPLETED' AND (%s::text IS NULL OR u.username = %s)
            """,
            (username, username),
        ).fetchone()
    return {"txn": txn, "utilization": util, "defrag": defrag, "throughput": throughput}


# ----------------------------------------------------------------------
# engine control
# ----------------------------------------------------------------------
@router.post("/engine/start")
def start_engine(request: Request, workers: int = 4, policy: str = "auto"):
    auth.require_admin(_viewer(request))
    global _engine
    if _engine and _engine.running():
        return {"ok": True, "already": True}
    # Join any previous engine's still-draining worker threads *before* building
    # the new one: workers read the single shared worker_mod._stop_event, which
    # Engine.__init__ rebinds -- so a lingering old worker would otherwise stop
    # seeing its own stop flag and keep running after the new engine starts.
    if _engine:
        _engine.stop()
    _engine = engine_mod.Engine(num_workers=workers, policy_mode=policy)
    _engine.start()
    with _dict_conn(autocommit=True) as c:
        c.execute(
            "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'ENGINE','INFO',%s)",
            (f"engine started: {workers} workers, policy={policy}",),
        )
    return {"ok": True, "workers": workers, "policy": policy}


@router.post("/engine/stop")
def stop_engine(request: Request):
    auth.require_admin(_viewer(request))
    if _engine:
        _engine.stop()
        with _dict_conn(autocommit=True) as c:
            c.execute(
                "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'ENGINE','WARN','engine stopped')"
            )
    return {"ok": True}


@router.post("/policy")
def policy(request: Request, body: Policy):
    auth.require_admin(_viewer(request))
    get_engine().policy_mode = body.mode
    return {"ok": True, "mode": body.mode}


# ----------------------------------------------------------------------
# jobs
# ----------------------------------------------------------------------
def _enqueue(job: dict):
    """Push to the live queue if the engine runs; otherwise leave it QUEUED.

    A job submitted while the engine is stopped stays QUEUED in the database
    and is picked up by ``Engine._load_queued`` on the next start.
    """
    if _engine and _engine.running():
        _engine.submit(job)


def _create_job(conn, body: SubmitJob) -> dict | None:
    row = conn.execute(
        """
        INSERT INTO Jobs (user_id, task_name, req_cpus, req_ram_mb, priority, duration_sec)
        SELECT user_id, %s, %s, %s, %s, %s FROM Users WHERE username = %s
        RETURNING job_id
        """,
        (body.task, body.cpus, body.ram_mb, body.priority, body.duration_sec, body.user),
    ).fetchone()
    if not row:
        return None
    jid = row["job_id"]
    return dict(job_id=jid, task_name=body.task, req_cpus=body.cpus,
                req_ram_mb=body.ram_mb, priority=body.priority,
                duration_sec=body.duration_sec, remaining_sec=body.duration_sec,
                submitted_at=time.time())


def _check_quota(conn, username: str, cpus: int, ram_mb: int) -> None:
    """Reject a submission that would push the user past their declared quota.

    Quota is counted over RUNNING + QUEUED jobs, i.e. everything the user has
    in flight, not just what is placed right now.
    """
    row = conn.execute(
        """
        SELECT u.quota_cpus, u.quota_ram_mb,
               COALESCE(SUM(j.req_cpus)   FILTER (WHERE j.status IN ('QUEUED','RUNNING')), 0) AS used_c,
               COALESCE(SUM(j.req_ram_mb) FILTER (WHERE j.status IN ('QUEUED','RUNNING')), 0) AS used_r
          FROM Users u LEFT JOIN Jobs j ON j.user_id = u.user_id
         WHERE u.username = %s GROUP BY u.quota_cpus, u.quota_ram_mb
        """,
        (username,),
    ).fetchone()
    if not row:
        raise HTTPException(400, "user not found")
    if row["used_c"] + cpus > row["quota_cpus"] or row["used_r"] + ram_mb > row["quota_ram_mb"]:
        with _dict_conn(autocommit=True) as c:
            c.execute(
                "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'QUOTA','WARN',%s)",
                (f"{username} over quota: wants {cpus}c/{ram_mb}MB, used {row['used_c']}c/{row['used_r']}MB of {row['quota_cpus']}c/{row['quota_ram_mb']}MB",),
            )
        raise HTTPException(409, "quota exceeded")


@router.post("/jobs")
def submit_job(request: Request, body: SubmitJob):
    who = _viewer(request)
    username = _scoped_username(who)
    if username:
        # A regular user can only submit under their own account, whatever the
        # request body claims.
        body.user = username
    with _dict_conn(autocommit=True) as c:
        _check_quota(c, body.user, body.cpus, body.ram_mb)
        job = _create_job(c, body)
        c.execute(
            "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'SUBMIT','INFO',%s)",
            (f"job#{job['job_id']} {body.task} {body.cpus}c/{body.ram_mb}MB prio{body.priority}",),
        )
    _enqueue(job)
    return {"ok": True, "job_id": job["job_id"]}


@router.post("/jobs/batch")
def batch(request: Request, body: Batch):
    auth.require_admin(_viewer(request))
    rng = random.Random(body.seed)
    jobs = []
    with _dict_conn(autocommit=True) as c:
        for _ in range(body.count):
            task = rng.choice(TASKS)
            cpus = rng.choice((1, 2, 2, 4, 4, 6, 8))
            ram = rng.choice((2048, 4096, 4096, 8192, 12288, 16384))
            pri = rng.randint(1, 10)
            dur = rng.randint(3, 15)
            spec = SubmitJob(task=task, cpus=cpus, ram_mb=ram, priority=pri, duration_sec=dur)
            job = _create_job(c, spec)
            jobs.append(job)
        c.execute(
            "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'SUBMIT','INFO',%s)",
            (f"batch of {body.count} jobs queued (#{jobs[0]['job_id']}..#${jobs[-1]['job_id']})".replace("$", ""),),
        )
    for job in jobs:
        _enqueue(job)
    return {"ok": True, "count": len(jobs),
            "first": jobs[0]["job_id"] if jobs else None,
            "last": jobs[-1]["job_id"] if jobs else None}


# ----------------------------------------------------------------------
# defrag & reset
# ----------------------------------------------------------------------
@router.post("/defrag")
def defrag(request: Request, body: DefragReq | None = None):
    auth.require_admin(_viewer(request))
    cluster = body.cluster if body else "all"
    # defrag.py indexes rows positionally, so use the default tuple-row
    # connection here rather than the dict-row one the read endpoints use.
    with db.connect() as c:
        if cluster == "all":
            mids = [int(r[0]) for r in
                    c.execute("SELECT machine_id FROM Machines ORDER BY machine_id").fetchall()]
        else:
            mids = [int(cluster)]
        results = []
        for mid in mids:
            st = defrag_mod.cluster_status(c, mid)
            if not st:
                continue
            moves = defrag_mod.defrag_plan(c, mid)
            eid = defrag_mod.apply_defrag(c, mid, moves, triggered_by="MANUAL", reason="api request")
            after = defrag_mod.cluster_status(c, mid)
            results.append({
                "machine_id": mid, "event_id": eid, "moves": len(moves),
                "frag_before": float(st["frag_index"]),
                "frag_after": float(after["frag_index"]),
            })
        c.execute(
            "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'DEFRAG','INFO',%s)",
            (f"manual defrag on {len(results)} cluster(s)",),
        )
        c.commit()
    return {"ok": True, "results": results}


@router.post("/reset")
def reset(request: Request):
    auth.require_admin(_viewer(request))
    # Always join any previous engine (even one that is merely draining) before
    # deleting rows, so no worker can touch jobs that no longer exist.
    if _engine:
        _engine.stop()
    db.reset_with_job_seq(100)
    with _dict_conn(autocommit=True) as c:
        c.execute(
            "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'ENGINE','WARN','cluster reset: jobs cleared, block maps rebuilt')"
        )
    return {"ok": True}


# ----------------------------------------------------------------------
# real hold-and-wait deadlocks on the live clusters
# ----------------------------------------------------------------------
@router.post("/deadlock/trigger")
def deadlock_trigger(request: Request):
    auth.require_admin(_viewer(request))
    eng = get_engine()
    if not eng.running():
        raise HTTPException(400, "the engine must be running before triggering a deadlock")
    who = _viewer(request)
    with _dict_conn() as c:
        row = c.execute(
            "SELECT user_id FROM Users WHERE username = %s",
            (who.get("username") or "admin",),
        ).fetchone()
    owner_id = int(row["user_id"]) if row else 1
    return eng.live_deadlock.trigger(owner_id)


@router.post("/deadlock/resolve")
def deadlock_resolve(request: Request):
    auth.require_admin(_viewer(request))
    return get_engine().live_deadlock.resolve()


# ----------------------------------------------------------------------
# legacy scripted scenario -- kept for the report walkthrough and tests
# ----------------------------------------------------------------------
@router.post("/deadlock-demo")
def deadlock_demo():
    """Two jobs, each holding on one cluster and waiting on another.

    The scenario needs two clusters in a known state. Rather than reset the
    whole database -- which deletes whatever the engine is running and
    cascades foreign-key errors into live workers -- two throwaway clusters
    are created for the duration of the demo and dropped afterwards. Live
    jobs, live clusters and a running engine are left untouched.
    """
    transcript = []

    def say(msg):
        transcript.append(msg)

    def request(conn, job_id, machine_id, cores, ram):
        try:
            with conn.transaction():
                conn.execute("SELECT AllocateJobResources(%s,%s,%s,%s)",
                             (job_id, machine_id, cores, ram))
            return True
        except Exception:
            return False

    def provision(conn, hostname, ip, cores, ram):
        mid = conn.execute(
            "INSERT INTO Machines (hostname,ip_address,status) VALUES (%s,%s,'ONLINE') RETURNING machine_id",
            (hostname, ip),
        ).fetchone()["machine_id"]
        conn.execute("INSERT INTO CPU_Resources (machine_id,total_cores,clock_speed_ghz) VALUES (%s,%s,3.20)", (mid, cores))
        conn.execute("INSERT INTO Memory_Resources (machine_id,total_ram_mb) VALUES (%s,%s)", (mid, ram))
        conn.execute("INSERT INTO Memory_Blocks (machine_id,start_mb,size_mb,state) VALUES (%s,0,%s,'FREE')", (mid, ram))
        return mid

    graph = wfg_mod.WaitForGraph()
    m1 = m2 = job_a = job_b = None
    try:
        with _dict_conn(autocommit=True) as conn:
            m1 = provision(conn, "demo-node-1", "10.99.0.1", 20, 65536)
            m2 = provision(conn, "demo-node-2", "10.99.0.2", 20, 65536)
            job_a = conn.execute(
                "INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec) "
                "VALUES (1,'Tensor_Train',12,32768,1,30) RETURNING job_id").fetchone()["job_id"]
            job_b = conn.execute(
                "INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec) "
                "VALUES (1,'Graph_Rank',10,40960,1,30) RETURNING job_id").fetchone()["job_id"]
            say(f"Provisioned two throwaway clusters: node 1 = m{m1}, node 2 = m{m2} (20 cores / 64 GB each)")
            say(f"Submitted Job#{job_a} (Tensor_Train) and Job#{job_b} (Graph_Rank)")

            say(f"Job#{job_a} acquires 32GB RAM on node 2 -> {'GRANTED' if request(conn, job_a, m2, 0, 32768) else 'REFUSED'}")
            say(f"Job#{job_b} acquires 10 cores on node 1 -> {'GRANTED' if request(conn, job_b, m1, 10, 0) else 'REFUSED'}")

            if request(conn, job_a, m1, 12, 0):
                say(f"Job#{job_a} got 12 cores - no deadlock this run")
            else:
                holders = [r["job_id"] for r in conn.execute(
                    "SELECT DISTINCT job_id FROM Allocations WHERE machine_id=%s AND released_at IS NULL", (m1,)).fetchall()]
                for h in holders:
                    graph.add_wait(job_a, h, {
                        "machine_id": m1,
                        "requested_cpus": 12,
                        "requested_ram_mb": 0,
                        "reason": "INSUFFICIENT_RESOURCES",
                    })
                say(f"Job#{job_a} requests 12 cores on node 1 -> BLOCKED (held by {holders})")

            if request(conn, job_b, m2, 0, 40960):
                say(f"Job#{job_b} got 40GB - no deadlock this run")
            else:
                holders = [r["job_id"] for r in conn.execute(
                    "SELECT DISTINCT job_id FROM Allocations WHERE machine_id=%s AND released_at IS NULL", (m2,)).fetchall()]
                for h in holders:
                    graph.add_wait(job_b, h, {
                        "machine_id": m2,
                        "requested_cpus": 0,
                        "requested_ram_mb": 40960,
                        "reason": "INSUFFICIENT_RESOURCES",
                    })
                say(f"Job#{job_b} requests 40GB on node 2 -> BLOCKED (held by {holders})")

            cycle = graph.find_cycle()
            demo_graph = _demo_graph_state(graph, cycle)
            if not cycle:
                say("[WFG] no cycle - no deadlock")
            else:
                say(f"[WFG] circular wait detected: {' -> '.join('Job#'+str(j) for j in cycle)}")
                victim = wfg_mod.youngest(cycle, {job_a: 1.0, job_b: 2.0})
                survivor = next(j for j in cycle if j != victim)
                say(f"[DBMS] rolling back Job#{victim} (youngest in the cycle)")
                with conn.transaction():
                    conn.execute("SELECT ReleaseJobResources(%s)", (victim,))
                    conn.execute("UPDATE Jobs SET status='QUEUED' WHERE job_id=%s", (victim,))
                    conn.execute(
                        "INSERT INTO Execution_Logs (job_id,log_level,message) VALUES (%s,'ERROR','rolled back: deadlock victim')",
                        (victim,))
                graph.clear_job(victim)
                say("[TRANSACTION] ROLLBACK committed; resources released on node 1")
                if request(conn, survivor, m1, 12, 0):
                    say(f"[SCHEDULER] Job#{survivor} unblocked and allocated; Job#{victim} re-queued")

            for mid in (m1, m2):
                conn.execute("SELECT ValidateBlockMap(%s)", (mid,))
            conn.execute(
                "INSERT INTO Txn_Log (job_id,op,outcome,reason,duration_ms) VALUES (NULL,'DEADLOCK_ROLLBACK','COMMIT',%s,0)",
                (f"demo cycle {cycle}",))
            conn.execute(
                "INSERT INTO System_Events (ts,kind,severity,message) VALUES (NOW(),'DEADLOCK','WARN',%s)",
                (f"demo: {' -> '.join('#'+str(j) for j in cycle) if cycle else 'no cycle'}",))
    finally:
        with _dict_conn(autocommit=True) as conn:
            # Jobs first: Allocations.job_id cascades, which also drops the
            # ALLOCATED Memory_Blocks via alloc_id. Only then can the demo
            # Machines go (Allocations.machine_id is not ON DELETE CASCADE).
            if job_a or job_b:
                conn.execute("DELETE FROM Jobs WHERE job_id = ANY(%s)", ([j for j in (job_a, job_b) if j],))
            if m1 or m2:
                conn.execute("DELETE FROM Machines WHERE machine_id = ANY(%s)", ([m for m in (m1, m2) if m],))
        say("Throwaway demo clusters and jobs removed; live state untouched.")
    return {"ok": True, "transcript": transcript, "graph": demo_graph}
