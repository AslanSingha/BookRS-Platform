"""The catalogue's vectors, held once in this process.

A search by meaning and a "similar books" panel each multiply one vector
against every book's embedding. Fetching the embeddings from PostgreSQL
costs far more than the product (432 ms against 1 ms at 5,284 works), so
they are held here as one float32 matrix.

Until 26 Nov there were two copies, one per route. Each was fetched as
Python lists and loaded again in full whenever any embedding changed.
At the 2,000-book slice that cost nothing. At the full UCSD catalogue
(883,468 books by 384 numbers) the matrix is 1.36 GB, and the lists it
was built from are about eight times that, for each copy. An embedding
run commits every 512 books, so during the hour it takes to embed the
catalogue almost every request would have started a full reload.

So now:

- One matrix, shared by both routes: ``CATALOGUE``.
- Loaded a page at a time, straight into float32
  (``bookrs.db.arrays``).
- Kept current by what changed:
  - books embedded since the last look are added, or overwritten where
    they are;
  - books withdrawn from or restored to the catalogue are masked or
    unmasked.
- A full reload only when rows have left the embeddings table, when the
  vectors change size (a new model), or once a day as a safety net. A
  full reload runs in the background while the vectors already held
  keep answering.
- The database is asked whether anything changed at most once every
  ``BOOKRS_CACHE_PROBE_SECONDS`` (30 by default), through indexes
  (migration 9), instead of on every request.

"Since the last look" means rows whose ``created_at`` is later than the
newest one already held. That is exact while one embedding run writes at
a time, which the scheduler's lock ensures: each batch commits before
the next begins, so every later batch carries a later ``created_at``.
Anything that breaks that (rows written out of order, or removed) shows
up as a row count that no longer adds up, and that starts a full reload.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from bookrs.db.arrays import vector_cursor

log = logging.getLogger(__name__)


def probe_seconds() -> float:
    """How often the in-process caches ask the database what changed."""
    return float(os.environ.get("BOOKRS_CACHE_PROBE_SECONDS", 30))


def full_reload_seconds() -> float:
    """How often the vectors are reloaded whole regardless; 0: never."""
    return float(os.environ.get("BOOKRS_VECTORS_FULL_RELOAD_HOURS", 24)) * 3600


#: Rows handed over per round trip while loading.
PAGE = 5_000
#: More changed embeddings than this are reloaded whole, in the
#: background, rather than applied inside a patron's request.
INLINE_LIMIT = 50_000


@dataclass(frozen=True)
class Snapshot:
    """The vectors as they stood when it was taken. Its length never
    changes. A vector overwritten in place, or a book withdrawn, can show
    through: those are single rows, and the next request sees them
    settled."""

    ids: np.ndarray           # (n,) int64: works.id
    matrix: np.ndarray        # (n, d) float32, L2-normalised
    title_only: np.ndarray    # (n,) bool
    source_ids: np.ndarray    # (n,) int64
    live: np.ndarray          # (n,) bool: False for a book withdrawn from the catalogue

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def dimensions(self) -> int:
        return self.matrix.shape[1]


@dataclass(frozen=True)
class _Probe:
    rows: int                       # rows in embeddings
    newest: datetime | None         # their latest created_at
    withdrawn: int                  # works withdrawn from the catalogue
    last_withdrawn: datetime | None # the latest withdrawal


# One statement, so the four numbers describe the same moment. Each is
# answered from an index: the primary key, embeddings_created_at_idx and
# works_withdrawn_idx (migration 9).
_PROBE = """
SELECT (SELECT count(*) FROM embeddings),
       (SELECT max(created_at) FROM embeddings),
       (SELECT count(*) FROM works WHERE deleted_at IS NOT NULL),
       (SELECT max(deleted_at) FROM works WHERE deleted_at IS NOT NULL)
"""

_ROWS = """
SELECT e.work_id, e.vector, e.is_title_only, w.source_id, w.deleted_at IS NULL
FROM embeddings e JOIN works w ON w.id = e.work_id
"""


class _Buffers:
    """The arrays behind the snapshots, with room to grow: rows [0, n)
    are in use. Growing allocates new arrays, so a snapshot taken
    earlier keeps the ones it was given."""

    def __init__(self, dimensions: int, capacity: int) -> None:
        self.dimensions = dimensions
        self.n = 0
        self.skipped = 0            # rows of another size, not held
        self.ids = np.empty(capacity, dtype=np.int64)
        self.matrix = np.empty((capacity, dimensions), dtype=np.float32)
        self.title_only = np.empty(capacity, dtype=bool)
        self.source_ids = np.empty(capacity, dtype=np.int64)
        self.live = np.empty(capacity, dtype=bool)
        # Row of each work, by work id; -1 where none. Work ids are this
        # database's own sequence, so this stays a few megabytes.
        self.row_of = np.full(1024, -1, dtype=np.int64)

    def _grow(self) -> None:
        capacity = int(len(self.ids) * 1.5) + 1024
        for name in ("ids", "title_only", "source_ids", "live"):
            old = getattr(self, name)
            new = np.empty(capacity, dtype=old.dtype)
            new[:self.n] = old[:self.n]
            setattr(self, name, new)
        matrix = np.empty((capacity, self.dimensions), dtype=np.float32)
        matrix[:self.n] = self.matrix[:self.n]
        self.matrix = matrix

    def put(self, work_id: int, vector: np.ndarray, title_only: bool,
            source_id: int, live: bool) -> bool:
        """Overwrite this work's row, or add one. False, holding nothing,
        for a vector of another size."""
        if len(vector) != self.dimensions:
            if self.n or self.skipped:
                return False
            # Nothing held yet (an empty catalogue's first embeddings):
            # the first vector decides the size.
            self.dimensions = len(vector)
            self.matrix = np.empty((len(self.ids), self.dimensions), dtype=np.float32)
        if work_id >= len(self.row_of):
            grown = np.full(max(work_id + 1, 2 * len(self.row_of)), -1, dtype=np.int64)
            grown[:len(self.row_of)] = self.row_of
            self.row_of = grown
        row = int(self.row_of[work_id])
        if row < 0:
            if self.n == len(self.ids):
                self._grow()
            row = self.n
            self.ids[row] = work_id
            self.row_of[work_id] = row
            self.n += 1
        self.matrix[row] = vector
        self.title_only[row] = title_only
        self.source_ids[row] = source_id
        self.live[row] = live
        return True

    def snapshot(self) -> Snapshot:
        n = self.n
        return Snapshot(self.ids[:n], self.matrix[:n], self.title_only[:n],
                        self.source_ids[:n], self.live[:n])


EMPTY = Snapshot(np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32),
                 np.zeros(0, dtype=bool), np.zeros(0, dtype=np.int64),
                 np.zeros(0, dtype=bool))


class CatalogueVectors:
    """Every book's embedding, kept current. ``get`` returns a snapshot."""

    def __init__(self, probe_every: float | None = None,
                 full_every: float | None = None) -> None:
        self.probe_every = probe_seconds() if probe_every is None else probe_every
        self.full_every = full_reload_seconds() if full_every is None else full_every
        # Where a full reload gets its own connection, so it can run in
        # the background: the API sets this to its pool's ``connection``.
        # Unset, a reload runs in the caller's request, on its connection.
        self.connect = None
        self._lock = threading.Lock()
        self._buffers: _Buffers | None = None
        self._seen: _Probe | None = None
        self._snapshot: Snapshot | None = None
        self._looked_at = float("-inf")
        self._loaded_at = float("-inf")
        self._reloading = False

    @property
    def snapshot(self) -> Snapshot | None:
        """What is held now, without asking the database anything."""
        return self._snapshot

    @property
    def reloading(self) -> bool:
        return self._reloading

    def get(self, conn) -> Snapshot:
        snap = self._snapshot
        if snap is not None and time.monotonic() - self._looked_at < self.probe_every:
            return snap
        if snap is None:
            # The first load: every request that arrives meanwhile waits
            # for this one rather than starting its own.
            with self._lock:
                if self._snapshot is None:
                    self._install(*self._load_all(conn))
                return self._snapshot
        # Someone is already looking; what is held answers this request.
        if not self._lock.acquire(blocking=False):
            return snap
        try:
            self._refresh(conn)
        finally:
            self._lock.release()
        return self._snapshot

    # -- inside the lock ------------------------------------------------

    def _refresh(self, conn) -> None:
        self._looked_at = time.monotonic()
        if self._reloading:
            return
        if self.full_every and self._looked_at - self._loaded_at >= self.full_every:
            return self._reload(conn, "the periodic full reload")

        probe = _Probe(*conn.execute(_PROBE).fetchone())
        seen, held = self._seen, self._buffers
        if probe == seen:
            return

        if probe.newest != seen.newest:
            pending = self._count_since(conn, seen.newest, probe.newest)
            if pending > INLINE_LIMIT:
                return self._reload(conn, f"{pending:,} embeddings changed")
            if not self._apply(conn, seen.newest, probe.newest):
                return self._reload(conn, "the vectors changed size")

        if held.n + held.skipped != probe.rows:
            return self._reload(conn, f"{held.n + held.skipped:,} embeddings held, "
                                      f"{probe.rows:,} stored")

        if (probe.withdrawn, probe.last_withdrawn) != (seen.withdrawn, seen.last_withdrawn):
            self._mark_withdrawn(conn)

        self._seen = probe
        self._snapshot = held.snapshot()

    @staticmethod
    def _count_since(conn, since, until) -> int:
        if since is None:
            return conn.execute("SELECT count(*) FROM embeddings "
                                "WHERE created_at <= %s", (until,)).fetchone()[0]
        return conn.execute("SELECT count(*) FROM embeddings "
                            "WHERE created_at > %s AND created_at <= %s",
                            (since, until)).fetchone()[0]

    def _apply(self, conn, since, until) -> bool:
        """Add or overwrite the rows embedded after ``since``, up to the
        probe's ``until``. False if one had another size."""
        held = self._buffers
        sql, params = _ROWS + " WHERE e.created_at <= %s", [until]
        if since is not None:
            sql += " AND e.created_at > %s"
            params.append(since)
        fitted = True
        with conn.transaction(), vector_cursor(conn, "bookrs_vectors_new") as cur:
            cur.itersize = PAGE
            cur.execute(sql, params)
            for row in cur:
                fitted &= held.put(*row)
        return fitted

    def _mark_withdrawn(self, conn) -> None:
        rows = conn.execute("SELECT id FROM works WHERE deleted_at IS NOT NULL").fetchall()
        withdrawn = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        held = self._buffers
        held.live[:held.n] = ~np.isin(held.ids[:held.n], withdrawn)

    def _reload(self, conn, reason: str) -> None:
        log.info("reloading every vector: %s", reason)
        if self.connect is None:
            self._install(*self._load_all(conn))
            return
        self._reloading = True
        threading.Thread(target=self._reload_in_background,
                         name="bookrs-vectors", daemon=True).start()

    # -- loading --------------------------------------------------------

    def _reload_in_background(self) -> None:
        try:
            with self.connect() as conn:
                built = self._load_all(conn)
            with self._lock:
                self._install(*built)
        except Exception:
            log.exception("reloading the vectors failed; the ones held still answer")
        finally:
            self._reloading = False

    def _install(self, held: _Buffers, probe: _Probe) -> None:
        self._buffers, self._seen = held, probe
        self._loaded_at = self._looked_at = time.monotonic()
        self._snapshot = held.snapshot()

    @staticmethod
    def _load_all(conn) -> tuple[_Buffers, _Probe]:
        """Every embedding, a page at a time. The probe is taken first, so
        anything embedded while this runs is newer than it and is picked
        up by the next look, whether or not the load already saw it."""
        started = time.monotonic()
        probe = _Probe(*conn.execute(_PROBE).fetchone())
        # The newest vector's size: after a change of model, the books
        # not yet embedded again are the ones left out.
        newest = conn.execute("SELECT array_length(vector, 1) FROM embeddings "
                              "ORDER BY created_at DESC LIMIT 1").fetchone()
        dimensions = (newest[0] or 0) if newest else 0
        held = _Buffers(dimensions, int(probe.rows * 1.05) + 1024)
        with conn.transaction(), vector_cursor(conn, "bookrs_vectors_all") as cur:
            cur.itersize = PAGE
            cur.execute(_ROWS + " ORDER BY e.work_id")
            for row in cur:
                if not held.put(*row):
                    held.skipped += 1
        if held.skipped:
            log.warning("%d embeddings are not %d numbers long and were left out; "
                        "embed them again with the current model", held.skipped,
                        held.dimensions)
        log.info("vectors loaded: %d books (%d withdrawn) in %.1fs", held.n,
                 held.n - int(held.live[:held.n].sum()), time.monotonic() - started)
        return held, probe


CATALOGUE = CatalogueVectors()
