"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS treaties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    inception_date TEXT NOT NULL,
                    initial_limit REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS endorsements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES treaties(id) ON DELETE CASCADE,
                    effective_date TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('increase','decrease')),
                    amount REAL NOT NULL CHECK (amount >= 0),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed')),
                    seq INTEGER,
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_endo_treaty ON endorsements(treaty_id, id);
                CREATE TABLE IF NOT EXISTS approved_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES treaties(id) ON DELETE CASCADE,
                    claim_number TEXT NOT NULL,
                    approved_date TEXT NOT NULL,
                    recoverable_amount REAL NOT NULL CHECK (recoverable_amount >= 0),
                    source_record_id INTEGER,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (treaty_id, claim_number)
                );
                CREATE INDEX IF NOT EXISTS idx_claim_treaty ON approved_claims(treaty_id, id);
                CREATE TABLE IF NOT EXISTS ledger_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES treaties(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    ref_table TEXT,
                    ref_id INTEGER,
                    version_seq INTEGER,
                    effective_date TEXT,
                    limit_after REAL,
                    used_approved_recoveries REAL,
                    remaining REAL,
                    summary TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_snapshot_treaty ON ledger_snapshots(treaty_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ----- 合约台账 -----

    def create_treaty(self, reference: str, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO treaties(reference,name,currency,inception_date,initial_limit,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (reference, data["name"], data["currency"], data["inception_date"], float(data["initial_limit"]), actor_id, now),
                )
                treaty_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO ledger_snapshots(treaty_id,kind,ref_table,ref_id,version_seq,effective_date,limit_after,used_approved_recoveries,remaining,summary,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (treaty_id, "initial", None, None, 0, data["inception_date"], float(data["initial_limit"]), 0.0, float(data["initial_limit"]), "合约建账，初始限额", actor_id, now),
                )
                row = connection.execute("SELECT * FROM treaties WHERE id=?", (treaty_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return dict(row)

    def get_treaty(self, treaty_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM treaties WHERE id=?", (treaty_id,)).fetchone()
        if row is None:
            raise NotFound("合约不存在")
        return dict(row)

    def find_treaty_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM treaties WHERE reference=?", (reference,)).fetchone()
        return dict(row) if row is not None else None

    def list_treaties(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM treaties ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def list_endorsements(self, treaty_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM endorsements WHERE treaty_id=? ORDER BY id", (treaty_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_claims(self, treaty_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM approved_claims WHERE treaty_id=? ORDER BY id", (treaty_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_snapshots(self, treaty_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM ledger_snapshots WHERE treaty_id=? ORDER BY id", (treaty_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_endorsement(self, treaty_id: int, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO endorsements(treaty_id,effective_date,direction,amount,reason,status,created_by,created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                    (treaty_id, data["effective_date"], data["direction"], float(data["amount"]), data["reason"], actor_id, now),
                )
                endorsement_id = int(cursor.lastrowid)
                row = connection.execute("SELECT * FROM endorsements WHERE id=?", (endorsement_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise NotFound("合约不存在") from exc
        return dict(row)

    def get_endorsement(self, endorsement_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM endorsements WHERE id=?", (endorsement_id,)).fetchone()
        if row is None:
            raise NotFound("批单不存在")
        return dict(row)

    def add_claim(self, treaty_id: int, data: Dict[str, Any], actor_id: str, snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = _now()
        source = data.get("source_record_id")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO approved_claims(treaty_id,claim_number,approved_date,recoverable_amount,source_record_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (treaty_id, data["claim_number"], data["approved_date"], float(data["recoverable_amount"]), source, actor_id, now),
                )
                claim_id = int(cursor.lastrowid)
                if snapshot is not None:
                    snapshot = dict(snapshot, ref_id=claim_id)
                    self.record_snapshot(connection, treaty_id, snapshot, now)
                row = connection.execute("SELECT * FROM approved_claims WHERE id=?", (claim_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "UNIQUE" in message:
                raise Conflict("该合约下赔案编号已登记") from exc
            raise NotFound("合约不存在") from exc
        return dict(row)

    def add_ledger_snapshot(self, treaty_id: int, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            self.record_snapshot(connection, treaty_id, snapshot, now)
            row = connection.execute("SELECT * FROM ledger_snapshots WHERE treaty_id=? ORDER BY id DESC LIMIT 1", (treaty_id,)).fetchone()
        return dict(row)

    def record_snapshot(self, connection: sqlite3.Connection, treaty_id: int, snapshot: Dict[str, Any], now: str) -> None:        connection.execute(
            "INSERT INTO ledger_snapshots(treaty_id,kind,ref_table,ref_id,version_seq,effective_date,limit_after,used_approved_recoveries,remaining,summary,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                treaty_id,
                snapshot["kind"],
                snapshot.get("ref_table"),
                snapshot.get("ref_id"),
                snapshot.get("version_seq"),
                snapshot.get("effective_date"),
                None if snapshot.get("limit_after") is None else float(snapshot["limit_after"]),
                None if snapshot.get("used_approved_recoveries") is None else float(snapshot["used_approved_recoveries"]),
                None if snapshot.get("remaining") is None else float(snapshot["remaining"]),
                snapshot["summary"],
                snapshot["actor_id"],
                now,
            ),
        )

    def confirm_endorsement(self, endorsement_id: int, actor_id: str, planner) -> None:
        """单个 IMMEDIATE 事务内：读取现状 → planner 试算 → 落库。

        planner 抛异常会随 with 回滚；返回 (versions, seq, snapshot)。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute("SELECT * FROM endorsements WHERE id=?", (endorsement_id,)).fetchone()
            if candidate is None:
                raise NotFound("批单不存在")
            candidate = dict(candidate)
            treaty = connection.execute("SELECT * FROM treaties WHERE id=?", (candidate["treaty_id"],)).fetchone()
            treaty = dict(treaty)
            confirmed_rows = connection.execute(
                "SELECT * FROM endorsements WHERE treaty_id=? AND status='confirmed' ORDER BY id",
                (treaty["id"],),
            ).fetchall()
            confirmed = [dict(row) for row in confirmed_rows]
            claim_rows = connection.execute(
                "SELECT * FROM approved_claims WHERE treaty_id=? ORDER BY id",
                (treaty["id"],),
            ).fetchall()
            claims = [dict(row) for row in claim_rows]

            versions, seq, snapshot = planner(treaty, candidate, confirmed, claims)

            connection.execute(
                "UPDATE endorsements SET status='confirmed',seq=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                (seq, actor_id, now, endorsement_id),
            )
            # 各版本限额不另建表：由已确认批单按（生效日，seq）重算，快照留存历史
            self.record_snapshot(connection, treaty["id"], {
                "kind": "endorsement_confirmed",
                "ref_table": "endorsements",
                "ref_id": endorsement_id,
                "version_seq": seq,
                "effective_date": snapshot["effective_date"],
                "limit_after": snapshot["limit_after"],
                "used_approved_recoveries": snapshot["used_approved_recoveries"],
                "remaining": snapshot["remaining"],
                "summary": snapshot["summary"],
                "actor_id": actor_id,
            }, now)
            for version in versions:
                self.record_snapshot(connection, treaty["id"], {
                    "kind": "limit_version",
                    "ref_table": "endorsements",
                    "ref_id": version["endorsement_id"],
                    "version_seq": version["seq"],
                    "effective_date": version["effective_date"],
                    "limit_after": version["limit_after"],
                    "used_approved_recoveries": version["used_approved_recoveries"],
                    "remaining": round(version["limit_after"] - version["used_approved_recoveries"], 2),
                    "summary": "限额版本v%s（%s生效）" % (version["seq"], version["effective_date"]),
                    "actor_id": actor_id,
                }, now)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
