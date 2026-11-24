# TransactAlloc: Adaptive Cloud Resource Manager with Fragmentation-Aware Reallocation

*B.Tech OSDBMS-V26-T198 — Team ArcReactor (T198)*

An **adaptive, fragmentation-aware cloud resource manager** built on
PostgreSQL. Four clusters managing **500 GB of RAM**, a live dashboard, and a
full DBMS treatment: normalised schema, ACID allocation, external-fragmentation
tracking with defragmentation, wait-for-graph deadlock detection with rollback,
and a performance evaluation.

This is the merged phase-3 deliverable: the console deadlock work is folded
into a running web service with a real block-level memory model.

---

## What it does

- **Schedules** jobs with **FCFS / Priority / Round-Robin**, switching policy
  automatically as load changes (and logging *why* to `Policy_Changes`).
- **Places** each job on the **best-fit** cluster.
- **Allocates** through real SQL transactions (`AllocateJobResources`), with
  `FOR UPDATE` locking and `CHECK` constraints as the backstop. Every outcome
  is recorded in `Txn_Log` — the ACID evidence trail.
- **Models RAM as a block map**, not a counter, so external fragmentation is a
  *query*: `v_cluster_status.frag_index = 1 - largest_free / total_free`.
- **Defragments** on demand (compact a cluster's blocks, recording before/after
  in `Defrag_Events` / `Defrag_Moves`) and automatically when an allocation
  fails with `FRAGMENTED`.
- **Detects deadlocks** with a wait-for graph and rolls back the youngest
  transaction, re-queuing it. A scripted live demo reproduces the report's
  circular-wait scenario end to end.
- **Shows it all live**: cluster load bars, a memory fragmentation strip per
  cluster, job table, wait-for graph, defrag history, event feed.
- **Manages profiles**: an admin can add an account with a role and quotas from
  the **Add profile** button; every signed-in user gets a **Profile** page with
  live quota usage and a password-change form.

---

## Stack

| Layer | Choice |
|---|---|
| Database | PostgreSQL 17 (Docker) |
| Backend | Python + FastAPI + uvicorn, `psycopg` 3 (psycopg3) |
| Frontend | Vanilla HTML / CSS / JS (no build step) |
| Tests | pytest; CI runs the database-free suite, while PostgreSQL tests require a configured database |

---

## Setup

### 1. Database (Docker)

```powershell
docker run -d --name transactalloc-pg -e POSTGRES_PASSWORD=crm -e POSTGRES_DB=transactalloc_p3 -p 5432:5432 postgres:17
```

Connection settings are read from environment variables; see [`.env.example`](.env.example).
`.env` files are not loaded automatically. Defaults are `localhost:5432`, user `postgres`, password `crm`, and database `transactalloc_p3`.

For the complete database and application setup, export
`TRANSALLOC_DB_PASSWORD` and run:

```powershell
docker compose up --build
```

The dashboard is then available at `http://127.0.0.1:8000`. The application
container waits for PostgreSQL, applies `schema.sql` and `seed.sql`, and starts
the ASGI app. Do not commit the environment file containing your password.

### 2. Python

```powershell
py -3 -m pip install -r requirements.txt
```

### 3. Schema + seed

```powershell
py -3 main.py init      # applies schema.sql then seed.sql
```

---

## Run it

```powershell
py -3 main.py serve     # http://127.0.0.1:8000
```

Open the **Cloud Management System** dashboard and sign in with a password:

| Account | Password | Role | Sees |
|---|---|---|---|
| `admin` | `admin123` | Administrator | every cluster, account, job, event and control |
| `Archita` | `archita123` | User | only their own jobs, quota, events and analytics |
| `student` | `student123` | User | only their own jobs, quota, events and analytics |

Pick the matching role tab on the sign-in screen. Engine controls, Batch,
Defrag, Reset and deadlock controls are administrator-only; regular users can
submit jobs under their own account. Press **Start** and **Batch** to run work.
Use the top navbar to move between Overview, Account, Profile, Clusters, Jobs,
Deadlock and Analytics. **Add profile** (admin) opens a form for a new username,
password, role and quotas; the new account can sign in immediately. The
**Profile** page shows your own quota usage and lets you change your password
(other sessions for that account are signed out).

In the Deadlock section, **Trigger live deadlock** starts a real hold-and-wait
between two running jobs on the live clusters. The wait-for graph and timeline
update as it happens; the cycle is left in place until **Resolve deadlock** is
pressed, which rolls back the youngest victim and lets the survivor finish.

Other entry points:

```powershell
py -3 main.py init                      # (re)create schema + seed
py -3 main.py reset                     # clear jobs, rebuild block maps
py -3 main.py list                      # cluster status
py -3 main.py submit --task X --cpus 4 --ram 8192
py -3 main.py batch --count 20
py -3 main.py dashboard                 # console node metrics
py -3 main.py run --policy fcfs --workers 4 --max-rounds 25 --avoidance off

py -3 demo.py                           # report §5 walkthrough (detection)
py -3 demo.py --skip-batch --avoidance on   # Banker's avoidance instead
py -3 test_deadlock.py                  # proves both deadlock layers
py -3 wfg.py                            # wait-for-graph self-check (no DB)
py -3 banker.py                         # Banker's self-check (no DB)
```

`run` dispatches queued jobs through the selected policy. `--max-rounds`
bounds scheduler dispatches and the scheduler is stopped cleanly afterward.
`--avoidance on` is intentionally rejected by the live runner because Banker
avoidance is demo/evaluation-only; use `demo.py --avoidance on` or pure
evaluation mode instead.

---

### Authentication and live deadlock API

```
POST /api/auth/login       {username, password, expected_role} -> {token, user}
POST /api/auth/logout      (Bearer token)
GET  /api/auth/me          (Bearer token)
GET  /api/profile          (Bearer token)  -> own account + live quota usage
POST /api/auth/password    (Bearer token)  {old_password, new_password}
POST /api/users            (admin)  {username, password, role, quota_cpus, quota_ram_mb}
POST /api/deadlock/trigger (admin)  -> starts a real two-phase circular wait
POST /api/deadlock/resolve (admin)  -> rolls back the victim and clears the cycle
```

All `/api/*` calls from the dashboard send `Authorization: Bearer <token>`.
Read responses are scoped to the signed-in identity. Requests without a token
keep the original legacy behaviour used by the CLI and the pytest suite.
`POST /api/deadlock-demo` is retained only for the original report walkthrough
and its tests; the dashboard does not call it.

## Tests

With PostgreSQL initialized (`py -3 main.py init`), the whole suite passes:

```powershell
py -3 -m pytest -q
```

The two tests named in the project synopsis live at the repository root:

- `test_defrag.py` — proves fragmented free blocks are merged into larger
  chunks and that every migration is recorded accurately in `Defrag_Moves`
  (old address → new address) with a valid block map afterwards.
- `test_deadlock.py` — proves wait-for-graph cycle detection and youngest-victim
  rollback under the report's circular-wait scenario, plus Banker's avoidance
  refusing the same unsafe grant. Both can also be run as scripts.

The rest of the suite:

- `tests/test_evaluate.py`, `tests/test_pure.py`, `tests/test_cli.py`, and
  `tests/test_engine.py` — database-free evaluation, policies, WFG, Banker,
  CLI, API-contract, and live-monitor tests.
- `tests/test_db.py` — allocation/release, block-map validity, deadlock scenario.
- `tests/test_frag.py` — fragmentation math, defrag plan/apply, block-map invariant.
- `tests/test_concurrency.py` — 64 threads racing the allocator: never overcommits.
- `tests/test_api_e2e.py` — the whole app in-process: submit → run → stats,
  policy switch, defrag, deadlock demo, graceful stop & resume.

The database-free subset (no PostgreSQL needed) is:

```powershell
py -3 -m pytest tests\test_evaluate.py tests\test_pure.py tests\test_cli.py tests\test_engine.py -q
```

> Only one engine should run against the database at a time. A leftover
> `main.py serve` process shares the same `Jobs` table and its reconciler will
> re-admit jobs another engine has just parked as `QUEUED`.

---

## Performance evaluation

```powershell
py -3 evaluate.py --jobs 20 --workers 4 --out docs/EVALUATION.md
py -3 evaluate.py --pure --jobs 12 --seed 11
```

Runs the same synthetic workload under each policy (best-fit placement) and
writes throughput, average/p95 wait, turnaround, peak fragmentation,
defragmentation activity and allocation-txn latency. Current results:
[`docs/EVALUATION.md`](docs/EVALUATION.md).

`--pure` runs without PostgreSQL and compares FCFS, priority, round-robin
(one-tick quantum), and demo-only Banker avoidance on a deterministic
two-phase workload. It reports response/resource wait, nearest-rank P95,
turnaround, service, makespan, throughput, and Banker refusal attempts.
Deadlock and rollback counts in pure mode are synthetic simulation events, not
live PostgreSQL engine or WFG measurements.

---

## The two deadlock layers

**Avoidance** (`banker.py`) — before granting, test whether some completion
order still exists; refuse the grant that would make the cluster unsafe. The
report's scenario is a safe-looking grant that is actually fatal.

**Detection** (`wfg.py` + engine monitor) — let it happen, then recover. Each
blocked job gets an edge to whoever holds what it wants; a cycle is a deadlock;
the **youngest** job is rolled back, its resources are released, and it is
re-queued. The live monitor records transaction and system events. The demo:

```
[WFG] circular wait detected: Job#121 -> Job#122 -> Job#121
[DBMS] rolling back Job#122 (youngest in the cycle)
[TRANSACTION] ROLLBACK committed; resources released on cluster 1
[SCHEDULER] Job#121 unblocked and allocated; Job#122 re-queued
```

Nothing is printed theatre — every line follows a real transaction, and the
dashboard afterwards reads committed rows.

The API exposes `wfg.edges` and matching `wfg.edge_details` in `/api/state`.
The dashboard renders waiter → holder arrows, resource/cluster explanations,
an empty-graph state, and actual deadlock/rollback events from the API event
feed.

Why the graph is application-level, not `pg_locks`: these jobs are **not**
blocked inside PostgreSQL. `AllocateJobResources` raised `FRAGMENTED` /
`INSUFFICIENT_RESOURCES`, the transaction rolled back, and the job is parked in
the application queue while possibly holding resources on another cluster. No
database lock is held during that wait, so only the scheduler can see it.

---

## Project layout

Key modules (as listed in the project synopsis): `main.py`, `scheduler.py`,
`defrag.py`, `deadlock.py`, `dashboard.py`, `schema.sql`.

```
schema.sql  seed.sql        database: schema + seed (4 clusters / 500 GB RAM)
db.py                       connection + reset helpers
engine.py                   queue, worker pool, adaptive policy, reconciler,
                            deadlock monitor, metrics sampler
worker.py                   one job's lifecycle over real transactions
defrag.py adaptive.py       fragmentation/compaction + policy selection
deadlock.py                 live two-phase hold-and-wait sessions
policies.py wfg.py banker.py scheduling / placement / graph / avoidance
api.py app.py main.py       HTTP layer, ASGI app, CLI
dashboard.py                console node-metrics renderer
evaluate.py demo.py         evaluation harness, report walkthrough
test_defrag.py              defrag migration-accuracy test (synopsis)
test_deadlock.py            deadlock detection/rollback test (synopsis)
static/                     web dashboard (index.html, styles.css, app.js)
tests/                      remaining pytest suite
docs/                       ER_DIAGRAM.md, SCHEMA.md, ARCHITECTURE.md, EVALUATION.md

frag.py, live_deadlock.py   backward-compatible aliases of defrag/deadlock
```

## Docs

- [`docs/ER_DIAGRAM.md`](docs/ER_DIAGRAM.md) — entity-relationship model.
- [`docs/SCHEMA.md`](docs/SCHEMA.md) — every table, constraint and function; normalisation notes.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — components, job lifecycle, deadlock & fragmentation handling.
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — measured performance per policy.
