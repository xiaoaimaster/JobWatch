"""Immutable per-run snapshots and a reusable AI cache."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .common import digest, dump


def connect(output):
    Path(output).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(output) / "watch.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, task TEXT NOT NULL, started TEXT NOT NULL,
        scope TEXT NOT NULL, settings TEXT NOT NULL, collection_ready INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS observations (
        run_id TEXT NOT NULL REFERENCES runs(id), source TEXT NOT NULL, scope TEXT NOT NULL,
        status TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id, source)
      );
      CREATE INDEX IF NOT EXISTS observations_baseline ON observations(source, scope, status);
      CREATE TABLE IF NOT EXISTS reused_observations (
        run_id TEXT NOT NULL REFERENCES runs(id), source TEXT NOT NULL, capture_run_id TEXT,
        PRIMARY KEY(run_id, source),
        FOREIGN KEY(capture_run_id, source) REFERENCES observations(run_id, source)
      );
      CREATE TABLE IF NOT EXISTS ai_cache (
        key TEXT PRIMARY KEY, payload TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS ai_results (
        run_id TEXT NOT NULL REFERENCES runs(id), source TEXT NOT NULL, job_id TEXT NOT NULL,
        key TEXT NOT NULL REFERENCES ai_cache(key), PRIMARY KEY(run_id, source, job_id)
      );
      CREATE TABLE IF NOT EXISTS ai_attempts (
        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        requests INTEGER NOT NULL, usage TEXT NOT NULL, error TEXT NOT NULL
      );
    """)
    return db


def save_observation(db, run_id, source, scope, result):
    payload = dict(company=source['company'], jobs=result.jobs, errors=result.errors, recovered_fields=result.recovered_fields,
                   pages=result.pages, list_complete=result.list_complete, declared_total=result.declared_total,
                   details=result.details, observed_at=datetime.now(timezone.utc).isoformat())
    with db:
        db.execute("INSERT INTO observations VALUES(?,?,?,?,?)",
                   (run_id, source['id'], scope, result.status, dump(payload)))


def latest_capture(db, source, identity):
    rows = db.execute(
        """SELECT o.run_id, o.scope, o.status, o.payload, r.scope AS task_scope, r.started
           FROM observations o JOIN runs r ON r.id=o.run_id
           WHERE o.source=? AND r.collection_ready=1 AND o.status IN ('complete','ai_assisted','partial')
           ORDER BY r.started DESC, r.id DESC""",
        (source,),
    )
    for row in rows:
        if row["scope"] != digest([row["task_scope"], identity, "normalization-v2"]):
            continue
        payload = json.loads(row["payload"])
        if row["status"] == "partial" and not payload["jobs"]:
            continue
        payload.setdefault("observed_at", row["started"])
        payload["snapshot_status"] = row["status"]
        return row["run_id"], payload
    return None


def reuse_observation(db, run_id, source, capture_run_id):
    with db:
        db.execute("INSERT INTO reused_observations VALUES(?,?,?)", (run_id, source, capture_run_id))


def reused_observations(db, run_id):
    return {row["source"]: row for row in db.execute(
        """SELECT refs.source, captures.status, captures.payload, r.started
           FROM reused_observations refs
           LEFT JOIN observations captures ON captures.run_id=refs.capture_run_id AND captures.source=refs.source
           LEFT JOIN runs r ON r.id=captures.run_id WHERE refs.run_id=?""",
        (run_id,),
    )}


def link_results(db, run_id, records):
    with db:
        db.executemany("INSERT OR IGNORE INTO ai_cache VALUES(?,?)",
                       [(key, dump(result)) for _, key, result in records if result is not None])
        db.executemany("INSERT INTO ai_results VALUES(?,?,?,?) ON CONFLICT(run_id,source,job_id) "
                       "DO UPDATE SET key=excluded.key",
                       [(run_id, job["source"], job["id"], key) for job, key, _ in records])


def results(db, run_id):
    return {(row["source"], row["job_id"]): (row["key"], json.loads(row["payload"]))
            for row in db.execute("SELECT r.*, c.payload FROM ai_results r "
                                  "JOIN ai_cache c USING(key) WHERE run_id=?", (run_id,))}


def previous(db, run, source, scope):
    row = db.execute(
        """SELECT o.payload FROM observations o JOIN runs r ON r.id=o.run_id
           WHERE r.task=? AND r.started<? AND r.collection_ready=1 AND o.source=? AND o.scope=?
           AND o.status IN ('complete','ai_assisted') ORDER BY r.started DESC LIMIT 1""",
        (run["task"], run["started"], source, scope),
    ).fetchone()
    return json.loads(row["payload"])["jobs"] if row else None
