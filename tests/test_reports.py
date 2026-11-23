"""Tests for actual-copy reports and reconciliation."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from archive.core import get_order, reel_state, submit_order  # noqa: E402
from archive.csv_import import parse_report_csv  # noqa: E402
from archive.db import connect, init_db  # noqa: E402
from archive.reports import (  # noqa: E402
    OrderNotFoundError,
    SubmissionError,
    submit_report,
)


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "archive.sqlite3"
    init_db(path)
    return path


def edit_row(index, start, count, reel="R1", shot=None):
    return {
        "row_index": index,
        "shot_no": shot or f"S{index}",
        "orig_reel": reel,
        "orig_start": start,
        "frame_count": count,
    }


def report(reel, start, count, index=1):
    return {
        "row_index": index,
        "orig_reel": reel,
        "arch_start": start,
        "frame_count": count,
    }


REPORT_HEADER = "原卷号,档案起帧,帧数\n"


def csv_report(*triples):
    lines = [REPORT_HEADER]
    for reel, start, count in triples:
        lines.append(f"{reel},{start},{count}\n")
    return "".join(lines)


def confirmations(db, order_no):
    rep = get_order(db, order_no)
    return [
        (item["orig_reel"], r["arch_start"], r["arch_end"])
        for item in rep["new_copy_confirmation"]
        for r in item["confirmed_ranges"]
    ]


def test_shot_not_ready_before_any_report(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    rep = get_order(db, "O1")
    assert rep["ready_for_pickup"] is False
    assert rep["shots"][0]["ready_for_pickup"] is False
    miss = rep["shots"][0]["missing_segments"]
    assert [(m["arch_start"], m["arch_end"]) for m in miss] == [(0, 100)]
    assert miss[0]["first_copy_order_no"] == "O1"


def test_full_report_marks_shot_ready(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    submit_report(db, "O1", [report("R1", 0, 100)])
    rep = get_order(db, "O1")
    assert rep["ready_for_pickup"] is True
    assert rep["shots"][0]["missing_segments"] == []
    assert rep["new_copy_confirmation"][0]["fully_confirmed"] is True
    assert len(rep["copy_reports"]) == 1


def test_partial_report_then_completion(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 40))))
    rep = get_order(db, "O1")
    miss = rep["shots"][0]["missing_segments"]
    assert [(m["arch_start"], m["arch_end"]) for m in miss] == [(40, 100)]
    assert rep["ready_for_pickup"] is False

    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 40, 60))))
    rep = get_order(db, "O1")
    assert rep["ready_for_pickup"] is True
    assert rep["shots"][0]["missing_segments"] == []


def test_report_row_may_span_adjacent_new_copy_pieces(db):
    # O2 needs two new-copy pieces separated by already-covered frames:
    # 120..150 reuses O1's archive, so the two new gaps stay distinct pieces.
    submit_order(db, "O1", "F", [edit_row(1, 0, 150)])
    submit_order(
        db,
        "O2",
        "F",
        [edit_row(1, 150, 30), edit_row(2, 300, 20)],
    )
    rep2 = get_order(db, "O2")
    pieces = [
        (c["orig_start"], c["orig_end"], c["arch_start"], c["arch_end"])
        for c in rep2["copies"]
        if c["is_new"]
    ]
    assert len(pieces) == 2  # two new gaps stay separate in the manifest
    arch_lo = min(p[2] for p in pieces)
    arch_hi = max(p[3] for p in pieces)
    assert arch_hi - arch_lo == 50

    # One CSV row spanning both new pieces is legal because together they form
    # the order's new-copy archive range.
    submit_report(
        db,
        "O2",
        parse_report_csv(csv_report(("R1", arch_lo, arch_hi - arch_lo))),
    )
    assert get_order(db, "O2")["ready_for_pickup"] is True


def test_out_of_range_row_rejects_whole_batch(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    with pytest.raises(SubmissionError) as exc:
        submit_report(
            db,
            "O1",
            [report("R1", 0, 50), report("R1", 90, 20)],  # second ends at 110
        )
    assert any("越界" in m for m in exc.value.messages)

    rep = get_order(db, "O1")
    assert rep["copy_reports"] == []
    assert confirmations(db, "O1") == []
    assert rep["ready_for_pickup"] is False


def test_report_for_unknown_order_raises(db):
    with pytest.raises(OrderNotFoundError):
        submit_report(db, "NOPE", [report("R1", 0, 10)])


def test_report_reel_without_new_copy_rejected(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    # Reuse-only order: no new archive frames of its own.
    submit_order(db, "O2", "F", [edit_row(1, 0, 10)])
    with pytest.raises(SubmissionError) as exc:
        submit_report(db, "O2", [report("R1", 0, 10)])
    assert any("不在本单安排新拷" in m for m in exc.value.messages)
    assert get_order(db, "O2")["copy_reports"] == []


def test_reused_frames_follow_first_copy_order_report(db):
    # O1 first-copies 0..100 at archive 0..100.
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    # O2 reuses 50..100 and new-copies 100..150 at archive 100..150.
    submit_order(db, "O2", "F", [edit_row(1, 50, 100)])

    # O2 confirming its own new part is not enough: reused 50..100 still open.
    submit_report(db, "O2", parse_report_csv(csv_report(("R1", 100, 50))))
    rep2 = get_order(db, "O2")
    miss = rep2["shots"][0]["missing_segments"]
    assert [(m["arch_start"], m["arch_end"], m["first_copy_order_no"]) for m in miss] == [
        (50, 100, "O1"),
    ]
    assert rep2["ready_for_pickup"] is False

    # Once the first-copy order reports, the reused frames count as confirmed.
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 100))))
    rep2 = get_order(db, "O2")
    assert rep2["shots"][0]["missing_segments"] == []
    assert rep2["ready_for_pickup"] is True


def test_overlapping_batches_take_union_and_keep_canonical_rows(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 60))))
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 40, 60))))  # overlap 40..60
    rep = get_order(db, "O1")
    assert [(r["arch_start"], r["arch_end"])
            for r in rep["new_copy_confirmation"][0]["confirmed_ranges"]] == [(0, 100)]
    assert rep["ready_for_pickup"] is True
    # Two raw batches kept for audit, but canonical union stays one interval.
    assert len(rep["copy_reports"]) == 2
    assert confirmations(db, "O1") == [("R1", 0, 100)]


def test_duplicate_report_adds_no_canonical_record(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 100))))
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 100))))
    # Second batch is stored as an upload but creates no new canonical range.
    assert confirmations(db, "O1") == [("R1", 0, 100)]
    assert len(get_order(db, "O1")["copy_reports"]) == 2


def test_out_of_order_batches_merge(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 60, 40))))
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 30))))
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 30, 30))))
    assert confirmations(db, "O1") == [("R1", 0, 100)]
    assert get_order(db, "O1")["ready_for_pickup"] is True


def test_reports_do_not_touch_other_orders_or_reels(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100, reel="R1")])
    submit_order(db, "O2", "F", [edit_row(1, 0, 5, reel="R2")])
    before = reel_state(db, "R1")
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 100))))
    # Reel tails and coverage index are untouched by reporting.
    assert reel_state(db, "R1") == before
    assert get_order(db, "O2")["ready_for_pickup"] is False
    submit_report(db, "O2", parse_report_csv(csv_report(("R2", 0, 5))))
    assert get_order(db, "O2")["ready_for_pickup"] is True


def test_multi_reel_report_validated_per_reel(db):
    submit_order(
        db,
        "O1",
        "F",
        [edit_row(1, 0, 10, reel="R1"), edit_row(2, 0, 7, reel="R2")],
    )
    submit_report(
        db,
        "O1",
        parse_report_csv(csv_report(("R1", 0, 10), ("R2", 0, 7))),
    )
    rep = get_order(db, "O1")
    assert rep["ready_for_pickup"] is True
    assert {item["orig_reel"] for item in rep["new_copy_confirmation"]} == {
        "R1",
        "R2",
    }

    # Unknown reel in a later batch rejects the whole batch.
    with pytest.raises(SubmissionError):
        submit_report(db, "O1", parse_report_csv(csv_report(("R3", 0, 1))))


def test_empty_report_rejected(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 10)])
    for payload in ("", REPORT_HEADER + "\n"):
        with pytest.raises(SubmissionError):
            submit_report(db, "O1", parse_report_csv(payload))


def test_huge_ranges_stay_interval_math(db):
    span = 50_000_000
    submit_order(db, "O1", "F", [edit_row(1, 0, span)])
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, span))))
    rep = get_order(db, "O1")
    assert rep["ready_for_pickup"] is True
    assert rep["new_copy_confirmation"][0]["confirmed_ranges"][0]["frame_count"] == span


def test_partial_confirmation_inside_one_coverage_segment(db):
    # One archive segment 0..100, only the middle 20..80 reported.
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 20, 60))))
    miss = get_order(db, "O1")["shots"][0]["missing_segments"]
    assert [(m["arch_start"], m["arch_end"]) for m in miss] == [
        (0, 20),
        (80, 100),
    ]


def test_concurrent_reports_serialize_to_union(db):
    from archive.db import connect

    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    # Overlapping batches submitted at the same instant.  Every frame ends up
    # confirmed and the canonical table stores one merged union interval.
    batches = [("R1", i * 5, 50) for i in range(11)]  # covers 0..100, overlaps
    results = []
    barrier = threading.Barrier(len(batches))

    def worker(chunk):
        barrier.wait()
        try:
            submit_report(db, "O1", parse_report_csv(csv_report(chunk)))
            results.append(("ok", chunk))
        except Exception as exc:  # noqa: BLE001
            results.append(("error", exc))

    threads = [threading.Thread(target=worker, args=(c,)) for c in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(kind == "ok" for kind, _ in results), results
    assert confirmations(db, "O1") == [("R1", 0, 100)]
    assert get_order(db, "O1")["ready_for_pickup"] is True
    # The canonical table is a disjoint partition: no frame twice, total 100.
    conn = connect(db)
    rows = conn.execute(
        "SELECT arch_start, frame_count FROM order_confirmations WHERE order_id = "
        "(SELECT id FROM orders WHERE order_no = 'O1') ORDER BY arch_start"
    ).fetchall()
    batches = conn.execute(
        "SELECT COUNT(*) c FROM copy_reports WHERE order_id = "
        "(SELECT id FROM orders WHERE order_no = 'O1')"
    ).fetchone()["c"]
    conn.close()
    total = sum(r["frame_count"] for r in rows)
    assert total == 100  # overlapping uploads never double-count a frame
    for prev, cur in zip(rows, rows[1:]):
        assert prev["arch_start"] + prev["frame_count"] <= cur["arch_start"]
    assert batches == 11


def test_concurrent_disjoint_batches_preserve_each_range(db):
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    chunks = [("R1", i * 10, 10) for i in range(10)]
    results = []
    barrier = threading.Barrier(len(chunks))

    def worker(chunk):
        barrier.wait()
        try:
            submit_report(db, "O1", parse_report_csv(csv_report(chunk)))
            results.append("ok")
        except Exception:  # noqa: BLE001
            results.append("error")

    threads = [threading.Thread(target=worker, args=(c,)) for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count("ok") == 10
    rep = get_order(db, "O1")
    assert rep["ready_for_pickup"] is True
    # Adjacent disjoint batches are stored as the union: one interval 0..100.
    assert confirmations(db, "O1") == [("R1", 0, 100)]
    # All ten raw batches remain auditable.
    assert len(rep["copy_reports"]) == 10


def test_concurrent_report_and_submission_do_not_block_each_other(db):
    # Reporting on O1 while a new order O2 is handed in: both commit, and
    # neither recomputes the other's data.
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    barrier = threading.Barrier(2)
    outcomes = []

    def reporter():
        barrier.wait()
        try:
            submit_report(db, "O1", parse_report_csv(csv_report(("R1", 0, 100))))
            outcomes.append(("report", "ok"))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("report", exc))

    def submitter():
        barrier.wait()
        try:
            submit_order(db, "O2", "F", [edit_row(1, 200, 10)])
            outcomes.append(("submit", "ok"))
        except Exception as exc:  # noqa: BLE001
            outcomes.append(("submit", exc))

    threads = [threading.Thread(target=reporter), threading.Thread(target=submitter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert dict(outcomes) == {"report": "ok", "submit": "ok"}, outcomes
    assert get_order(db, "O1")["ready_for_pickup"] is True
    assert get_order(db, "O2")["ready_for_pickup"] is False


def test_query_snapshot_is_stable_across_enrichment(db):
    # The report is computed from one read transaction; spot-check that the
    # JSON view stays internally consistent (missing + confirmed tile the
    # new-copy ranges).
    submit_order(db, "O1", "F", [edit_row(1, 0, 100)])
    submit_report(db, "O1", parse_report_csv(csv_report(("R1", 30, 40))))
    rep = get_order(db, "O1")
    item = rep["new_copy_confirmation"][0]
    new = [(r["arch_start"], r["arch_end"]) for r in item["new_ranges"]]
    conf = [(r["arch_start"], r["arch_end"]) for r in item["confirmed_ranges"]]
    miss = [(r["arch_start"], r["arch_end"]) for r in item["missing_ranges"]]
    assert new == [(0, 100)]
    assert conf == [(30, 70)]
    assert miss == [(0, 30), (70, 100)]
    assert rep["ready_for_pickup"] is False
