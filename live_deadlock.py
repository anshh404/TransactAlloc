"""Real hold-and-wait deadlocks on the live clusters.

The ordinary worker allocates a job's whole requirement on a single cluster
in one transaction, then never asks for anything else, so a live job can
never really deadlock. This module adds a two-phase job that genuinely holds
resources on one cluster while requesting more on another. Both phases go
through the real AllocateJobResources()/ReleaseJobResources() transactions,
so the circular wait, the wait-for edges, the victim rollback and the final
release are all committed database state -- not a scripted transcript.

A detected cycle is left in place until an operator presses Resolve. The
engine's automatic resolver is suspended while a session is active.
"""

from __future__ import annotations

import threading
import time

import psycopg
from psycopg.rows import dict_row

import db
import wfg as wfg_mod

_WAIT_TIMEOUT = 20.0


def _stamp() -> str:
    return time.strftime("%H:%M:%S", time.localtime())


class LiveDeadlock:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.session: dict | None = None
        self._abort = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    def has_active(self) -> bool:
        with self.lock:
            return bool(self.session) and self.session["status"] in (
                "triggering",
                "awaiting_detection",
                "detected",
                "resolving",
            )

    def status(self) -> dict:
        with self.lock:
            s = self.session
            if not s:
                return {
                    "active": False,
                    "status": "idle",
                    "cycle": [],
                    "jobs": [],
                    "victim": None,
                    "triggered_at": None,
                    "detected_at": None,
                    "resolved_at": None,
                    "timeline": [],
                }
            return {
                "active": s["status"] in (
                    "triggering", "awaiting_detection", "detected", "resolving"
                ),
                "status": s["status"],
                "cycle": list(s["cycle"]),
                "jobs": [dict(j) for j in s["jobs"]],
                "victim": s["victim"],
                "triggered_at": s["triggered_at"],
                "detected_at": s["detected_at"],
                "resolved_at": s["resolved_at"],
                "timeline": list(s["timeline"]),
            }

    def _say(self, message: str) -> None:
        with self.lock:
            if self.session:
                self.session["timeline"].append(f"[{_stamp()}] {message}")

    # ------------------------------------------------------------------
    # trigger
    # ------------------------------------------------------------------
    def trigger(self, owner_id: int) -> dict:
        with self.lock:
            if self.has_active():
                return {"ok": False, "error": "A deadlock is already active"}
            self._abort.clear()
            with db.connect(row_factory=dict_row) as c:
                machines = c.execute(
                    """
                    SELECT machine_id, hostname, total_cores, free_cores,
                           total_ram_mb, free_ram_mb, largest_free_mb
                      FROM v_cluster_status
                     WHERE status = 'ONLINE'
                     ORDER BY free_ram_mb DESC, machine_id
                    """
                ).fetchall()
            if len(machines) < 2:
                return {"ok": False, "error": "At least two online clusters are needed"}

            def plan(m):
                hold_ram = int(m["total_ram_mb"] * 0.70 // 1024) * 1024
                request_ram = int(m["total_ram_mb"] * 0.55 // 1024) * 1024
                hold_cpus = max(1, int(m["total_cores"] * 0.60))
                request_cpus = max(1, int(m["total_cores"] * 0.50))
                return hold_ram, request_ram, hold_cpus, request_cpus

            feasible = [
                m
                for m in machines
                if (lambda p: p[0] > 0 and m["free_cores"] >= p[2] and m["free_ram_mb"] >= p[0] and m["largest_free_mb"] >= p[0])(
                    plan(m)
                )
            ]
            if len(feasible) < 2:
                return {
                    "ok": False,
                    "error": "Two clusters need a 70% contiguous free block; reset or defrag first",
                }
            a, b = feasible[0], feasible[1]
            pa, pb = plan(a), plan(b)

            # Two real job rows, owned by the operator, visible everywhere.
            with db.connect(autocommit=True, row_factory=dict_row) as c:
                job_a = c.execute(
                    "INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec) "
                    "VALUES (%s,%s,%s,%s,1,60) RETURNING job_id",
                    (owner_id, "HoldWait_Alpha", pa[2], pa[0]),
                ).fetchone()["job_id"]
                job_b = c.execute(
                    "INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec) "
                    "VALUES (%s,%s,%s,%s,1,60) RETURNING job_id",
                    (owner_id, "HoldWait_Beta", pb[2], pb[0]),
                ).fetchone()["job_id"]

            self.session = {
                "status": "triggering",
                "cycle": [],
                "victim": None,
                "triggered_at": time.time(),
                "detected_at": None,
                "resolved_at": None,
                "timeline": [],
                "events": {job_a: threading.Event(), job_b: threading.Event()},
                "finished": set(),
                "jobs": [
                    {
                        "job_id": job_a,
                        "task": "HoldWait_Alpha",
                        "hold_machine_id": a["machine_id"],
                        "hold_host": a["hostname"],
                        "request_machine_id": b["machine_id"],
                        "request_host": b["hostname"],
                        "hold_cpus": pa[2],
                        "hold_ram_mb": pa[0],
                        "request_cpus": pb[2],
                        "request_ram_mb": pb[0],
                    },
                    {
                        "job_id": job_b,
                        "task": "HoldWait_Beta",
                        "hold_machine_id": b["machine_id"],
                        "hold_host": b["hostname"],
                        "request_machine_id": a["machine_id"],
                        "request_host": a["hostname"],
                        "hold_cpus": pb[2],
                        "hold_ram_mb": pb[0],
                        "request_cpus": pa[2],
                        "request_ram_mb": pa[0],
                    },
                ],
            }
            self._say(f"Submitted Job #{job_a} and Job #{job_b} as two-phase workloads")
            specs = [
                (job_a, a["machine_id"], b["machine_id"], pa[2], pa[0], pa[2], pa[0], 0.35, a["hostname"], b["hostname"]),
                (job_b, b["machine_id"], a["machine_id"], pb[2], pb[0], pb[2], pb[0], 1.05, b["hostname"], a["hostname"]),
            ]
            # For A the second request must exceed what B holds on a; use a's
            # request sizing against b's hold, and vice versa.
            specs[0] = (job_a, a["machine_id"], b["machine_id"], pa[2], pa[0], pb[2], pb[0], 0.35, a["hostname"], b["hostname"])
            specs[1] = (job_b, b["machine_id"], a["machine_id"], pb[2], pb[0], pa[2], pa[0], 1.05, b["hostname"], a["hostname"])

        for spec in specs:
            t = threading.Thread(target=self._holder, args=spec, daemon=True,
                                 name=f"ta-dl-{spec[0]}")
            t.start()
            self._threads.append(t)
        return {"ok": True, "deadlock": self.status()}

    # ------------------------------------------------------------------
    # one of the two holding jobs
    # ------------------------------------------------------------------
    def _holder(self, job_id, hold_mid, peer_mid, hold_cpus, hold_ram,
                req_cpus, req_ram, delay, hold_host, peer_host) -> None:
        granted_second = False
        try:
            with db.connect(autocommit=True) as c:
                started = time.perf_counter()
                with c.transaction():
                    c.execute(
                        "SELECT AllocateJobResources(%s,%s,%s,%s)",
                        (job_id, hold_mid, hold_cpus, hold_ram),
                    )
                dur = int((time.perf_counter() - started) * 1000)
                c.execute(
                    "INSERT INTO Txn_Log (job_id,machine_id,op,outcome,reason,duration_ms) "
                    "VALUES (%s,%s,'ALLOCATE','COMMIT',%s,%s)",
                    (job_id, hold_mid, "phase 1: hold and wait", dur),
                )
                c.execute(
                    "INSERT INTO Execution_Logs (job_id,log_level,message) VALUES (%s,'INFO',%s)",
                    (job_id, f"holding {hold_cpus}c/{hold_ram}MB on {hold_host}"),
                )
            self._say(f"Job #{job_id} acquired {hold_cpus}c/{hold_ram}MB on {hold_host}")

            time.sleep(delay)
            if self._abort.is_set() or self.engine._stop.is_set():
                return

            with db.connect(autocommit=True) as c:
                started = time.perf_counter()
                try:
                    with c.transaction():
                        c.execute(
                            "SELECT AllocateJobResources(%s,%s,%s,%s)",
                            (job_id, peer_mid, req_cpus, req_ram),
                        )
                    granted_second = True
                    dur = int((time.perf_counter() - started) * 1000)
                    c.execute(
                        "INSERT INTO Txn_Log (job_id,machine_id,op,outcome,reason,duration_ms) "
                        "VALUES (%s,%s,'ALLOCATE','COMMIT',%s,%s)",
                        (job_id, peer_mid, "phase 2: second resource", dur),
                    )
                except psycopg.errors.RaiseException as exc:
                    reason = str(exc).strip().splitlines()[0]
                    dur = int((time.perf_counter() - started) * 1000)
                    holders = [
                        int(r[0])
                        for r in c.execute(
                            "SELECT DISTINCT job_id FROM Allocations "
                            "WHERE machine_id=%s AND released_at IS NULL",
                            (peer_mid,),
                        ).fetchall()
                        if int(r[0]) != job_id
                    ]
                    self.engine.wfg.set_waits(
                        job_id,
                        [
                            {
                                "holder": h,
                                "machine_id": peer_mid,
                                "requested_cpus": req_cpus,
                                "requested_ram_mb": req_ram,
                                "reason": reason.split(":")[0],
                            }
                            for h in holders
                        ],
                    )
                    c.execute(
                        "INSERT INTO Txn_Log (job_id,machine_id,op,outcome,reason,duration_ms) "
                        "VALUES (%s,%s,'ALLOCATE','ROLLBACK',%s,%s)",
                        (job_id, peer_mid, reason[:240], dur),
                    )
                    c.execute(
                        "INSERT INTO Execution_Logs (job_id,log_level,message) VALUES (%s,'ERROR',%s)",
                        (job_id, f"blocked on {peer_host}: {reason.split(':')[0]}"),
                    )
                    self._say(
                        f"Job #{job_id} requests {req_cpus}c/{req_ram}MB on {peer_host} "
                        f"-> BLOCKED behind {holders or ['unknown']}"
                    )

            if granted_second:
                self._say(f"Job #{job_id} obtained its second resource; no circular wait")
                self._fail("The second grant succeeded, so no deadlock formed")
                return

            with self.lock:
                s = self.session
                if s and s["status"] == "triggering":
                    s["status"] = "awaiting_detection"

            with self.lock:
                event = self.session["events"][job_id] if self.session else threading.Event()
            deadline = time.monotonic() + _WAIT_TIMEOUT
            while not event.is_set() and not self._abort.is_set() and not self.engine._stop.is_set():
                if time.monotonic() > deadline:
                    self._fail("Deadlock was not resolved in time")
                    return
                event.wait(0.2)

            if self._abort.is_set() or self.engine._stop.is_set():
                self._cleanup(job_id)
                return

            with self.lock:
                victim = self.session["victim"] if self.session else None
            with db.connect(autocommit=True) as c:
                started = time.perf_counter()
                if job_id == victim:
                    with c.transaction():
                        c.execute("SELECT ReleaseJobResources(%s)", (job_id,))
                        c.execute(
                            "UPDATE Jobs SET status='QUEUED', remaining_sec=1, duration_sec=1 "
                            "WHERE job_id=%s",
                            (job_id,),
                        )
                        c.execute(
                            "INSERT INTO Execution_Logs (job_id,log_level,message) "
                            "VALUES (%s,'ERROR','rolled back: deadlock victim')",
                            (job_id,),
                        )
                    dur = int((time.perf_counter() - started) * 1000)
                    c.execute(
                        "INSERT INTO Txn_Log (job_id,op,outcome,reason,duration_ms) "
                        "VALUES (%s,'DEADLOCK_ROLLBACK','COMMIT',%s,%s)",
                        (job_id, f"victim of cycle #{job_id}", dur),
                    )
                    self._say("[DBMS] ROLLBACK committed; held resources released")
                    self._say(f"Job #{job_id} rolled back; its resources released and job re-queued")
                else:
                    with c.transaction():
                        c.execute(
                            "SELECT AllocateJobResources(%s,%s,%s,%s)",
                            (job_id, peer_mid, req_cpus, req_ram),
                        )
                    with c.transaction():
                        c.execute("SELECT ReleaseJobResources(%s)", (job_id,))
                        c.execute(
                            "UPDATE Jobs SET status='COMPLETED', remaining_sec=0, completed_at=NOW() "
                            "WHERE job_id=%s",
                            (job_id,),
                        )
                    self._say(f"Job #{job_id} unblocked, acquired its second resource and completed")
            self.engine.wfg.clear_job(job_id)
        except Exception as exc:  # pragma: no cover - defensive
            self._say(f"Job #{job_id} scenario ended: {str(exc)[:140]}")
            self._cleanup(job_id)
        finally:
            with self.lock:
                if self.session:
                    self.session["finished"].add(job_id)

    def _cleanup(self, job_id: int) -> None:
        try:
            with db.connect(autocommit=True) as c:
                with c.transaction():
                    c.execute("SELECT ReleaseJobResources(%s)", (job_id,))
                    c.execute("UPDATE Jobs SET status='QUEUED' WHERE job_id=%s", (job_id,))
        except Exception:
            pass
        self.engine.wfg.clear_job(job_id)

    # ------------------------------------------------------------------
    # detection / resolution
    # ------------------------------------------------------------------
    def on_cycle(self, cycle) -> None:
        with self.lock:
            s = self.session
            if not s or s["status"] in ("resolving", "resolved", "failed"):
                return
            if not set(cycle) <= {j["job_id"] for j in s["jobs"]}:
                return
            s["cycle"] = list(cycle)
            s["status"] = "detected"
            s["detected_at"] = time.time()
            sub = {}
            with db.connect(row_factory=dict_row) as c:
                rows = c.execute(
                    "SELECT job_id, EXTRACT(EPOCH FROM submitted_at) AS t "
                    "FROM Jobs WHERE job_id = ANY(%s)",
                    (list(dict.fromkeys(cycle)),),
                ).fetchall()
            sub = {int(r["job_id"]): float(r["t"]) for r in rows}
            s["victim"] = wfg_mod.youngest(cycle, sub) if sub else cycle[-1]
            path = " -> ".join("#" + str(j) for j in cycle)
            self._say(f"[WFG] circular wait detected: {path}")
            self._say(f"[DBMS] youngest transaction in the cycle is Job #{s['victim']}")
            with db.connect(autocommit=True) as c:
                c.execute(
                    "INSERT INTO System_Events (ts,kind,severity,message) "
                    "VALUES (NOW(),'DEADLOCK','WARN',%s)",
                    (f"live circular wait {path}; Job #{s['victim']} awaiting operator resolution",),
                )

    def resolve(self) -> dict:
        with self.lock:
            s = self.session
            if not s:
                return {"ok": False, "error": "No deadlock is active"}
            if s["status"] != "detected":
                return {"ok": False, "error": f"Deadlock is {s['status']}, not awaiting resolution"}
            s["status"] = "resolving"
            s["resolved_at"] = time.time()
            victim = s["victim"]
            cycle = list(s["cycle"])
            path = " -> ".join("#" + str(j) for j in cycle)
            self._say(f"[OPERATOR] resolve requested; rolling back Job #{victim}")
            for event in s["events"].values():
                event.set()
        deadline = time.monotonic() + 8
        while True:
            with self.lock:
                if len(self.session["finished"]) >= len(self.session["events"]) or time.monotonic() > deadline:
                    break
            time.sleep(0.1)
        with self.lock:
            s["status"] = "resolved"
            self.engine.wfg.clear_job(victim)
        try:
            with db.connect(autocommit=True) as c:
                c.execute(
                    "INSERT INTO Txn_Log (job_id,op,outcome,reason,duration_ms) "
                    "VALUES (%s,'DEADLOCK_ROLLBACK','COMMIT',%s,0)",
                    (victim, f"live cycle {path}"),
                )
                c.execute(
                    "INSERT INTO System_Events (ts,kind,severity,message) "
                    "VALUES (NOW(),'DEADLOCK','INFO',%s)",
                    (f"resolved {path}; Job #{victim} rolled back and re-queued",),
                )
        except Exception:
            pass
        self._say("Deadlock resolved; all held resources released")
        return {"ok": True, "deadlock": self.status()}

    def _fail(self, reason: str) -> None:
        with self.lock:
            if self.session:
                self.session["status"] = "failed"
            for event in self.session["events"].values() if self.session else []:
                event.set()
        self._say(reason)

    def abort(self) -> None:
        """Called when the engine stops or the cluster is reset."""
        self._abort.set()
        with self.lock:
            s = self.session
            events = list(s["events"].values()) if s else []
            jobs = [j["job_id"] for j in s["jobs"]] if s else []
        for event in events:
            event.set()
        for job_id in jobs:
            self._cleanup(job_id)
        for t in self._threads:
            t.join(timeout=1.0)
        with self.lock:
            if self.session:
                self.session["status"] = "aborted"
                self.session["resolved_at"] = time.time()
        self._threads = []
