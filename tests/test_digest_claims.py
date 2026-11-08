from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _init(path: str) -> None:
    with _connect(path) as connection:
        connection.execute(
            """create table publication (
                id text primary key,
                status text not null,
                attempt_count integer not null default 0,
                claim_token text,
                claim_expires_at text,
                last_error text
            )"""
        )
        connection.execute("insert into publication(id, status) values ('p1', 'pending')")


def _claim(path: str, token: str, lease_seconds: float = 10) -> dict | None:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=lease_seconds)
    with _connect(path) as connection:
        connection.execute("begin immediate")
        cursor = connection.execute(
            """update publication
               set claim_token = ?, claim_expires_at = ?
               where id = ? and status in ('pending', 'failed')
                 and (claim_token is null or claim_expires_at <= ?)""",
            (token, expires.isoformat(), "p1", now.isoformat()),
        )
        row = connection.execute("select * from publication where id='p1'").fetchone()
        connection.commit()
        return dict(row) if cursor.rowcount == 1 else None


def _advance(path: str, token: str, expected: int) -> bool:
    with _connect(path) as connection:
        cursor = connection.execute(
            """update publication set attempt_count = attempt_count + 1
               where id = 'p1' and claim_token = ? and attempt_count = ?
                 and claim_expires_at > ?""",
            (token, expected, datetime.now(UTC).isoformat()),
        )
        return cursor.rowcount == 1


def _complete(path: str, token: str) -> bool:
    with _connect(path) as connection:
        cursor = connection.execute(
            """update publication set status='published', claim_token=null,
                 claim_expires_at=null where id='p1' and claim_token=?
                 and claim_expires_at > ?""",
            (token, datetime.now(UTC).isoformat()),
        )
        return cursor.rowcount == 1


def test_two_independent_database_workers_only_one_claim_and_send(tmp_path) -> None:
    path = str(tmp_path / "claims.sqlite")
    _init(path)
    barrier = threading.Barrier(2)
    senders: list[str] = []
    senders_lock = threading.Lock()

    def worker(token: str) -> bool:
        barrier.wait()
        claim = _claim(path, token)
        if claim is None:
            return False
        assert _advance(path, token, expected=0)
        with senders_lock:
            senders.append(token)
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, ("worker-a", "worker-b")))

    assert sorted(outcomes) == [False, True]
    assert len(senders) == 1
    with _connect(path) as connection:
        row = connection.execute("select * from publication where id='p1'").fetchone()
    assert row["attempt_count"] == 1
    assert row["claim_token"] in senders


def test_expired_claim_recovery_and_stale_worker_fencing(tmp_path) -> None:
    path = str(tmp_path / "claims.sqlite")
    _init(path)
    first = _claim(path, "stale-worker", lease_seconds=0.1)
    assert first is not None
    assert _advance(path, "stale-worker", expected=0)

    time.sleep(0.12)
    replacement = _claim(path, "replacement-worker", lease_seconds=10)
    assert replacement is not None
    assert replacement["attempt_count"] == 1
    assert _advance(path, "stale-worker", expected=1) is False
    assert _complete(path, "stale-worker") is False
    assert _complete(path, "replacement-worker") is True


def test_expired_attempted_claim_requires_reconciliation_before_a_send(tmp_path) -> None:
    path = str(tmp_path / "claims.sqlite")
    _init(path)
    first = _claim(path, "worker-a", lease_seconds=0.1)
    assert first is not None
    assert _advance(path, "worker-a", expected=0)
    time.sleep(0.12)

    second = _claim(path, "worker-b", lease_seconds=10)
    assert second is not None
    sends: list[str] = []
    marker_lookup_conclusive = False
    if second["attempt_count"] > 0:
        if marker_lookup_conclusive:
            sends.append("worker-b")
        else:
            with _connect(path) as connection:
                connection.execute(
                    """update publication set last_error='reconciliation_unavailable',
                         claim_token=null, claim_expires_at=null
                         where id='p1' and claim_token='worker-b'"""
                )
    assert sends == []
    with _connect(path) as connection:
        row = connection.execute("select * from publication where id='p1'").fetchone()
    assert row["attempt_count"] == 1
    assert row["last_error"] == "reconciliation_unavailable"
