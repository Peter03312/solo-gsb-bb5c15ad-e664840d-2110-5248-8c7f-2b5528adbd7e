"""End-to-end tests for the read-only publish impact preview (只读影响预演).

``POST /points/{id}/rules/preview`` validates a would-be publication exactly
like a formal publish (structural rule checks, optimistic expected_version)
but answers from one committed snapshot at one server timestamp and changes
nothing: it diffs current vs proposed open hours from now through the
furthest bookable day and lists the non-cancelled reservations, valid locks
and waiting waitlist entries that would lose full continuous open coverage.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def parse(dt_string):
    return datetime.fromisoformat(dt_string.replace("Z", "+00:00"))


def at(hour, minute=0, day_offset=1):
    """UTC instant at ``hour:minute`` on the day ``day_offset`` days ahead."""
    day = (datetime.now(UTC) + timedelta(days=day_offset)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def create_point(client, capacity=None, tz="UTC"):
    body = {"name": "预演测试点", "timezone": tz}
    if capacity is not None:
        body["capacity"] = capacity
    r = client.post("/points", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def publish(client, pid, rules, expected=0):
    return client.post(
        f"/points/{pid}/rules",
        json={"expected_version": expected, "rules": rules},
    )


def preview(client, pid, rules, expected):
    return client.post(
        f"/points/{pid}/rules/preview",
        json={"expected_version": expected, "rules": rules},
    )


def open_all_week(client, start_time="08:00", end_time="20:00", capacity=None):
    """Point whose weekly rules cover ``start_time``-``end_time`` every day."""
    point = create_point(client, capacity=capacity)
    pid = point["id"]
    rules = {
        "weekly": [
            {"weekday": weekday, "start_time": start_time, "end_time": end_time}
            for weekday in range(1, 8)
        ],
        "closed_dates": [],
        "makeup": [],
    }
    r = publish(client, pid, rules, expected=0)
    assert r.status_code == 201, r.text
    return pid, rules


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def lock(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/locks",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def waitlist(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/waitlist",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def force_expire_lock(lock_id):
    """Push a lock past its confirmation deadline straight in the database."""
    from app import db

    with db.conn() as connection:
        with connection.transaction():
            connection.cursor().execute(
                "UPDATE slot_locks SET expires_at = now() - interval '1 second' "
                "WHERE id = %s",
                (lock_id,),
            )


def db_field(sql, params):
    from app import db

    with db.conn() as connection:
        with connection.transaction():
            row = connection.cursor().execute(sql, params).fetchone()
    return next(iter(row.values()))


def day_entry(body, local_date):
    """The diff entry of one local day, or None when the day has no changes."""
    for day in body["days"]:
        if day["local_date"] == local_date:
            return day
    return None


# ---------------------------------------------------------------------------
# Diff of the open hours
# ---------------------------------------------------------------------------


def test_preview_diffs_added_and_removed_intervals_per_local_day(client):
    pid, _rules = open_all_week(client, "09:00", "12:00")
    proposed = {
        "weekly": [
            {"weekday": weekday, "start_time": "10:00", "end_time": "13:00"}
            for weekday in range(1, 8)
        ],
        "closed_dates": [],
        "makeup": [],
    }
    before = datetime.now(UTC)
    r = preview(client, pid, proposed, expected=1)
    after = datetime.now(UTC)
    assert r.status_code == 200, r.text
    body = r.json()

    # The answer names the version it bases on, one server-side sampling
    # timestamp and the compared local-date range (today .. furthest day).
    assert body["point_id"] == pid
    assert body["base_version"] == 1
    assert body["timezone"] == "UTC"
    sampled_at = parse(body["sampled_at"])
    assert before - timedelta(seconds=5) <= sampled_at <= after + timedelta(seconds=5)
    today = datetime.now(UTC).date()
    assert body["date_from"] == today.isoformat()
    assert body["date_to"] == (today + timedelta(days=30)).isoformat()

    # Every listed day carries UTC half-open added/removed intervals; only
    # days with an actual difference appear.
    tomorrow = (today + timedelta(days=1)).isoformat()
    entry = day_entry(body, tomorrow)
    assert entry is not None
    assert entry["added"] == [
        {"start": f"{tomorrow}T12:00:00Z", "end": f"{tomorrow}T13:00:00Z"}
    ]
    assert entry["removed"] == [
        {"start": f"{tomorrow}T09:00:00Z", "end": f"{tomorrow}T10:00:00Z"}
    ]
    dates = [day["local_date"] for day in body["days"]]
    assert dates == sorted(set(dates))
    # Today drops out of the diff once its open hours are entirely past.
    assert len(dates) in (30, 31)
    assert body["affected_reservations"] == []
    assert body["affected_locks"] == []
    assert body["affected_waitlist"] == []


def test_preview_identical_rules_report_no_difference(client):
    pid, rules = open_all_week(client, "09:00", "12:00")
    r = preview(client, pid, rules, expected=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == []
    assert body["affected_reservations"] == []
    assert body["affected_locks"] == []
    assert body["affected_waitlist"] == []


def test_preview_source_label_change_is_not_a_time_difference(client):
    pid, _rules = open_all_week(client, "09:00", "12:00")
    relabelled = {
        "weekly": [
            {
                "weekday": weekday,
                "start_time": "09:00",
                "end_time": "12:00",
                "label": "上午场",
            }
            for weekday in range(1, 8)
        ],
        "closed_dates": [],
        "makeup": [],
    }
    r = preview(client, pid, relabelled, expected=1)
    assert r.status_code == 200, r.text
    assert r.json()["days"] == []


def test_preview_overnight_session_diff_follows_calendar_rules(client):
    point = create_point(client)
    pid = point["id"]
    target = (datetime.now(UTC) + timedelta(days=2)).date()
    rules = {
        "weekly": [
            {
                "weekday": target.isoweekday(),
                "start_time": "20:00",
                "end_time": "02:00",
            }
        ],
        "closed_dates": [],
        "makeup": [],
    }
    assert publish(client, pid, rules, expected=0).status_code == 201

    # Closing the next local day removes exactly the carried-over tail.
    proposed = {**rules, "closed_dates": [(target + timedelta(days=1)).isoformat()]}
    r = preview(client, pid, proposed, expected=1)
    assert r.status_code == 200, r.text
    body = r.json()
    # The session's first-day piece is untouched; only the tail day differs.
    assert day_entry(body, target.isoformat()) is None
    tail = day_entry(body, (target + timedelta(days=1)).isoformat())
    assert tail["added"] == []
    assert tail["removed"] == [
        {
            "start": f"{target + timedelta(days=1)}T00:00:00Z",
            "end": f"{target + timedelta(days=1)}T02:00:00Z",
        }
    ]


# ---------------------------------------------------------------------------
# Publish-like preconditions: structure, version, existence
# ---------------------------------------------------------------------------


def test_preview_version_conflict_returns_no_stale_preview(client):
    pid, rules = open_all_week(client, "09:00", "12:00")
    r = preview(client, pid, rules, expected=0)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "version_conflict"
    assert err["details"] == {"expected_version": 0, "current_version": 1}
    # The error envelope carries no preview content.
    assert "days" not in r.json()

    # After a real publish the previously seen version is stale as well.
    assert publish(client, pid, rules, expected=1).status_code == 201
    r = preview(client, pid, rules, expected=1)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["current_version"] == 2
    assert preview(client, pid, rules, expected=2).status_code == 200


def test_preview_invalid_rules_are_rejected_and_change_nothing(client):
    pid, rules = open_all_week(client, "09:00", "12:00")
    bad_payloads = [
        # weekly zero length
        {"weekly": [{"weekday": 2, "start_time": "10:00", "end_time": "10:00"}],
         "closed_dates": [], "makeup": []},
        # weekday out of range
        {"weekly": [{"weekday": 8, "start_time": "09:00", "end_time": "10:00"}],
         "closed_dates": [], "makeup": []},
        # bad wall-clock format
        {"weekly": [{"weekday": 2, "start_time": "25:00", "end_time": "26:00"}],
         "closed_dates": [], "makeup": []},
        # duplicate weekly rule
        {"weekly": [
            {"weekday": 2, "start_time": "09:00", "end_time": "10:00"},
            {"weekday": 2, "start_time": "09:00", "end_time": "10:00"},
        ], "closed_dates": [], "makeup": []},
        # makeup must be same-day
        {"weekly": [], "closed_dates": [], "makeup": [
            {"day": "2026-10-05", "start_time": "12:00", "end_time": "12:00"}
        ]},
    ]
    for bad in bad_payloads:
        r = preview(client, pid, bad, expected=1)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_period"

    # Structural problems (missing fields) are invalid_request.
    r = client.post(f"/points/{pid}/rules/preview", json={"expected_version": 1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"

    # Nothing was published: the point is still at version 1 with one version row.
    assert client.get(f"/points/{pid}").json()["version"] == 1
    assert len(client.get(f"/points/{pid}/rules/versions").json()) == 1


def test_preview_unknown_point_is_404(client):
    r = preview(
        client,
        99999999,
        {"weekly": [], "closed_dates": [], "makeup": []},
        expected=0,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "point_not_found"


def test_preview_on_unpublished_point_compares_against_empty_rules(client):
    point = create_point(client)
    pid = point["id"]
    proposed = {
        "weekly": [{"weekday": 1, "start_time": "09:00", "end_time": "12:00"}],
        "closed_dates": [],
        "makeup": [],
    }
    r = preview(client, pid, proposed, expected=0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_version"] == 0
    # With no current rules every proposed interval is purely added.
    assert body["days"] != []
    assert all(day["removed"] == [] for day in body["days"])
    mondays = [day for day in body["days"]]
    assert all(day["added"] for day in mondays)
    assert body["affected_reservations"] == []

    # A non-zero expected version conflicts against the empty history.
    r = preview(client, pid, proposed, expected=1)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["current_version"] == 0


# ---------------------------------------------------------------------------
# Affected bookings
# ---------------------------------------------------------------------------


def test_preview_lists_reservations_losing_full_coverage(client):
    pid, _rules = open_all_week(client, capacity=2)
    res1 = reserve(client, pid, "P-R1", at(10), at(11))
    assert res1.status_code == 201, res1.text
    res2 = reserve(client, pid, "P-R2", at(14), at(15))
    assert res2.status_code == 201, res2.text
    res3 = reserve(client, pid, "P-R3", at(10, 15), at(10, 45))
    assert res3.status_code == 201, res3.text
    # A cancelled reservation no longer blocks anything and is not listed.
    cancelled = client.post(
        f"/points/{pid}/reservations/{res3.json()['id']}/cancel",
        json={"cancel_token": res3.json()["cancel_token"]},
    )
    assert cancelled.status_code == 200

    proposed = {
        "weekly": [
            {"weekday": weekday, "start_time": "08:00", "end_time": "10:30"}
            for weekday in range(1, 8)
        ]
        + [
            {"weekday": weekday, "start_time": "11:00", "end_time": "20:00"}
            for weekday in range(1, 8)
        ],
        "closed_dates": [],
        "makeup": [],
    }
    r = preview(client, pid, proposed, expected=1)
    assert r.status_code == 200, r.text
    body = r.json()

    affected = body["affected_reservations"]
    assert [item["id"] for item in affected] == [res1.json()["id"]]
    item = affected[0]
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    assert item["start"] == f"{tomorrow}T10:00:00Z"
    assert item["end"] == f"{tomorrow}T11:00:00Z"
    assert item["rules_version"] == 1
    assert item["reason"] == "outside_open_hours"
    # Only the 10:30-11:00 slice falls outside the proposed open hours.
    assert item["uncovered"] == [
        {"start": f"{tomorrow}T10:30:00Z", "end": f"{tomorrow}T11:00:00Z"}
    ]

    # The preview agrees with the formal publish protection: the same
    # reservation blocks the same proposal with the same id.
    blocked = publish(client, pid, proposed, expected=1)
    assert blocked.status_code == 409
    err = blocked.json()["error"]
    assert err["code"] == "reservation_change_conflict"
    assert err["details"]["conflicting_reservation_ids"] == [res1.json()["id"]]
    # The failed publish changed nothing; the preview still bases on v1.
    assert preview(client, pid, proposed, expected=1).status_code == 200


def test_preview_lists_locks_and_waitlist_and_is_read_only(client):
    pid, _rules = open_all_week(client, capacity=1)
    holder = reserve(client, pid, "P-HOLD", at(14), at(15))
    assert holder.status_code == 201, holder.text
    queued = waitlist(client, pid, "P-W1", at(14), at(15))
    assert queued.status_code == 201, queued.text
    held = lock(client, pid, "P-L1", at(10), at(10, 30))
    assert held.status_code == 201, held.text
    stale = lock(client, pid, "P-L2", at(11), at(11, 30))
    assert stale.status_code == 201, stale.text
    force_expire_lock(stale.json()["id"])

    proposed = {
        "weekly": [
            {"weekday": weekday, "start_time": "11:00", "end_time": "14:30"}
            for weekday in range(1, 8)
        ],
        "closed_dates": [],
        "makeup": [],
    }
    r = preview(client, pid, proposed, expected=1)
    assert r.status_code == 200, r.text
    body = r.json()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()

    # The reservation and the queued entry lose 14:30-15:00; the valid lock
    # loses its whole window; the expired lock is not listed at all.
    assert [i["id"] for i in body["affected_reservations"]] == [holder.json()["id"]]
    assert body["affected_reservations"][0]["uncovered"] == [
        {"start": f"{tomorrow}T14:30:00Z", "end": f"{tomorrow}T15:00:00Z"}
    ]
    assert [i["id"] for i in body["affected_locks"]] == [held.json()["id"]]
    lock_item = body["affected_locks"][0]
    assert lock_item["reason"] == "outside_open_hours"
    assert lock_item["uncovered"] == [
        {"start": f"{tomorrow}T10:00:00Z", "end": f"{tomorrow}T10:30:00Z"}
    ]
    assert lock_item["expires_at"] == held.json()["expires_at"]
    assert [i["id"] for i in body["affected_waitlist"]] == [queued.json()["id"]]
    assert body["affected_waitlist"][0]["uncovered"] == [
        {"start": f"{tomorrow}T14:30:00Z", "end": f"{tomorrow}T15:00:00Z"}
    ]

    # The preview is read-only: no version was published, the expired lock
    # was not swept, the queue was not promoted and nothing was cancelled.
    assert client.get(f"/points/{pid}").json()["version"] == 1
    assert len(client.get(f"/points/{pid}/rules/versions").json()) == 1
    assert db_field(
        "SELECT status FROM slot_locks WHERE id = %s", (stale.json()["id"],)
    ) == "locked"
    assert db_field(
        "SELECT status FROM slot_locks WHERE id = %s", (held.json()["id"],)
    ) == "locked"
    assert db_field(
        "SELECT status FROM waitlist_entries WHERE id = %s", (queued.json()["id"],)
    ) == "waiting"
    assert db_field(
        "SELECT status FROM reservations WHERE id = %s", (holder.json()["id"],)
    ) == "active"

    # A formal publish of the same proposal still validates at submission
    # time and is refused by the very reservation and lock the preview named.
    blocked = publish(client, pid, proposed, expected=1)
    assert blocked.status_code == 409
    details = blocked.json()["error"]["details"]
    assert details["conflicting_reservation_ids"] == [holder.json()["id"]]
    assert details["conflicting_lock_ids"] == [held.json()["id"]]


def test_preview_judges_cross_midnight_windows_as_one_segment(client):
    point = create_point(client)
    pid = point["id"]
    target = (datetime.now(UTC) + timedelta(days=2)).date()
    rules = {
        "weekly": [
            {
                "weekday": target.isoweekday(),
                "start_time": "20:00",
                "end_time": "02:00",
            }
        ],
        "closed_dates": [],
        "makeup": [],
    }
    assert publish(client, pid, rules, expected=0).status_code == 201

    # A visit crossing local midnight inside the overnight session.
    start = datetime(target.year, target.month, target.day, 23, 30, tzinfo=UTC)
    end = start + timedelta(hours=1)
    res = reserve(client, pid, "P-NIGHT", start, end)
    assert res.status_code == 201, res.text

    # Closing the next day removes the session's tail: the window is judged
    # as one segment and loses exactly its part past midnight.
    proposed = {**rules, "closed_dates": [(target + timedelta(days=1)).isoformat()]}
    r = preview(client, pid, proposed, expected=1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["id"] for i in body["affected_reservations"]] == [res.json()["id"]]
    next_day = (target + timedelta(days=1)).isoformat()
    assert body["affected_reservations"][0]["uncovered"] == [
        {"start": f"{next_day}T00:00:00Z", "end": f"{next_day}T00:30:00Z"}
    ]

    # Under the unchanged rules the same window keeps its full coverage.
    r = preview(client, pid, rules, expected=1)
    assert r.status_code == 200, r.text
    assert r.json()["affected_reservations"] == []


# ---------------------------------------------------------------------------
# Concurrency: one committed view, never a 500
# ---------------------------------------------------------------------------


def test_preview_racing_a_publish_is_consistent(client):
    pid, _rules = open_all_week(client, "09:00", "12:00")
    proposed = {
        "weekly": [
            {"weekday": weekday, "start_time": "10:00", "end_time": "13:00"}
            for weekday in range(1, 8)
        ],
        "closed_dates": [],
        "makeup": [],
    }
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_preview():
        barrier.wait()
        outcomes["preview"] = preview(client, pid, proposed, expected=1)

    def do_publish():
        barrier.wait()
        outcomes["publish"] = publish(client, pid, proposed, expected=1)

    threads = [
        threading.Thread(target=do_preview),
        threading.Thread(target=do_publish),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # The publish always lands (nothing else moves the version); the preview
    # either answered from the v1 snapshot or conflicted against v2 — never
    # a stale base_version and never a server error.
    assert outcomes["publish"].status_code == 201
    assert outcomes["preview"].status_code in (200, 409)
    if outcomes["preview"].status_code == 200:
        assert outcomes["preview"].json()["base_version"] == 1
    else:
        err = outcomes["preview"].json()["error"]
        assert err["code"] == "version_conflict"
        assert err["details"]["current_version"] == 2


# ---------------------------------------------------------------------------
# Interval subtraction (pure engine helper)
# ---------------------------------------------------------------------------


def test_subtract_intervals_pure_cases():
    from app.scheduler import subtract_intervals

    def t(hour, minute=0):
        return datetime(2026, 10, 5, hour, minute, tzinfo=UTC)

    # Disjoint minus leaves base untouched; touching does not subtract.
    assert subtract_intervals([(t(9), t(12))], [(t(12), t(13))]) == [(t(9), t(12))]
    assert subtract_intervals([(t(9), t(12))], [(t(7), t(8))]) == [(t(9), t(12))]
    # Full containment removes the base interval entirely.
    assert subtract_intervals([(t(9), t(12))], [(t(8), t(13))]) == []
    # Middle slice splits the base into two pieces.
    assert subtract_intervals([(t(9), t(12))], [(t(10), t(11))]) == [
        (t(9), t(10)),
        (t(11), t(12)),
    ]
    # Several minus intervals compose; base edges are clipped.
    assert subtract_intervals(
        [(t(9), t(12))], [(t(8), t(9, 30)), (t(10, 30), t(11)), (t(11, 30), t(14))]
    ) == [(t(9, 30), t(10, 30)), (t(11), t(11, 30))]
    # Multiple base intervals and empty inputs.
    assert subtract_intervals([(t(9), t(10)), (t(11), t(12))], [(t(9, 30), t(11, 30))]) == [
        (t(9), t(9, 30)),
        (t(11, 30), t(12)),
    ]
    assert subtract_intervals([], [(t(9), t(10))]) == []
    assert subtract_intervals([(t(9), t(10))], []) == [(t(9), t(10))]
