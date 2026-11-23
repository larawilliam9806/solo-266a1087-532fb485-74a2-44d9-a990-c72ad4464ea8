"""Actual-copy reports (实拷回报) and reconciliation.

After the lab prints a batch, the keeper uploads, on the order page, a CSV of
archive intervals that were actually copied: ``原卷号,档案起帧,帧数``.

Rules implemented here
----------------------
* Every reported row must fall inside the archive ranges **this order**
  arranged as new copies (``order_copies.is_new = 1``).  A row may span
  adjacent new-copy pieces, but one out-of-range row rejects the whole batch.
* A report batch is all-or-nothing: validation and storage run in one
  ``BEGIN IMMEDIATE`` transaction; any failure rolls everything back, so a
  failed upload has no effect at all.
* Batches may be submitted out of order, overlapping or concurrently.  The
  canonical ``order_confirmations`` table stores their *union* as disjoint
  archive intervals, so re-reporting already confirmed frames adds no new
  reconciliation records.
* Reused frames are confirmed by the order that first copied them.  A shot is
  ready for pickup only when every archive frame it reads is confirmed by the
  corresponding first-copy order's reports.
* Everything is interval arithmetic on half-open ranges
  ``[arch_start, arch_start + frame_count)``; individual frames are never
  expanded, regardless of frame counts.
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .core import (
    MAX_FRAME,
    MAX_ROWS,
    SubmissionError,
    clean_identifier,
)
from .db import connect


class OrderNotFoundError(SubmissionError):
    """The report references an order number that does not exist."""


@dataclass(frozen=True)
class ReportRow:
    row_index: int
    orig_reel: str
    arch_start: int
    frame_count: int

    @property
    def arch_end(self) -> int:
        return self.arch_start + self.frame_count


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _merge(intervals) -> list[tuple[int, int]]:
    """Union of half-open ``(start, end)`` intervals, sorted and disjoint."""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _subtract(ranges, holes) -> list[tuple[int, int]]:
    """Sorted disjoint ``ranges`` minus the union of sorted disjoint ``holes``."""
    result: list[tuple[int, int]] = []
    hole_starts = [h[0] for h in holes]
    for start, end in ranges:
        cursor = start
        idx = bisect.bisect_right(hole_starts, cursor)
        if idx > 0 and holes[idx - 1][1] > cursor:
            idx -= 1
        j = idx
        while j < len(holes):
            h_start, h_end = holes[j]
            if h_start >= end:
                break
            if h_end > cursor:
                lo = max(h_start, cursor)
                hi = min(h_end, end)
                if lo > cursor:
                    result.append((cursor, lo))
                cursor = max(cursor, hi)
                if cursor >= end:
                    break
            j += 1
        if cursor < end:
            result.append((cursor, end))
    return result


def _contained(start: int, end: int, ranges) -> bool:
    """True when the whole ``[start, end)`` lies inside one ``ranges`` entry."""
    idx = bisect.bisect_right([r[0] for r in ranges], start)
    if idx == 0:
        return False
    r_start, r_end = ranges[idx - 1]
    return r_start <= start and end <= r_end


def validate_report_rows(rows) -> list[ReportRow]:
    if not isinstance(rows, list) or not rows:
        raise SubmissionError("实拷清单为空，至少需要一行实拷记录")
    if len(rows) > MAX_ROWS:
        raise SubmissionError(f"实拷清单行数过多（最多 {MAX_ROWS} 行）")
    cleaned: list[ReportRow] = []
    problems: list[str] = []
    for row in rows:
        try:
            if isinstance(row, ReportRow):
                cleaned.append(row)
                continue
            row_index = int(row["row_index"])
            orig_reel = clean_identifier(
                row["orig_reel"], f"第 {row['row_index']} 行原卷号"
            )
            arch_start = int(row["arch_start"])
            frame_count = int(row["frame_count"])
            if arch_start < 0:
                raise SubmissionError(f"第 {row['row_index']} 行档案起帧不能为负数")
            if frame_count <= 0:
                raise SubmissionError(f"第 {row['row_index']} 行帧数必须大于 0")
            if arch_start + frame_count > MAX_FRAME:
                raise SubmissionError(f"第 {row['row_index']} 行帧段超出允许范围")
            cleaned.append(
                ReportRow(row_index, orig_reel, arch_start, frame_count)
            )
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"第 {row.get('row_index', '?')} 行格式不正确: {exc}")
        except SubmissionError as exc:
            problems.extend(exc.messages)
    if problems:
        raise SubmissionError(problems)
    return cleaned


def _new_copy_arch_ranges(
    conn: sqlite3.Connection, order_id: int
) -> dict[str, list[tuple[int, int]]]:
    """Archive intervals this order itself created (its new copies)."""
    rows = conn.execute(
        "SELECT orig_reel, arch_start, frame_count FROM order_copies "
        "WHERE order_id = ? AND is_new = 1 ORDER BY orig_reel, arch_start",
        (order_id,),
    ).fetchall()
    by_reel: dict[str, list[tuple[int, int]]] = {}
    for row in rows:
        by_reel.setdefault(row["orig_reel"], []).append(
            (row["arch_start"], row["arch_start"] + row["frame_count"])
        )
    return {reel: _merge(intervals) for reel, intervals in by_reel.items()}


def submit_report(
    db_path: str | Path,
    order_no: str,
    rows,
) -> dict:
    """Store one actual-copy report batch atomically; return the order report.

    The whole batch is rejected (no rows take effect) if any reported interval
    lies outside the order's own new-copy archive ranges.
    """
    order_no = clean_identifier(order_no, "单号")
    rows = validate_report_rows(rows)

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            order = conn.execute(
                "SELECT id, order_no FROM orders WHERE order_no = ?", (order_no,)
            ).fetchone()
            if order is None:
                raise OrderNotFoundError(f"单号 {order_no} 不存在，无法回报实拷")
            order_id = order["id"]
            new_ranges = _new_copy_arch_ranges(conn, order_id)

            problems: list[str] = []
            for row in rows:
                reel_ranges = new_ranges.get(row.orig_reel)
                if not reel_ranges:
                    problems.append(
                        f"第 {row.row_index} 行原卷 {row.orig_reel} "
                        "不在本单安排新拷的档案帧段内"
                    )
                elif not _contained(
                    row.arch_start, row.arch_end, reel_ranges
                ):
                    problems.append(
                        f"第 {row.row_index} 行档案帧段 "
                        f"[{row.arch_start}, {row.arch_end}) 越界："
                        "只能确认本单安排新拷的帧"
                    )
            if problems:
                raise SubmissionError(problems)

            cur = conn.execute(
                "INSERT INTO copy_reports (order_id, created_at, row_count) "
                "VALUES (?, ?, ?)",
                (order_id, _now(), len(rows)),
            )
            report_id = cur.lastrowid
            for row in rows:
                conn.execute(
                    "INSERT INTO copy_report_rows "
                    "(report_id, row_index, orig_reel, arch_start, frame_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        report_id,
                        row.row_index,
                        row.orig_reel,
                        row.arch_start,
                        row.frame_count,
                    ),
                )

            # Fold the batch into the canonical per-order union.  Ranges that
            # were already confirmed are silently absorbed: re-reporting a
            # confirmed frame inserts no new reconciliation record.
            existing_rows = conn.execute(
                "SELECT orig_reel, arch_start, frame_count "
                "FROM order_confirmations WHERE order_id = ?",
                (order_id,),
            ).fetchall()
            existing: dict[str, list[tuple[int, int]]] = {}
            for row in existing_rows:
                existing.setdefault(row["orig_reel"], []).append(
                    (row["arch_start"], row["arch_start"] + row["frame_count"])
                )
            reported: dict[str, list[tuple[int, int]]] = {}
            for row in rows:
                reported.setdefault(row.orig_reel, []).append(
                    (row.arch_start, row.arch_end)
                )
            for reel, intervals in reported.items():
                before = _merge(existing.get(reel, []))
                after = _merge(before + intervals)
                for add_start, add_end in _subtract(after, before):
                    conn.execute(
                        "INSERT INTO order_confirmations "
                        "(order_id, orig_reel, arch_start, frame_count) "
                        "VALUES (?, ?, ?, ?)",
                        (order_id, reel, add_start, add_end - add_start),
                    )

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    # Imported lazily to avoid an import cycle (core imports this module only
    # when it enriches an order report).
    from .core import get_order

    report = get_order(db_path, order_no)
    if report is None:  # pragma: no cover - committed rows must be visible
        raise RuntimeError("order vanished after report commit")
    return report


def _confirmation_unions(
    conn: sqlite3.Connection, order_ids
) -> dict[int, dict[str, list[tuple[int, int]]]]:
    """Confirmed archive intervals keyed by order id, then original reel."""
    ids = list(dict.fromkeys(order_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT order_id, orig_reel, arch_start, frame_count "
        f"FROM order_confirmations WHERE order_id IN ({placeholders})",
        ids,
    ).fetchall()
    grouped: dict[int, dict[str, list[tuple[int, int]]]] = {}
    for row in rows:
        grouped.setdefault(row["order_id"], {}).setdefault(
            row["orig_reel"], []
        ).append((row["arch_start"], row["arch_start"] + row["frame_count"]))
    return {
        order_id: {
            reel: _merge(intervals) for reel, intervals in reels.items()
        }
        for order_id, reels in grouped.items()
    }


def _archive_segments_window(
    conn: sqlite3.Connection, orig_reel: str, start: int, end: int
):
    """Coverage-index pieces mapping archive ``[start, end)`` of one reel."""
    return conn.execute(
        "SELECT s.orig_start, s.frame_count, s.arch_start, "
        "       s.created_order_id, o.order_no AS created_order_no "
        "FROM archive_segments s "
        "JOIN reels r ON r.id = s.reel_id "
        "LEFT JOIN orders o ON o.id = s.created_order_id "
        "WHERE r.orig_reel = ? "
        "  AND s.arch_start < ? AND s.arch_start + s.frame_count > ? "
        "ORDER BY s.arch_start",
        (orig_reel, end, start),
    ).fetchall()


def enrich_order_report(
    conn: sqlite3.Connection, report: dict, order_id: int
) -> dict:
    """Add reconciliation information to an order report in place.

    Adds, per shot, the still-missing archive frame intervals together with
    the first-copy order that owns each of them, and a ``ready_for_pickup``
    flag.  Also adds the order-level new-copy confirmation summary and the
    history of accepted report batches.  All reads run on ``conn`` so one
    query always sees one committed snapshot.
    """
    # 1. This order's own new-copy ranges and canonical confirmations.
    new_ranges = _new_copy_arch_ranges(conn, order_id)
    own_conf_rows = conn.execute(
        "SELECT orig_reel, arch_start, frame_count FROM order_confirmations "
        "WHERE order_id = ? ORDER BY orig_reel, arch_start",
        (order_id,),
    ).fetchall()
    own_conf: dict[str, list[tuple[int, int]]] = {}
    for row in own_conf_rows:
        own_conf.setdefault(row["orig_reel"], []).append(
            (row["arch_start"], row["arch_start"] + row["frame_count"])
        )
    own_conf = {
        reel: _merge(intervals) for reel, intervals in own_conf.items()
    }

    # 2. First-copy order of every archive segment the shots read.
    needed_windows: dict[str, list[int]] = {}
    for shot in report["shots"]:
        for seg in shot["segments"]:
            window = needed_windows.setdefault(
                shot["orig_reel"],
                [seg["arch_start"], seg["arch_end"]],
            )
            window[0] = min(window[0], seg["arch_start"])
            window[1] = max(window[1], seg["arch_end"])

    creator_ids: set[int] = set()
    segment_cache: dict[str, list] = {}
    for reel, (win_start, win_end) in needed_windows.items():
        pieces = _archive_segments_window(conn, reel, win_start, win_end)
        segment_cache[reel] = pieces
        for piece in pieces:
            if piece["created_order_id"] is not None:
                creator_ids.add(piece["created_order_id"])

    # 3. Confirmation unions of every first-copy order involved.
    unions = _confirmation_unions(conn, creator_ids)

    # 4. Per-shot missing archive intervals, annotated with the first-copy
    #    order that has to report them.  A coverage segment may itself be only
    #    partly confirmed, so subtract the first-copy order's confirmed union
    #    from the needed range instead of treating the segment as all-or-nothing.
    all_ready = True
    for shot in report["shots"]:
        missing: list[dict] = []
        for seg in shot["segments"]:
            reel = shot["orig_reel"]
            need_start, need_end = seg["arch_start"], seg["arch_end"]
            cursor = need_start
            for piece in segment_cache.get(reel, ()):
                p_arch = piece["arch_start"]
                p_end = p_arch + piece["frame_count"]
                if p_end <= cursor:
                    continue
                if p_arch >= need_end:
                    break
                lo = max(cursor, p_arch)
                hi = min(need_end, p_end)
                if hi <= lo:
                    continue
                creator_id = piece["created_order_id"]
                confirmed = unions.get(creator_id, {}).get(reel, [])
                for miss_start, miss_end in _subtract([(lo, hi)], confirmed):
                    offset = miss_start - p_arch
                    missing.append(
                        {
                            "orig_reel": reel,
                            "arch_start": miss_start,
                            "arch_end": miss_end,
                            "frame_count": miss_end - miss_start,
                            "orig_start": piece["orig_start"] + offset,
                            "orig_end": piece["orig_start"]
                            + offset
                            + (miss_end - miss_start),
                            "first_copy_order_id": creator_id,
                            "first_copy_order_no": piece["created_order_no"],
                        }
                    )
                cursor = hi
                if cursor >= need_end:
                    break

        # Coalesce neighbours that stay contiguous and share one first-copy
        # order, so the page lists frame segments rather than internal pieces.
        merged_missing: list[dict] = []
        for item in missing:
            if merged_missing:
                prev = merged_missing[-1]
                if (
                    prev["orig_reel"] == item["orig_reel"]
                    and prev["arch_end"] == item["arch_start"]
                    and prev["orig_end"] == item["orig_start"]
                    and prev["first_copy_order_id"]
                    == item["first_copy_order_id"]
                ):
                    prev["arch_end"] = item["arch_end"]
                    prev["orig_end"] = item["orig_end"]
                    prev["frame_count"] = prev["arch_end"] - prev["arch_start"]
                    continue
            merged_missing.append(dict(item))
        shot["missing_segments"] = merged_missing
        shot["ready_for_pickup"] = not merged_missing
        all_ready = all_ready and shot["ready_for_pickup"]

    report["ready_for_pickup"] = all_ready

    # 5. Order-level summary of this order's own new-copy confirmation.
    summary = []
    for reel in sorted(new_ranges):
        new = new_ranges[reel]
        confirmed = own_conf.get(reel, [])
        missing_ranges = _subtract(new, confirmed)
        summary.append(
            {
                "orig_reel": reel,
                "new_ranges": [
                    {"arch_start": s, "arch_end": e, "frame_count": e - s}
                    for s, e in new
                ],
                "confirmed_ranges": [
                    {"arch_start": s, "arch_end": e, "frame_count": e - s}
                    for s, e in confirmed
                ],
                "missing_ranges": [
                    {"arch_start": s, "arch_end": e, "frame_count": e - s}
                    for s, e in missing_ranges
                ],
                "fully_confirmed": not missing_ranges,
            }
        )
    report["new_copy_confirmation"] = summary

    # 6. History of accepted report batches (raw uploads, newest first).
    batch_rows = conn.execute(
        "SELECT id, created_at, row_count FROM copy_reports "
        "WHERE order_id = ? ORDER BY id DESC",
        (order_id,),
    ).fetchall()
    batches = []
    for batch in batch_rows:
        raw_rows = conn.execute(
            "SELECT row_index, orig_reel, arch_start, frame_count "
            "FROM copy_report_rows WHERE report_id = ? ORDER BY row_index",
            (batch["id"],),
        ).fetchall()
        batches.append(
            {
                "id": batch["id"],
                "created_at": batch["created_at"],
                "row_count": batch["row_count"],
                "rows": [
                    {
                        "row_index": r["row_index"],
                        "orig_reel": r["orig_reel"],
                        "arch_start": r["arch_start"],
                        "arch_end": r["arch_start"] + r["frame_count"],
                        "frame_count": r["frame_count"],
                    }
                    for r in raw_rows
                ],
            }
        )
    report["copy_reports"] = batches
    return report
