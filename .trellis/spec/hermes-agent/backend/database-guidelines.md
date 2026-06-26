# Database Guidelines

> Storage is **SQLite in WAL mode**, one database per store, multi-reader /
> single-writer guarded by a process-wide `threading.Lock`. There is **no** ORM
> and **no** formal migration tool — schema lives in `_SCHEMA_SQL` strings and
> `_ensure_*_columns` `ALTER ADD COLUMN` guards.

Databases in scope:
- `data/audit_log.db` — `toolsets/audit_log.py`
- `data/incidents.db` — `toolsets/incident_store.py`
- `data/identity.db` — `aiops/domain/identity.py` (`SQLiteIdentityStore`)
- approval store (`apps/aiops_k8s_gateway/approval_service.py`, in-process,
  path from env `AIOPS_IDENTITY_DB` / `AIOPS_DATA_DIR`)

Path resolution is identical everywhere (`toolsets/audit_log.py:93`,
`toolsets/incident_store.py:233`, `identity.py:650`):

```python
def _default_db_path() -> Path:
    env_dir = os.getenv("AIOPS_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser() / "<name>.db"
    return _project_root() / "data" / "<name>.db"
```

---

## Connection setup (copy verbatim)

`toolsets/audit_log.py:108`:

```python
self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                              timeout=1.0, isolation_level=None)
self._conn.row_factory = sqlite3.Row
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA foreign_keys=ON")
self._conn.executescript(_SCHEMA_SQL)
self._ensure_audit_columns()
```

Rules:
- **WAL + `check_same_thread=False`** because the store is shared across request
  threads / `asyncio.to_thread` workers.
- **`isolation_level=None`** + explicit `BEGIN IMMEDIATE` / `commit` — transactions
  are managed manually inside `_execute_write`.
- **`row_factory = sqlite3.Row`** so reads return dict-like rows; `_fetchall` /
  `_fetchone` convert to plain `dict` (`toolsets/audit_log.py:181`).

---

## Write path with retry — `_execute_write` (`toolsets/audit_log.py:141`)

```python
for attempt in range(_WRITE_MAX_RETRIES):           # 15
    try:
        with self._lock:
            ...
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self._conn); self._conn.commit()
            except BaseException:
                try: self._conn.rollback()
                except Exception: pass
                raise
        self._write_count += 1
        if self._write_count % _CHECKPOINT_EVERY_N_WRITES == 0:   # 50
            self._try_wal_checkpoint()
        return result
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if ("locked" in message or "busy" in message) and attempt < _WRITE_MAX_RETRIES - 1:
            last_err = exc
            time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))  # 0.02..0.15
            continue
        raise
```

Async methods wrap it: `await asyncio.to_thread(self._execute_write, _write)`
(`toolsets/audit_log.py:263`). **Copy this exact retry/backoff for any new store.**
Random jitter prevents write-thundering; checkpoint-every-50 keeps the WAL bounded.

---

## Schema + migration guard pattern

Schema as module constant `_SCHEMA_SQL` (run via `executescript`), plus an
`_ensure_*_columns` method that **additively** backfills new columns for existing
databases (`toolsets/audit_log.py:75,121`):

```python
_AUDIT_EXTRA_COLUMNS = {
    "actor": "TEXT", "role": "TEXT", "scope": "TEXT", "request_id": "TEXT",
    "permission": "TEXT", "decision": "TEXT", "resource_scope": "TEXT",
    "approval_id": "TEXT", "action_proposal_id": "TEXT",
}
def _ensure_audit_columns(self):
    for column, definition in _AUDIT_EXTRA_COLUMNS.items():
        try:
            self._conn.execute(f"ALTER TABLE audit_log ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass                      # column already exists
```

`incident_store` uses the same pattern with `_INCIDENT_EXTRA_COLUMNS` /
`_CASE_PROFILE_EXTRA_COLUMNS` and an `_ensure_incident_indexes`
(`toolsets/incident_store.py:36,263`). identity.py seeds builtin roles
(`SQLiteIdentityStore.seed_builtin_roles`, `identity.py:376`).

Rules:
- **Migrations are additive `ALTER ADD COLUMN` only**, tolerated by idempotent
  `try/except sqlite3.OperationalError`. There is no drop/rename path.
- **New index**: `CREATE INDEX IF NOT EXISTS` in `executescript` or in an
  `_ensure_*_indexes` helper run at construction (`incident_store.py:281`).
- **Never `DROP TABLE`/`DROP COLUMN`** in a running store. Ship it as a new
  column and stop writing the old one.

## Common query patterns

- Reads go through `_fetchall`/`_fetchone` under `with self._lock:` (no retry —
  reads contend far less; `toolsets/audit_log.py:181`).
- **JSON columns** are stored as `TEXT` via
  `json.dumps(value, ensure_ascii=False, sort_keys=True)` and decoded on read in
  the same read helper (`incident_store._json_dumps`, line 360; decoded inline,
  e.g. `row["payload"] = json.loads(row.pop("payload_json") or "{}")`, line 539).
- **Upsert** = `INSERT ... ON CONFLICT(...) DO UPDATE SET ...` keyed on the
  primary id (`incident_store.upsert_analysis`, line 543;
  `approval_service.py` approval rows). `effective_created` is preserved from the
  existing row when present (`incident_store.py:679`).
- **Foreign keys with `ON DELETE CASCADE`** for child tables that belong to one
  `incident` (`incident_events`, `incident_evidence`, `incident_case_profiles`,
  `incident_lessons`, `incident_store.py:159`).

## Common mistakes

- Opening a *second* `sqlite3.connect` for the same db file — use the single
  module-level `_DB` / `_STORE` instance (`toolsets/audit_log.py:337`,
  `toolsets/incident_store.py:1158`) so the lock + WAL are shared.
- Writing JSON with `json.dumps(default)` — the repo standardizes on
  `ensure_ascii=False, sort_keys=True` so Chinese text survives and hashes are
  stable (`hermes/service_main.py:_stable_digest` depends on it).
- A write path that holds the lock across an `await`/IO — `_execute_write` runs the
  whole transaction synchronously inside the lock, and the whole thing is wrapped
  in `asyncio.to_thread`; do not interleave awaitable work into `fn`.
