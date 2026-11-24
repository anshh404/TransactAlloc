"""Defragmentation tests (Phase III deliverable ``test_defrag.py``).

The synopsis claims two properties for the defragmenter:

1. scattered free blocks are merged back into larger contiguous chunks;
2. the migration is accurate -- every move is recorded (old address -> new
   address), the allocated block actually ends up at the recorded address,
   and the block map is still a valid partition afterwards.

Both are verified here against the real PostgreSQL functions, not a mock.
Run with ``py -3 -m pytest test_defrag.py -q`` or as part of the full suite.
"""

import psycopg

import db
import defrag


CLUSTER = 1


def setup_module():
    db.reset_all()


def _job(conn, ram_mb, name="DefragTest"):
    return conn.execute(
        "INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec) "
        "VALUES (1,%s,1,%s,5,5) RETURNING job_id",
        (name, ram_mb),
    ).fetchone()[0]


def _allocate(conn, job_id, ram_mb):
    with conn.transaction():
        conn.execute(
            "SELECT AllocateJobResources(%s,%s,1,%s)", (job_id, CLUSTER, ram_mb)
        )


def _release(conn, job_id):
    with conn.transaction():
        conn.execute("SELECT ReleaseJobResources(%s)", (job_id,))


def test_defrag_merges_free_blocks_with_accurate_moves(conn):
    """Four 4 GB blocks, middle two freed -> compaction merges the holes.

    Before:  ALLOC[0..4G] FREE[4..12G] ALLOC[12..16G] FREE[16..64G]
    After:   ALLOC[0..4G] ALLOC[4..8G] FREE[8..64G]
    """
    db.reset_all()

    jobs = []
    for _ in range(4):
        jid = _job(conn, 4096)
        _allocate(conn, jid, 4096)
        jobs.append(jid)

    blocks = conn.execute(
        "SELECT block_id, start_mb FROM Memory_Blocks "
        "WHERE machine_id=%s AND state='ALLOCATED' ORDER BY start_mb",
        (CLUSTER,),
    ).fetchall()
    assert [start for _bid, start in blocks] == [0, 4096, 8192, 12288]
    moved_block = blocks[3][0]

    # Free the two middle blocks. CoalesceFreeBlocks merges them into one
    # 8 GB hole, but the two ALLOCATED blocks are still far apart.
    for jid in (jobs[1], jobs[2]):
        _release(conn, jid)

    before = defrag.cluster_status(conn, CLUSTER)
    assert before["holes"] == 2
    assert before["largest_free_mb"] == 49152
    assert float(before["frag_index"]) > 0

    plan = defrag.defrag_plan(conn, CLUSTER)
    assert len(plan) == 1, plan
    move = plan[0]
    assert move["block_id"] == moved_block
    assert move["from_start_mb"] == 12288
    assert move["to_start_mb"] == 4096
    assert move["size_mb"] == 4096

    with conn.transaction():
        event_id = defrag.apply_defrag(
            conn, CLUSTER, plan, triggered_by="MANUAL", reason="migration accuracy"
        )

    # The recorded move is exactly the migration that happened.
    recorded = conn.execute(
        "SELECT block_id, from_start_mb, to_start_mb, size_mb, seq "
        "FROM Defrag_Moves WHERE event_id=%s ORDER BY seq",
        (event_id,),
    ).fetchall()
    assert recorded == [(moved_block, 12288, 4096, 4096, 1)], recorded

    landed = conn.execute(
        "SELECT start_mb, size_mb, job_id FROM Memory_Blocks WHERE block_id=%s",
        (moved_block,),
    ).fetchone()
    assert landed == (4096, 4096, jobs[3]), landed

    event = conn.execute(
        "SELECT status, holes_before, holes_after, largest_before_mb, "
        "largest_after_mb, frag_before, frag_after, blocks_moved, mb_moved "
        "FROM Defrag_Events WHERE event_id=%s",
        (event_id,),
    ).fetchone()
    assert event[0] == "COMMITTED"
    assert (event[1], event[2]) == (2, 1)
    assert (event[3], event[4]) == (49152, 57344)
    assert float(event[5]) > float(event[6]) == 0
    assert (event[7], event[8]) == (1, 4096)

    after = defrag.cluster_status(conn, CLUSTER)
    assert after["holes"] == 1
    assert after["largest_free_mb"] == 57344

    conn.execute("SELECT ValidateBlockMap(%s)", (CLUSTER,))


def test_defrag_creates_a_hole_big_enough_for_the_next_job(conn):
    """The on-demand path: FRAGMENTED -> compact -> the same job now fits."""
    db.reset_all()

    jobs = []
    for _ in range(3):
        jid = _job(conn, 20480)
        _allocate(conn, jid, 20480)
        jobs.append(jid)
    _release(conn, jobs[1])

    before = defrag.cluster_status(conn, CLUSTER)
    assert before["holes"] == 2
    assert before["largest_free_mb"] == 20480

    big = _job(conn, 24576)
    try:
        _allocate(conn, big, 24576)
    except psycopg.errors.RaiseException as exc:
        assert "FRAGMENTED" in str(exc), exc
    else:
        raise AssertionError("aggregate free RAM exists, so a fragmented cluster must refuse")

    with conn.transaction():
        plan = defrag.defrag_plan(conn, CLUSTER)
        defrag.apply_defrag(conn, CLUSTER, plan, triggered_by="AUTO", reason="on-demand retry")

    after = defrag.cluster_status(conn, CLUSTER)
    assert after["holes"] == 1
    assert after["largest_free_mb"] >= 24576

    _allocate(conn, big, 24576)  # must now succeed
    conn.execute("SELECT ValidateBlockMap(%s)", (CLUSTER,))


def test_defrag_is_idempotent_on_an_unfragmented_cluster(conn):
    """No scattered blocks -> no moves -> the map is left untouched."""
    db.reset_all()

    with conn.transaction():
        plan = defrag.defrag_plan(conn, CLUSTER)
        assert plan == []
        defrag.apply_defrag(conn, CLUSTER, plan)

    status = defrag.cluster_status(conn, CLUSTER)
    assert status["holes"] == 1
    conn.execute("SELECT ValidateBlockMap(%s)", (CLUSTER,))
