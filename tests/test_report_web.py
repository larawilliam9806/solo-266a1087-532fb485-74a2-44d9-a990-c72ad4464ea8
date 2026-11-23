"""HTTP tests for the actual-copy report workflow."""

import io

import pytest

from archive.app import create_app
from archive.db import init_db

EDIT_HEADER = "镜头号,原卷号,起帧,帧数\n"
REPORT_HEADER = "原卷号,档案起帧,帧数\n"


@pytest.fixture()
def app(tmp_path):
    db = tmp_path / "web.sqlite3"
    init_db(db)
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(db),
            "SECRET_KEY": "test-secret",
        }
    )
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf(client):
    client.get("/")
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def submit_edit(client, order_no, csv_text):
    token = csrf(client)
    return client.post(
        "/submit",
        data={
            "csrf_token": token,
            "order_no": order_no,
            "film_no": "FILM-A",
            "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "edit.csv"),
        },
        content_type="multipart/form-data",
    )


def submit_report(client, order_no, csv_text, filename="report.csv"):
    token = csrf(client)
    return client.post(
        f"/orders/{order_no}/reports",
        data={
            "csrf_token": token,
            "report_file": (io.BytesIO(csv_text.encode("utf-8")), filename),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


@pytest.fixture()
def one_order(client):
    resp = submit_edit(
        client, "ORD-1", EDIT_HEADER + "S01,R001,0,100\n"
    )
    assert resp.status_code == 302
    return "ORD-1"


def test_order_page_shows_report_form_and_pending_state(client, one_order):
    page = client.get(f"/orders/{one_order}")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "实拷回报" in body
    assert "暂不可取片" in body
    assert "首拷单号" in body


def test_successful_report_redirects_and_marks_ready(client, one_order):
    resp = submit_report(client, one_order, REPORT_HEADER + "R001,0,100\n")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/orders/{one_order}")

    page = client.get(f"/orders/{one_order}")
    assert "可取片" in page.get_data(as_text=True)

    data = client.get(f"/api/orders/{one_order}").get_json()
    assert data["ready_for_pickup"] is True
    assert data["shots"][0]["missing_segments"] == []
    assert data["copy_reports"][0]["row_count"] == 1


def test_out_of_range_report_returns_400_and_changes_nothing(client, one_order):
    resp = submit_report(client, one_order, REPORT_HEADER + "R001,90,20\n")
    assert resp.status_code == 400
    assert "越界" in resp.get_data(as_text=True)

    data = client.get(f"/api/orders/{one_order}").get_json()
    assert data["copy_reports"] == []
    assert data["ready_for_pickup"] is False


def test_missing_report_file_rejected(client, one_order):
    token = csrf(client)
    resp = client.post(
        f"/orders/{one_order}/reports",
        data={"csrf_token": token},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "请选择" in resp.get_data(as_text=True)


def test_report_unknown_order_404(client):
    resp = submit_report(client, "GHOST", REPORT_HEADER + "R1,0,1\n")
    assert resp.status_code == 404


def test_report_requires_csrf(client, one_order):
    resp = client.post(
        f"/orders/{one_order}/reports",
        data={
            "csrf_token": "wrong",
            "report_file": (io.BytesIO(b"x"), "r.csv"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_overlapping_batches_eventually_ready(client, one_order):
    assert submit_report(
        client, one_order, REPORT_HEADER + "R001,0,60\n"
    ).status_code == 302
    assert submit_report(
        client, one_order, REPORT_HEADER + "R001,40,60\n"
    ).status_code == 302
    data = client.get(f"/api/orders/{one_order}").get_json()
    assert data["ready_for_pickup"] is True
    assert len(data["copy_reports"]) == 2
    # Canonical union stays a single interval.
    assert data["new_copy_confirmation"][0]["confirmed_ranges"] == [
        {"arch_start": 0, "arch_end": 100, "frame_count": 100}
    ]


def test_reuse_chain_waits_for_first_copy_order(client):
    submit_edit(client, "O1", EDIT_HEADER + "S01,R1,0,100\n")
    submit_edit(client, "O2", EDIT_HEADER + "S01,R1,50,100\n")

    # O2 reports only its own new archive part 100..150.
    resp = submit_report(client, "O2", REPORT_HEADER + "R1,100,50\n")
    assert resp.status_code == 302
    data = client.get("/api/orders/O2").get_json()
    assert data["ready_for_pickup"] is False
    miss = data["shots"][0]["missing_segments"]
    assert [(m["arch_start"], m["arch_end"], m["first_copy_order_no"]) for m in miss] == [
        (50, 100, "O1"),
    ]

    # The missing segment on the page links to the first-copy order.
    page = client.get("/orders/O2").get_data(as_text=True)
    assert "/orders/O1" in page

    submit_report(client, "O1", REPORT_HEADER + "R1,0,100\n")
    data = client.get("/api/orders/O2").get_json()
    assert data["ready_for_pickup"] is True


def test_original_manifest_and_reel_pages_still_work(client, one_order):
    submit_report(client, one_order, REPORT_HEADER + "R001,0,50\n")
    # Existing views remain available and unchanged in shape.
    order = client.get(f"/api/orders/{one_order}").get_json()
    assert [c["arch_start"] for c in order["copies"]] == [0]
    reel = client.get("/api/reels/R001").get_json()
    assert reel["next_frame"] == 100
    assert client.get("/reels/R001").status_code == 200
    assert client.get("/").status_code == 200


def test_report_during_new_submission_keeps_other_order_intact(client):
    submit_edit(client, "O1", EDIT_HEADER + "S01,R1,0,100\n")
    submit_report(client, "O1", REPORT_HEADER + "R1,0,100\n")
    submit_edit(client, "O2", EDIT_HEADER + "S01,R1,200,10\n")

    o1 = client.get("/api/orders/O1").get_json()
    o2 = client.get("/api/orders/O2").get_json()
    assert o1["ready_for_pickup"] is True
    assert o2["ready_for_pickup"] is False
    # O2's new submission did not alter O1's stored confirmations.
    assert o1["new_copy_confirmation"][0]["fully_confirmed"] is True
