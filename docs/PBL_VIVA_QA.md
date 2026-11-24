# TransactAlloc — Viva Flashcards (40 questions)

> Same 40 questions as Section 14 of `docs/PBL_PREP_REPORT.md`: Q1–Q20 OS, Q21–Q36 DBMS, Q37–Q40 design choices. Cover the answer, say it out loud, then check. File/function names are real — look them up in the repo if a card is fuzzy.

---

## OS — scheduling, memory, deadlocks (Q1–Q20)

### Q1. What is FCFS and where is it implemented?

`policies.fcfs()` — sort jobs by `submitted_at`; non-preemptive. Arrival order wins, priority is ignored.

---

### Q2. In this project, is priority 1 high or low?

High. `policies.effective_priority()` says lower number = higher priority; `Jobs.priority` is `CHECK BETWEEN 1 AND 10`.

---

### Q3. What is aging and how big is a step?

`policies.effective_priority()`: every 10 s waited (`AGING_STEP_SEC = 10`) lowers the number by 1, clamped at 1. It bounds starvation.

---

### Q4. What is the Round-Robin quantum?

5 seconds — `policies.QUANTUM_SEC = 5`. The worker runs `min(5, remaining_sec)` and re-queues the rest.

---

### Q5. How does a preempted RR job resume?

`worker._finish()` sets `status='QUEUED'` with reduced `remaining_sec`; `engine._reconciler()` re-reads it from the DB every second and re-submits it.

---

### Q6. When does the policy switch to Round-Robin?

`adaptive.choose_policy()`: `queue_depth >= 6` → `round-robin` for fairness.

---

### Q7. When does it switch to Priority?

When at least 2 queued jobs have `priority >= 8` (low priority) and have waited > 30 s.

---

### Q8. When does it choose FCFS?

`queue_depth <= 2`, or the default case. Small queues do not benefit from reordering.

---

### Q9. Best-fit vs first-fit — what is the difference?

`policies.best_fit()` minimises the leftover fraction; `policies.first_fit()` takes the first fitting machine. `policies._self_check()` shows them disagree on its sample nodes.

---

### Q10. Is worst-fit implemented?

No. It is only a textbook contrast; `policies.py` has `best_fit` and `first_fit` only.

---

### Q11. There are two best-fit decisions — what are they?

Cluster-level in `policies.best_fit()`, and block-level in SQL `AllocateJobResources()` (`ORDER BY size_mb ASC, start_mb ASC … FOR UPDATE`).

---

### Q12. Define external fragmentation.

Free RAM exists but is split into pieces too small for a request. Measured by `v_cluster_status.frag_index`.

---

### Q13. What is the frag_index formula?

`1 − largest_free_mb / total_free_mb`; if `total_free = 0`, it is defined as 0. A full cluster is not fragmented.

---

### Q14. Why does CPU not fragment?

Cores are fungible; `CPU_Resources` is an aggregate counter. `docs/ER_DIAGRAM.md` states this explicitly.

---

### Q15. What does `defrag_plan()` do?

Walks allocated blocks in address order, emits `{block_id, from_start_mb, to_start_mb, size_mb, seq}` to compact them toward offset 0. No writes.

---

### Q16. Name the four deadlock conditions and where "hold and wait" appears here.

Mutual exclusion, hold and wait, no preemption, circular wait. Hold and wait is in `deadlock.LiveDeadlock._holder()`: hold on one cluster, then request the peer.

---

### Q17. Why isn't the wait-for graph read from `pg_locks`?

Blocked jobs have already rolled back their DB transaction; they wait in the application queue while holding resources elsewhere. No DB lock exists during that wait (`wfg.py` docstring).

---

### Q18. How is a cycle detected?

`wfg.WaitForGraph.find_cycle()` — three-colour DFS; a GREY node reached again is a back edge; nodes visited in sorted order for deterministic output.

---

### Q19. How is the victim chosen and why?

`wfg.youngest()` picks the job with the largest `submitted_at` — least work lost, and a total order prevents livelock.

---

### Q20. What is the worker pool made of?

4 daemon worker threads in `engine.py`, a bounded `PriorityQueue(200)`, a mutex, a counting semaphore, plus sampler/monitor/reconciler threads; `worker.HEARTBEAT_SEC = 2`.

---

## DBMS — schema, transactions, audit (Q21–Q36)

### Q21. What normal form is the schema?

3NF, stated in `schema.sql` header and `docs/SCHEMA.md`, with two documented controlled denormalisations.

---

### Q22. Name the denormalised columns and the reason.

`CPU_Resources.allocated_cores` and `Memory_Resources.allocated_ram_mb` — written in the same transaction as the details and used as the `FOR UPDATE` lock target. Also `Memory_Blocks.job_id` as a convenience copy.

---

### Q23. What are the key 1:1 relationships?

`Machines` ↔ `CPU_Resources` and `Machines` ↔ `Memory_Resources` via `machine_id UNIQUE REFERENCES Machines`.

---

### Q24. Give three CHECK constraints.

`role IN ('Admin','User')`, `priority BETWEEN 1 AND 10`, `allocated_cores <= total_cores` (also `allocated_ram_mb <= total_ram_mb`, `size_mb > 0`).

---

### Q25. Why is `block_no_overlap` DEFERRABLE INITIALLY DEFERRED?

Defrag rewrites block offsets in place, so two blocks can transiently share a `start_mb`; consistency is required only at `COMMIT`.

---

### Q26. Give an atomicity example tied to `Txn_Log`.

`worker.try_allocate()` wraps the carve in `conn.transaction()`; an exception yields `Txn_Log(ALLOCATE, ROLLBACK, reason)`. See `tests/test_db.py`.

---

### Q27. Give a consistency example.

`ValidateBlockMap()` plus the CHECK constraints; `demo.finish_and_report()` asserts `allocated <= total` on every node.

---

### Q28. Give an isolation example with a number.

64 threads in `tests/test_concurrency.py` race cluster 4; `FOR UPDATE` ensures `used_c == winners * 4` and no over-commit.

---

### Q29. Give a durability example.

Committed rows persist in PostgreSQL; `Txn_Log.job_id ON DELETE SET NULL` and loose `machine_id` let the audit outlive deleted jobs.

---

### Q30. What does `AllocateJobResources()` lock and in which order?

`CPU_Resources` row then `Memory_Resources` row, both `FOR UPDATE`, then the chosen block `FOR UPDATE`.

---

### Q31. Difference between `INSUFFICIENT_RESOURCES` and `FRAGMENTED`?

Both are `RAISE EXCEPTION`. The first means aggregate `total − allocated` is too small; the second means aggregate is enough but no single FREE hole fits.

---

### Q32. What does `ReleaseJobResources()` deliberately NOT do?

It does not change job status — completion, RR preemption and deadlock rollback all release but set different statuses.

---

### Q33. What does `CoalesceFreeBlocks()` do?

Loops until no two adjacent FREE blocks remain; the merged block takes the predecessor's `start_mb` to avoid overlap.

---

### Q34. What does `ValidateBlockMap()` check?

Sum of block sizes = capacity, no gap/overlap when ordered by `start_mb`, end offset = capacity, and ALLOCATED sum = `allocated_ram_mb`; raises `BLOCK_MAP_INVALID`.

---

### Q35. What columns does `v_cluster_status` add?

`free_cores`, `free_ram_mb`, `holes`, `largest_free_mb`, `frag_index` (plus machine/resource details).

---

### Q36. Name the audit tables and one column each.

`Txn_Log.op`, `Policy_Changes.reason`, `Defrag_Events.frag_before/frag_after`, `Defrag_Moves.from_start_mb/to_start_mb`, `System_Events.kind`, `Execution_Logs.log_level`, `Metrics_Samples.frag_index`, `Engine_Sample.queue_depth`.

---

## Design choices (Q37–Q40)

### Q37. Why model RAM as blocks instead of a counter?

So fragmentation is a SQL query and "best-fit hole" / "move block X to Y" are expressible. A counter cannot tell those apart.

---

### Q38. Why two deadlock layers?

Avoidance is cheap but needs future claims; detection handles unknown claims with recovery. Keeping both enables a direct teaching comparison (`demo.py`, `evaluate.py`).

---

### Q39. Why is placement independent of scheduling policy?

`policies.best_fit()` answers *where*; `adaptive.py` answers *which job next*. `worker.pick_machine()` uses best-fit for all policies (first-fit only when explicitly requested).

---

### Q40. Why FastAPI + vanilla JS?

FastAPI gives validated JSON routes and an in-process `TestClient` for `evaluate.py`; vanilla JS needs no build step and polls the API for live state.
