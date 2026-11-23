"""Tests for actual-copy report CSV parsing."""

import pytest

from archive.core import SubmissionError
from archive.csv_import import parse_report_csv

HEADER = "原卷号,档案起帧,帧数\n"


def test_parse_basic_report():
    rows = parse_report_csv((HEADER + "R001,0,120\nR002,7,30\n").encode("utf-8"))
    assert rows == [
        {"row_index": 1, "orig_reel": "R001", "arch_start": 0, "frame_count": 120},
        {"row_index": 2, "orig_reel": "R002", "arch_start": 7, "frame_count": 30},
    ]


def test_report_utf8_bom_and_gb18030():
    for encoding in ("utf-8-sig", "gb18030"):
        rows = parse_report_csv((HEADER + "R1,7,8\n").encode(encoding))
        assert rows[0]["arch_start"] == 7


def test_report_header_aliases():
    text = "reel,archive_start,frames\nR1,10,5\n"
    assert parse_report_csv(text)[0] == {
        "row_index": 1,
        "orig_reel": "R1",
        "arch_start": 10,
        "frame_count": 5,
    }


def test_report_blank_lines_and_row_index_compacted():
    text = HEADER + "\nR1,0,10\n\nR2,10,10\n  \n"
    assert [r["row_index"] for r in parse_report_csv(text)] == [1, 2]


@pytest.mark.parametrize(
    "text,needle",
    [
        ("", "为空"),
        ("原卷号,档案起帧\nR1,10\n", "缺少列"),
        (HEADER + "R1,abc,10\n", "不是整数"),
        (HEADER + "R1,-1,10\n", "不能为负数"),
        (HEADER + "R1,0,0\n", "帧数必须大于 0"),
        (HEADER + "\n", "没有有效"),
        (HEADER + "R1,0\n", "列数不足"),
    ],
)
def test_invalid_report_csv_rejected(text, needle):
    with pytest.raises(SubmissionError) as exc:
        parse_report_csv(text)
    assert any(needle in m for m in exc.value.messages)


def test_report_crlf_and_quoted_fields():
    text = '原卷号,档案起帧,帧数\r\n"R,001",2,6\r\n'
    rows = parse_report_csv(text)
    assert rows[0]["orig_reel"] == "R,001"
