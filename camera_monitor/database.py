from __future__ import annotations
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    host        TEXT NOT NULL,
    location    TEXT,
    ports_json  TEXT,
    enabled     INTEGER DEFAULT 1,
    icmp_only   INTEGER DEFAULT 0,
    lat         REAL,
    lng         REAL,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id   TEXT NOT NULL,
    ts          TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    rtt_ms      REAL,
    method      TEXT,
    detail      TEXT,
    port        INTEGER,
    FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS state (
    camera_id              TEXT PRIMARY KEY,
    consecutive_failures   INTEGER DEFAULT 0,
    consecutive_successes  INTEGER DEFAULT 0,
    last_status            TEXT DEFAULT 'UNKNOWN',
    last_change_ts         TEXT,
    FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id       TEXT NOT NULL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    status          TEXT DEFAULT 'OPEN',
    reason          TEXT,
    last_notify_at  TEXT,
    notify_count    INTEGER DEFAULT 0,
    FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_checks_camera_ts ON checks (camera_id, ts);
CREATE INDEX IF NOT EXISTS idx_tickets_camera    ON tickets (camera_id);
"""

def _get_db_path(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///"):]
    return database_url


class Database:
    """
    Thread-safe SQLite wrapper com connection pool via threading.local.

    Cada thread obtém sua própria conexão SQLite reutilizável, evitando
    o overhead de abrir/fechar conexão a cada operação sem violar a
    restrição de thread-safety do SQLite.
    """

    def __init__(self, database_url: str) -> None:
        self._path = _get_db_path(database_url)
        self._local = threading.local()  
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # Gerenciamento de conexão (pool por thread)

    def _get_conn(self) -> sqlite3.Connection:
        """Retorna a conexão da thread atual, criando-a se necessário."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            logger.debug("Nova conexão SQLite criada para thread %s", threading.current_thread().name)
        return conn

    def close(self) -> None:
        """Fecha a conexão da thread atual (chamar ao encerrar a thread)."""
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(cameras)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "lat" not in columns:
            logger.info("Migrating database: Adding 'lat' column to cameras table")
            conn.execute("ALTER TABLE cameras ADD COLUMN lat REAL")
        if "lng" not in columns:
            logger.info("Migrating database: Adding 'lng' column to cameras table")
            conn.execute("ALTER TABLE cameras ADD COLUMN lng REAL")
            
        conn.commit()
        logger.info("Database schema initialized: %s", self._path)

    # Cameras

    def upsert_camera(
        self,
        cam_id: str,
        name: str,
        host: str,
        location: str,
        ports: List[int],
        enabled: bool,
        icmp_only: bool = False,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
    ) -> None:
        now = _now_iso()
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO cameras (id, name, host, location, ports_json, enabled, icmp_only, lat, lng, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    host=excluded.host,
                    location=excluded.location,
                    ports_json=excluded.ports_json,
                    enabled=excluded.enabled,
                    icmp_only=excluded.icmp_only,
                    lat=excluded.lat,
                    lng=excluded.lng
                """, (cam_id, name, host, location, json.dumps(ports), int(enabled), int(icmp_only), lat, lng, now))

    def rename_camera(self, old_id: str, new_id: str) -> None:
        if old_id == new_id:
            return
        conn = self._get_conn()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                conn.execute("UPDATE cameras SET id = ? WHERE id = ?", (new_id, old_id))
                conn.execute("UPDATE state SET camera_id = ? WHERE camera_id = ?", (new_id, old_id))
                conn.execute("UPDATE tickets SET camera_id = ? WHERE camera_id = ?", (new_id, old_id))
                conn.execute("UPDATE checks SET camera_id = ? WHERE camera_id = ?", (new_id, old_id))
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def delete_camera(self, cam_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))

    def get_all_cameras(self) -> List[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM cameras ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def get_camera(self, cam_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)).fetchone()
        return dict(row) if row else None


    def insert_check(
        self,
        camera_id: str,
        ts: str,
        ok: bool,
        rtt_ms: Optional[float],
        method: str,
        detail: str,
        port: Optional[int],
    ) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO checks (camera_id, ts, ok, rtt_ms, method, detail, port)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (camera_id, ts, int(ok), rtt_ms, method, detail, port))

    def get_recent_checks(self, camera_id: str, limit: int = 50) -> List[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM checks WHERE camera_id = ? ORDER BY ts DESC LIMIT ?",
            (camera_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_losses(self, camera_id: str, limit: int = 30) -> List[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM checks WHERE camera_id = ? AND ok = 0 ORDER BY ts DESC LIMIT ?",
            (camera_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_check(self, camera_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM checks WHERE camera_id = ? ORDER BY ts DESC LIMIT 1",
            (camera_id,),
        ).fetchone()
        return dict(row) if row else None


    def ensure_state(self, camera_id: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO state "
                "(camera_id, consecutive_failures, consecutive_successes, last_status, last_change_ts) "
                "VALUES (?, 0, 0, 'UNKNOWN', NULL)",
                (camera_id,),
            )

    def get_state(self, camera_id: str) -> dict:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM state WHERE camera_id = ?", (camera_id,)).fetchone()
        if row is None:
            return {
                "camera_id": camera_id,
                "consecutive_failures": 0,
                "consecutive_successes": 0,
                "last_status": "UNKNOWN",
                "last_change_ts": None,
            }
        return dict(row)

    def update_state(
        self,
        camera_id: str,
        consecutive_failures: int,
        consecutive_successes: int,
        last_status: str,
        last_change_ts: Optional[str] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE state SET consecutive_failures = ?, consecutive_successes = ?, "
                "last_status = ?, last_change_ts = ? WHERE camera_id = ?",
                (consecutive_failures, consecutive_successes, last_status, last_change_ts, camera_id),
            )

    def update_state_atomic(
        self,
        camera_id: str,
        result_ok: bool,
        failure_threshold: int,
        recovery_threshold: int,
        ts: str,
    ) -> dict:
        """
        Atualiza o estado de forma atômica dentro de uma única transação,
        eliminando a race condition de read-modify-write.

        Retorna um dict com as chaves:
          - old_status: status antes da atualização
          - new_status: status após a atualização
          - failures: contador de falhas atualizado
          - successes: contador de sucessos atualizado
          - last_change_ts: timestamp da última mudança de status
          - transitioned: True se houve mudança de estado (ONLINE<->OFFLINE)
          - direction: 'went_offline' | 'went_online' | None
        """
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO state "
                "(camera_id, consecutive_failures, consecutive_successes, last_status, last_change_ts) "
                "VALUES (?, 0, 0, 'UNKNOWN', NULL)",
                (camera_id,),
            )

            row = conn.execute(
                "SELECT consecutive_failures, consecutive_successes, last_status, last_change_ts "
                "FROM state WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()

            failures      = row["consecutive_failures"]
            successes     = row["consecutive_successes"]
            old_status    = row["last_status"]
            last_change   = row["last_change_ts"]

            if result_ok:
                successes += 1
                failures   = 0
            else:
                failures  += 1
                successes  = 0

            new_status  = old_status
            transitioned = False
            direction    = None

            if result_ok:
                if old_status in ("OFFLINE", "UNKNOWN") and successes >= recovery_threshold:
                    new_status   = "ONLINE"
                    last_change  = ts
                    transitioned = True
                    direction    = "went_online"
                elif old_status == "UNKNOWN":
                    new_status  = "ONLINE"
                    last_change = ts
            else:
                if old_status in ("ONLINE", "UNKNOWN") and failures >= failure_threshold:
                    new_status   = "OFFLINE"
                    last_change  = ts
                    transitioned = True
                    direction    = "went_offline"

            conn.execute(
                "UPDATE state SET consecutive_failures = ?, consecutive_successes = ?, "
                "last_status = ?, last_change_ts = ? WHERE camera_id = ?",
                (failures, successes, new_status, last_change, camera_id),
            )

        return {
            "old_status":    old_status,
            "new_status":    new_status,
            "failures":      failures,
            "successes":     successes,
            "last_change_ts": last_change,
            "transitioned":  transitioned,
            "direction":     direction,
        }

    # Tickets

    def open_ticket(self, camera_id: str, reason: str, opened_at: str) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO tickets (camera_id, opened_at, status, reason, last_notify_at, notify_count) "
                "VALUES (?, ?, 'OPEN', ?, ?, 1)",
                (camera_id, opened_at, reason, opened_at),
            )
            return cur.lastrowid  

    def close_ticket(self, ticket_id: int, closed_at: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status = 'RESOLVED', closed_at = ? WHERE id = ?",
                (closed_at, ticket_id),
            )

    def get_open_ticket(self, camera_id: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM tickets WHERE camera_id = ? AND status = 'OPEN' ORDER BY opened_at DESC LIMIT 1",
            (camera_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_all_tickets(self, status_filter: Optional[str] = None, limit: int = 100) -> List[dict]:
        conn = self._get_conn()
        if status_filter:
            rows = conn.execute(
                "SELECT t.*, c.name as camera_name, c.host, c.location "
                "FROM tickets t JOIN cameras c ON t.camera_id = c.id "
                "WHERE t.status = ? ORDER BY t.opened_at DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t.*, c.name as camera_name, c.host, c.location "
                "FROM tickets t JOIN cameras c ON t.camera_id = c.id "
                "ORDER BY t.opened_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # Dashboard

    def get_overview_stats(self) -> dict:
        conn = self._get_conn()
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM cameras WHERE enabled = 1"
        ).fetchone()["cnt"]
        open_tickets = conn.execute(
            "SELECT COUNT(DISTINCT camera_id) as cnt FROM tickets WHERE status='OPEN'"
        ).fetchone()["cnt"]
        return {
            "total_cameras":   total,
            "offline_cameras": open_tickets,
            "online_cameras":  total - open_tickets,
        }

    def get_cameras_with_status(self) -> List[dict]:
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT
                c.id, c.name, c.host, c.location, c.ports_json, c.icmp_only,
                s.last_status, s.last_change_ts, s.consecutive_failures, s.consecutive_successes,
                ch.ts AS last_check_ts, ch.rtt_ms AS last_rtt_ms, ch.ok AS last_ok,
                ch.method AS last_method, ch.detail AS last_detail,
                (SELECT id FROM tickets WHERE camera_id = c.id AND status='OPEN'
                    ORDER BY opened_at DESC LIMIT 1) AS open_ticket_id,
                (SELECT opened_at FROM tickets WHERE camera_id = c.id AND status='OPEN'
                    ORDER BY opened_at DESC LIMIT 1) AS offline_since
            FROM cameras c
            LEFT JOIN state s ON s.camera_id = c.id
            LEFT JOIN checks ch ON ch.id = (
                SELECT id FROM checks WHERE camera_id = c.id ORDER BY ts DESC LIMIT 1
            )
            WHERE c.enabled = 1
            ORDER BY c.name
            """).fetchall()
        return [dict(r) for r in rows]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
