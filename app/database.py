"""SQLite-backed revision storage.

Durability and concurrency contract:

* the database runs in WAL mode with ``synchronous=FULL``: every commit
  is fsync-ed and survives a process restart; readers never block on a
  writer and always observe a whole committed revision (or none of it);
* every write happens inside one ``BEGIN IMMEDIATE`` transaction which
  both serialises writers (across threads and processes) and makes the
  "revision must be strictly increasing" check atomic with the insert —
  a failed transaction rolls back completely, so storage errors can
  never leave a half revision;
* raw input and computed state are stored together, therefore a reader
  of any revision sees input, geometry and status from the same commit.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    relation_id          INTEGER NOT NULL,
    revision             INTEGER NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('valid', 'invalid')),
    input_json           TEXT    NOT NULL,
    geometry_json        TEXT,
    error_code           TEXT,
    error_message        TEXT,
    error_way_ids_json   TEXT,
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (relation_id, revision)
);
"""

# No in-process lock is needed on purpose: SQLite's own BEGIN IMMEDIATE
# write lock serialises writers (across threads AND processes, e.g.
# multiple uvicorn workers), while WAL lets readers proceed concurrently
# without blocking a commit. A 30 s busy timeout makes contending writers
# queue instead of failing.
BUSY_TIMEOUT_SECONDS = 30.0
LOCK_RETRIES = 5


class Database:
    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False: one short-lived connection per request,
        # shared nothing between threads.
        self._connect_kwargs = dict(
            timeout=BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # manual transaction control
            check_same_thread=False,
        )
        self.init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, **self._connect_kwargs)
        try:
            conn.row_factory = sqlite3.Row
            # Persistent WAL keeps working across restarted connections.
            # PRAGMAs must run outside an explicit transaction.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(SCHEMA)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ writes
    def save_revision(
        self,
        relation_id: int,
        revision: int,
        status: str,
        input_payload: dict,
        geometry: dict | None,
        error: dict | None,
    ) -> None:
        """Atomically persist one full revision.

        Raises :class:`RevisionConflict` when ``revision`` is not strictly
        greater than every existing revision of the relation.
        """
        with self._connect() as conn:
            attempt = 0
            while True:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    # Another writer holds the lock; busy_timeout usually
                    # makes the execute block, but keep an explicit retry
                    # for the edge case where it returns immediately.
                    if "locked" in str(exc).lower() and attempt < LOCK_RETRIES:
                        attempt += 1
                        time.sleep(0.05 * attempt)
                        continue
                    raise
            try:
                row = conn.execute(
                    "SELECT MAX(revision) AS max_rev FROM revisions WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                max_rev = row["max_rev"]
                if max_rev is not None and revision <= max_rev:
                    conn.rollback()
                    raise RevisionConflict(
                        f"revision {revision} is not greater than the latest revision {max_rev}",
                        latest=max_rev,
                    )
                conn.execute(
                    """
                    INSERT INTO revisions (
                        relation_id, revision, status, input_json, geometry_json,
                        error_code, error_message, error_way_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relation_id,
                        revision,
                        status,
                        json.dumps(input_payload, separators=(",", ":"), ensure_ascii=False),
                        json.dumps(geometry, separators=(",", ":")) if geometry else None,
                        error["code"] if error else None,
                        error["message"] if error else None,
                        json.dumps(error["way_ids"]) if error else None,
                    ),
                )
                conn.commit()
            except RevisionConflict:
                raise
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------- reads
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        error = None
        if row["error_code"]:
            error = {
                "code": row["error_code"],
                "message": row["error_message"],
                "way_ids": json.loads(row["error_way_ids_json"] or "[]"),
            }
        return {
            "relation_id": row["relation_id"],
            "revision": row["revision"],
            "status": row["status"],
            "input": json.loads(row["input_json"]),
            "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None,
            "error": error,
            "created_at": row["created_at"],
        }

    def get_revision(self, relation_id: int, revision: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? AND revision = ?",
                (relation_id, revision),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_latest_revision(self, relation_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (relation_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def get_latest_revisions_bulk(
        self, relation_ids: list[int], inside_snapshot=None
    ) -> dict[int, dict]:
        """Return the latest row of every relation from one committed snapshot.

        The whole read runs inside a single deferred read transaction.
        In WAL mode a read transaction pins one database snapshot for its
        whole lifetime, so a revision upload committing while this call is
        in flight can never make the returned set mix pre-upload and
        post-upload revisions: every row comes from the same snapshot.
        Relations without any stored row are simply absent from the result.

        ``inside_snapshot`` is an optional callback invoked after the
        snapshot-acquiring read has run (the transaction is still open); it
        exists for tests that need to race a writer against the open
        snapshot while proving the already-fetched rows stay unchanged.
        """
        ids = sorted(set(relation_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                # Run the data read before any hook: in Python's sqlite3
                # wrapper a deferred BEGIN only acquires its WAL snapshot
                # on the first statement, so this SELECT is what pins the
                # snapshot for the whole transaction.
                rows = conn.execute(
                    f"""
                    SELECT r.*
                    FROM revisions r
                    JOIN (
                        SELECT relation_id, MAX(revision) AS max_revision
                        FROM revisions
                        WHERE relation_id IN ({placeholders})
                        GROUP BY relation_id
                    ) m
                      ON r.relation_id = m.relation_id
                     AND r.revision = m.max_revision
                    """,
                    ids,
                ).fetchall()
                if inside_snapshot is not None:
                    inside_snapshot()
                result = {row["relation_id"]: self._row_to_dict(row) for row in rows}
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def get_last_valid(self, relation_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE relation_id = ? AND status = 'valid' "
                "ORDER BY revision DESC LIMIT 1",
                (relation_id,),
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def list_revisions(self, relation_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT revision, status FROM revisions WHERE relation_id = ? "
                "ORDER BY revision",
                (relation_id,),
            ).fetchall()
            return [dict(r) for r in rows]


class RevisionConflict(Exception):
    def __init__(self, message: str, latest: int):
        super().__init__(message)
        self.latest = latest
