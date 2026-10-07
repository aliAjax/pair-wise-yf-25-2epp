"""学术会议同行评审系统：标准库 + SQLite 的可运行示例。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "review.db"
VALID_DECISIONS = {"accept", "reject", "minor_revision", "major_revision"}
TARGET_REVIEWERS = 2
# 占用评审人负载的状态（completed 已释放负载，但仍占论文评审名额）。
ACTIVE_STATUSES = ("invited", "accepted")
SLOT_STATUSES = ("invited", "accepted", "completed")
ASSIGNABLE_STATUSES = ("submitted", "under_review")
SYSTEM_USER = "system"


class BusinessError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewStore:
    """领域逻辑。每个公开方法使用独立连接，避免 HTTP 线程共享 SQLite 连接。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._schema_lock = threading.Lock()
        self._job_locks: dict[int, threading.Lock] = {}
        # 测试钩子：分配某篇前抛错，模拟写入中途失败。
        self.allocation_failure_paper_id: int | None = None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init_schema(self) -> None:
        with self._schema_lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('author','reviewer','chair')),
                    load_limit INTEGER NOT NULL DEFAULT 3 CHECK (load_limit >= 0)
                );
                CREATE TABLE IF NOT EXISTS papers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK (status IN ('submitted','under_review','decided','withdrawn')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (paper_id, version)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS bids (
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    interest TEXT NOT NULL CHECK (interest IN ('want','maybe','decline')),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reviewer_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'invited'
                        CHECK (status IN ('invited','accepted','declined','completed','revoked')),
                    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
                    review_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (paper_id, reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS waitlist (
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    queued_by TEXT NOT NULL REFERENCES users(id),
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (paper_id, reviewer_id)
                );
                CREATE TABLE IF NOT EXISTS allocation_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    reason TEXT NOT NULL DEFAULT 'auto_allocate',
                    status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS allocation_items (
                    job_id INTEGER NOT NULL REFERENCES allocation_jobs(id),
                    paper_id INTEGER NOT NULL REFERENCES papers(id),
                    status TEXT NOT NULL CHECK (status IN ('pending','completed','failed')),
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, paper_id)
                );
                CREATE TABLE IF NOT EXISTS rebuttals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    author_id TEXT NOT NULL REFERENCES users(id),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id),
                    decision TEXT NOT NULL CHECK (decision IN ('accept','reject','minor_revision','major_revision')),
                    note TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id INTEGER,
                    actor_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (paper_id) REFERENCES papers(id)
                );
                """
            )
            # 旧库的 assignments CHECK 约束不含 revoked：通过表重建做一次轻量迁移。
            check = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='assignments'"
            ).fetchone()
            if check is not None and "'revoked'" not in (check[0] or ""):
                conn.executescript(
                    """
                    ALTER TABLE assignments RENAME TO assignments_old;
                    CREATE TABLE assignments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        paper_id INTEGER NOT NULL REFERENCES papers(id),
                        reviewer_id TEXT NOT NULL REFERENCES users(id),
                        status TEXT NOT NULL DEFAULT 'invited'
                            CHECK (status IN ('invited','accepted','declined','completed','revoked')),
                        score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 5),
                        review_text TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (paper_id, reviewer_id)
                    );
                    INSERT INTO assignments
                        SELECT id,paper_id,reviewer_id,status,score,review_text,created_at,updated_at
                        FROM assignments_old;
                    DROP TABLE assignments_old;
                    """
                )
            conn.execute(
                "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)",
                (SYSTEM_USER, "系统自动分配", "chair", 0),
            )

    def seed(self) -> None:
        self.init_schema()
        users = [
            ("alice", "Alice 作者", "author", 0),
            ("bob", "Bob 作者", "author", 0),
            ("r1", "评审人一号", "reviewer", 3),
            ("r2", "评审人二号", "reviewer", 3),
            ("r3", "评审人三号", "reviewer", 2),
            ("chair", "程序委员会主席", "chair", 0),
            ("chair2", "程序委员会副主席", "chair", 0),
        ]
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,load_limit) VALUES(?,?,?,?)", users
            )

    def _user(self, conn: sqlite3.Connection, user_id: str | None) -> sqlite3.Row:
        if not user_id:
            raise BusinessError("缺少 X-User-Id 请求头", 401, "authentication_required")
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise BusinessError("用户不存在", 401, "unknown_user")
        return row

    @staticmethod
    def _require(row: sqlite3.Row, role: str) -> None:
        if row["role"] != role:
            raise BusinessError(f"该操作仅允许 {role} 角色", 403, "forbidden")

    def _audit(self, conn: sqlite3.Connection, paper_id: int | None, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(paper_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (paper_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def submit_paper(self, user_id: str, title: str, abstract: str) -> dict:
        title, abstract = title.strip(), abstract.strip()
        if len(title) < 3 or len(abstract) < 20:
            raise BusinessError("标题至少 3 字，摘要至少 20 字", 422, "invalid_paper")
        digest = hashlib.sha256(f"{title}\n{abstract}".encode()).hexdigest()
        with self.connect() as conn:
            user = self._user(conn, user_id)
            self._require(user, "author")
            cur = conn.execute(
                "INSERT INTO papers(author_id,title,abstract,created_at) VALUES(?,?,?,?)",
                (user_id, title, abstract, utcnow()),
            )
            paper_id = cur.lastrowid
            conn.execute(
                "INSERT INTO paper_versions(paper_id,version,content_hash,created_at) VALUES(?,?,?,?)",
                (paper_id, 1, digest, utcnow()),
            )
            self._audit(conn, paper_id, user_id, "paper.submit", {"version": 1, "sha256": digest})
            return {"id": paper_id, "status": "submitted", "version": 1, "sha256": digest}

    def _paper_view(self, conn: sqlite3.Connection, paper: sqlite3.Row, viewer: sqlite3.Row) -> dict:
        data = {
            "id": paper["id"],
            "title": paper["title"],
            "abstract": paper["abstract"],
            "status": paper["status"],
            "created_at": paper["created_at"],
        }
        if viewer["role"] == "chair" or viewer["id"] == paper["author_id"]:
            data["author_id"] = paper["author_id"]
        else:
            data["author_id"] = None  # 双盲：评审人看不到作者身份。
        return data

    def list_papers(self, user_id: str) -> list[dict]:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "chair":
                rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
            elif user["role"] == "author":
                rows = conn.execute("SELECT * FROM papers WHERE author_id=? ORDER BY id", (user_id,)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT p.* FROM papers p
                       LEFT JOIN assignments a ON a.paper_id=p.id AND a.reviewer_id=?
                       LEFT JOIN bids b ON b.paper_id=p.id AND b.reviewer_id=?
                       WHERE a.id IS NOT NULL OR b.paper_id IS NOT NULL ORDER BY p.id""",
                    (user_id, user_id),
                ).fetchall()
            return [self._paper_view(conn, row, user) for row in rows]

    def get_paper(self, user_id: str, paper_id: int) -> dict:
        with self.connect() as conn:
            user = self._user(conn, user_id)
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper:
                raise BusinessError("论文不存在", 404, "not_found")
            if user["role"] == "reviewer":
                allowed = conn.execute(
                    "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? UNION SELECT 1 FROM bids WHERE paper_id=? AND reviewer_id=?",
                    (paper_id, user_id, paper_id, user_id),
                ).fetchone()
                if not allowed:
                    raise BusinessError("评审人未获授权查看该论文", 403, "forbidden")
            elif user["role"] == "author" and paper["author_id"] != user_id:
                raise BusinessError("作者只能查看自己的论文", 403, "forbidden")
            return self._paper_view(conn, paper, user)

    def add_conflict(self, chair_id: str, paper_id: int, reviewer_id: str, reason: str) -> dict:
        if not reason.strip():
            raise BusinessError("利益冲突原因不能为空", 422, "invalid_reason")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                chair = self._user(conn, chair_id)
                self._require(chair, "chair")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper:
                    raise BusinessError("论文不存在", 404, "not_found")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                try:
                    conn.execute(
                        "INSERT INTO conflicts(reviewer_id,paper_id,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                        (reviewer_id, paper_id, reason.strip(), chair_id, utcnow()),
                    )
                except sqlite3.IntegrityError:
                    raise BusinessError("利益冲突已登记", 409, "conflict_exists")
                # 排队位置立即作废；待回复邀请立即失效并补人。
                conn.execute("DELETE FROM waitlist WHERE paper_id=? AND reviewer_id=?", (paper_id, reviewer_id))
                invited = conn.execute(
                    "SELECT id FROM assignments WHERE paper_id=? AND reviewer_id=? AND status='invited'",
                    (paper_id, reviewer_id),
                ).fetchone()
                revoked_id = invited["id"] if invited else None
                if invited:
                    conn.execute(
                        "UPDATE assignments SET status='revoked',updated_at=? WHERE id=?",
                        (utcnow(), invited["id"]),
                    )
                self._audit(conn, paper_id, chair_id, "conflict.add", {"reviewer_id": reviewer_id, "reason": reason.strip()})
                if revoked_id is not None:
                    self._audit(
                        conn, paper_id, chair_id, "assignment.revoke",
                        {"assignment_id": revoked_id, "reviewer_id": reviewer_id,
                         "reason": "refill_conflict"},
                    )
                refill = []
                if paper["status"] in ASSIGNABLE_STATUSES:
                    # 本论文优先补位（排队者先上），再全局为其他缺口论文补人。
                    refill = [self._allocate_paper_locked(conn, paper_id, chair_id, "refill_conflict")]
                    refill.extend(self._sweep_locked(conn, chair_id, "refill_conflict"))
                return {
                    "paper_id": paper_id,
                    "reviewer_id": reviewer_id,
                    "reason": reason.strip(),
                    "revoked_assignment_id": revoked_id,
                    "refill": refill,
                }
            except Exception:
                conn.rollback()
                raise

    def bid(self, reviewer_id: str, paper_id: int, interest: str, note: str = "") -> dict:
        if interest not in {"want", "maybe", "decline"}:
            raise BusinessError("意向必须为 want、maybe 或 decline", 422, "invalid_interest")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            paper = conn.execute("SELECT status FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["status"] not in {"submitted", "under_review"}:
                raise BusinessError("论文不存在或当前不可表达意向", 409, "paper_unavailable")
            if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                raise BusinessError("存在利益冲突，不能表达评审意向", 409, "conflict_of_interest")
            conn.execute(
                """INSERT INTO bids(reviewer_id,paper_id,interest,note,created_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(reviewer_id,paper_id) DO UPDATE SET interest=excluded.interest,note=excluded.note,created_at=excluded.created_at""",
                (reviewer_id, paper_id, interest, note.strip(), utcnow()),
            )
            self._audit(conn, paper_id, reviewer_id, "bid.set", {"interest": interest, "note": note.strip()})
            return {"paper_id": paper_id, "reviewer_id": reviewer_id, "interest": interest}

    def assign(self, chair_id: str, paper_id: int, reviewer_id: str) -> dict:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或不可分配", 409, "paper_unavailable")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                if conn.execute("SELECT 1 FROM conflicts WHERE reviewer_id=? AND paper_id=?", (reviewer_id, paper_id)).fetchone():
                    raise BusinessError("评审人与论文存在利益冲突", 409, "conflict_of_interest")
                load = conn.execute(
                    "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
                    (reviewer_id,),
                ).fetchone()[0]
                if load >= reviewer["load_limit"]:
                    raise BusinessError("评审人已达到负载上限", 409, "reviewer_at_capacity")
                try:
                    cur = conn.execute(
                        "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
                        (paper_id, reviewer_id, utcnow(), utcnow()),
                    )
                except sqlite3.IntegrityError:
                    raise BusinessError("该评审人已被分配此论文", 409, "assignment_exists")
                conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
                assignment_id = cur.lastrowid
                self._audit(conn, paper_id, chair_id, "assignment.invite", {"assignment_id": assignment_id, "reviewer_id": reviewer_id})
                return {"id": assignment_id, "paper_id": paper_id, "reviewer_id": reviewer_id, "status": "invited"}
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------
    # 一次分配：投标优先、冲突/人手跳过、容量满排队、变化后自动补位
    # ------------------------------------------------------------------

    @staticmethod
    def _active_slots(conn: sqlite3.Connection, paper_id: int) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status IN ({','.join('?' * len(SLOT_STATUSES))})",
            (paper_id, *SLOT_STATUSES),
        ).fetchone()[0]

    @staticmethod
    def _reviewer_load(conn: sqlite3.Connection, reviewer_id: str) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
            (reviewer_id, *ACTIVE_STATUSES),
        ).fetchone()[0]

    def _pop_waitlist(
        self, conn: sqlite3.Connection, paper_id: int, enqueued: set[str]
    ) -> sqlite3.Row | None:
        """取排队中意愿最高的一位（want > maybe > 无意向，同档按登记先后）。"""
        placeholders = ",".join("?" * len(enqueued)) if enqueued else ""
        sql = (
            """SELECT w.reviewer_id, u.load_limit,
                      CASE b.interest WHEN 'want' THEN 0 WHEN 'maybe' THEN 1 ELSE 2 END AS rank,
                      w.created_at
                 FROM waitlist w
                 JOIN users u ON u.id=w.reviewer_id
                 LEFT JOIN bids b ON b.paper_id=w.paper_id AND b.reviewer_id=w.reviewer_id
                WHERE w.paper_id=? AND w.reviewer_id NOT IN (
                    SELECT reviewer_id FROM assignments
                     WHERE paper_id=w.paper_id AND status IN ('declined','revoked')
                )"""
        )
        params: list = [paper_id]
        if enqueued:
            sql += f" AND w.reviewer_id NOT IN ({placeholders})"
            params.extend(enqueued)
        sql += " ORDER BY rank, w.created_at, w.reviewer_id LIMIT 1"
        return conn.execute(sql, params).fetchone()

    def _general_candidates(
        self, conn: sqlite3.Connection, paper_id: int, enqueued: set[str]
    ) -> list[sqlite3.Row]:
        """开放候选人：未排队、无冲突、未被邀请/拒绝过，按投标意愿和负载排序。"""
        placeholders = ",".join("?" * len(enqueued)) if enqueued else ""
        sql = (
            """SELECT u.id AS reviewer_id, u.load_limit,
                      CASE b.interest WHEN 'want' THEN 0 WHEN 'maybe' THEN 1 ELSE 2 END AS rank,
                      (SELECT COUNT(*) FROM assignments a2
                        WHERE a2.reviewer_id=u.id AND a2.status IN ('invited','accepted')) AS load
                 FROM users u
                 LEFT JOIN bids b ON b.paper_id=? AND b.reviewer_id=u.id
                WHERE u.role='reviewer'
                  AND NOT EXISTS (SELECT 1 FROM conflicts c WHERE c.paper_id=? AND c.reviewer_id=u.id)
                  AND NOT EXISTS (SELECT 1 FROM assignments a
                                   WHERE a.paper_id=? AND a.reviewer_id=u.id)
                  AND COALESCE(b.interest,'')!='decline'"""
        )
        params: list = [paper_id, paper_id, paper_id]
        if enqueued:
            sql += f" AND u.id NOT IN ({placeholders})"
            params.extend(enqueued)
        sql += " ORDER BY rank, load, u.id"
        return conn.execute(sql, params).fetchall()

    def _create_invitation(
        self,
        conn: sqlite3.Connection,
        paper_id: int,
        reviewer_id: str,
        actor: str,
        reason: str,
        from_waitlist: bool,
    ) -> int:
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO assignments(paper_id,reviewer_id,created_at,updated_at) VALUES(?,?,?,?)",
            (paper_id, reviewer_id, now, now),
        )
        assignment_id = cur.lastrowid
        conn.execute("DELETE FROM waitlist WHERE paper_id=? AND reviewer_id=?", (paper_id, reviewer_id))
        conn.execute("UPDATE papers SET status='under_review' WHERE id=?", (paper_id,))
        self._audit(
            conn, paper_id, actor, "assignment.invite",
            {"assignment_id": assignment_id, "reviewer_id": reviewer_id,
             "reason": reason, "from_waitlist": from_waitlist},
        )
        return assignment_id

    def _enqueue(
        self,
        conn: sqlite3.Connection,
        paper_id: int,
        reviewer_id: str,
        actor: str,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO waitlist(paper_id,reviewer_id,queued_by,reason,created_at) VALUES(?,?,?,?,?)",
            (paper_id, reviewer_id, actor, reason, utcnow()),
        )

    def _allocate_paper_locked(
        self, conn: sqlite3.Connection, paper_id: int, actor: str, reason: str
    ) -> dict:
        """在已持有的写事务内为一篇论文凑满两名评审人。必须幂等。"""
        invited: list[dict] = []
        enqueued: list[dict] = []
        seen: set[str] = set()

        # 先服务排队者：容量释放后按意愿顺序补位。
        while self._active_slots(conn, paper_id) < TARGET_REVIEWERS:
            row = self._pop_waitlist(conn, paper_id, seen)
            if row is None:
                break
            reviewer_id = row["reviewer_id"]
            seen.add(reviewer_id)
            # 硬不合格（冲突、已在论文上有有效分配）立即离队。
            if conn.execute(
                "SELECT 1 FROM conflicts WHERE paper_id=? AND reviewer_id=?", (paper_id, reviewer_id)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM assignments WHERE paper_id=? AND reviewer_id=? AND status NOT IN ('declined','revoked')",
                (paper_id, reviewer_id),
            ).fetchone():
                conn.execute("DELETE FROM waitlist WHERE paper_id=? AND reviewer_id=?", (paper_id, reviewer_id))
                seen.discard(reviewer_id)
                continue
            if self._reviewer_load(conn, reviewer_id) >= row["load_limit"]:
                # 队首仍满员：继续排队，本轮跳过；后续排队者/开放候选人可照常补位。
                continue
            assignment_id = self._create_invitation(conn, paper_id, reviewer_id, actor, reason, True)
            invited.append({"assignment_id": assignment_id, "reviewer_id": reviewer_id, "from_waitlist": True})

        # 再从开放候选人中按意愿补人：容量够直接邀请，满了排队等待补位。
        if self._active_slots(conn, paper_id) < TARGET_REVIEWERS:
            for cand in self._general_candidates(conn, paper_id, seen):
                if self._active_slots(conn, paper_id) >= TARGET_REVIEWERS:
                    break
                reviewer_id = cand["reviewer_id"]
                seen.add(reviewer_id)
                if cand["load"] >= cand["load_limit"]:
                    self._enqueue(conn, paper_id, reviewer_id, actor, reason)
                    enqueued.append(reviewer_id)
                    continue
                assignment_id = self._create_invitation(conn, paper_id, reviewer_id, actor, reason, False)
                invited.append({"assignment_id": assignment_id, "reviewer_id": reviewer_id, "from_waitlist": False})

        return {
            "paper_id": paper_id,
            "invited": invited,
            "enqueued": enqueued,
            "active": self._active_slots(conn, paper_id),
            "target": TARGET_REVIEWERS,
            "short": self._active_slots(conn, paper_id) < TARGET_REVIEWERS,
        }

    def _sweep_locked(self, conn: sqlite3.Connection, actor: str, reason: str) -> list[dict]:
        """容量变化后全局补位：排队者优先，所有未满论文尝试补齐。"""
        results: list[dict] = []
        paper_ids = [
            r[0]
            for r in conn.execute(
                f"""SELECT DISTINCT p.id FROM papers p
                    WHERE p.status IN ({','.join('?' * len(ASSIGNABLE_STATUSES))})
                    ORDER BY p.id""",
                ASSIGNABLE_STATUSES,
            ).fetchall()
        ]
        for paper_id in paper_ids:
            if self._active_slots(conn, paper_id) < TARGET_REVIEWERS:
                result = self._allocate_paper_locked(conn, paper_id, actor, reason)
                if result["invited"]:
                    results.append(result)
        return results

    def allocate_paper(self, chair_id: str, paper_id: int, reason: str = "manual") -> dict:
        chair = None
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                chair = self._user(conn, chair_id)
                self._require(chair, "chair")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper:
                    raise BusinessError("论文不存在", 404, "not_found")
                if paper["status"] not in ASSIGNABLE_STATUSES:
                    raise BusinessError("论文当前不可分配", 409, "paper_unavailable")
                result = self._allocate_paper_locked(conn, paper_id, chair_id, reason)
                return result
            except Exception:
                conn.rollback()
                raise

    def list_waitlist(self, chair_id: str, paper_id: int | None = None) -> list[dict]:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if paper_id is None:
                rows = conn.execute(
                    """SELECT w.*, CASE b.interest WHEN 'want' THEN 0 WHEN 'maybe' THEN 1 ELSE 2 END AS rank
                       FROM waitlist w
                       LEFT JOIN bids b ON b.paper_id=w.paper_id AND b.reviewer_id=w.reviewer_id
                       ORDER BY w.paper_id, rank, w.created_at""",
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT w.*, CASE b.interest WHEN 'want' THEN 0 WHEN 'maybe' THEN 1 ELSE 2 END AS rank
                       FROM waitlist w
                       LEFT JOIN bids b ON b.paper_id=w.paper_id AND b.reviewer_id=w.reviewer_id
                      WHERE w.paper_id=? ORDER BY rank, w.created_at""",
                    (paper_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def create_allocation_job(
        self, chair_id: str, paper_ids: list[int] | None = None, reason: str = "auto_allocate"
    ) -> dict:
        """为一批论文建一次分配任务；每篇独立提交，失败可从断点重试补齐。"""
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                chair = self._user(conn, chair_id)
                self._require(chair, "chair")
                now = utcnow()
                if paper_ids is None:
                    paper_ids = [
                        r[0]
                        for r in conn.execute(
                            f"SELECT id FROM papers WHERE status IN ({','.join('?' * len(ASSIGNABLE_STATUSES))}) ORDER BY id",
                            ASSIGNABLE_STATUSES,
                        ).fetchall()
                    ]
                else:
                    for pid in paper_ids:
                        paper = conn.execute("SELECT status FROM papers WHERE id=?", (pid,)).fetchone()
                        if not paper:
                            raise BusinessError(f"论文 {pid} 不存在", 404, "not_found")
                        if paper["status"] not in ASSIGNABLE_STATUSES:
                            raise BusinessError(f"论文 {pid} 当前不可分配", 409, "paper_unavailable")
                cur = conn.execute(
                    "INSERT INTO allocation_jobs(created_by,reason,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (chair_id, reason, "running", now, now),
                )
                job_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO allocation_items(job_id,paper_id,status,updated_at) VALUES(?,?,?,?)",
                    [(job_id, pid, "pending", now) for pid in paper_ids],
                )
                self._audit(conn, None, chair_id, "allocation_job.create",
                            {"job_id": job_id, "paper_ids": list(paper_ids), "reason": reason})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._run_allocation_job(job_id, retry=False)

    def _run_allocation_job(self, job_id: int, retry: bool) -> dict:
        lock = self._job_locks.setdefault(job_id, threading.Lock())
        if retry and not lock.acquire(blocking=False):
            raise BusinessError("该分配任务正在执行", 409, "job_running")
        if not retry:
            lock.acquire()
        try:
            with self.connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    job = conn.execute("SELECT * FROM allocation_jobs WHERE id=?", (job_id,)).fetchone()
                    if not job:
                        raise BusinessError("分配任务不存在", 404, "not_found")
                    if retry:
                        item_rows = conn.execute(
                            "SELECT * FROM allocation_items WHERE job_id=? ORDER BY paper_id", (job_id,)
                        ).fetchall()
                    else:
                        item_rows = conn.execute(
                            "SELECT * FROM allocation_items WHERE job_id=? AND status='pending' ORDER BY paper_id",
                            (job_id,),
                        ).fetchall()
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            failed_at: int | None = None
            for item in item_rows:
                paper_id = item["paper_id"]
                already_done = item["status"] == "completed"
                # 初次只跑 pending；重试时 pending/failed 重跑、completed 幂等补齐缺口。
                if already_done and not retry:
                    continue
                with self.connect() as conn:
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        if self.allocation_failure_paper_id == paper_id:
                            self.allocation_failure_paper_id = None
                            raise RuntimeError("simulated write failure")
                        run_reason = job["reason"] if not already_done else "job_retry"
                        result = self._allocate_paper_locked(conn, paper_id, job["created_by"], run_reason)
                        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
                        conn.execute(
                            "UPDATE allocation_items SET status='completed',result=?,error='',updated_at=? WHERE job_id=? AND paper_id=?",
                            (payload, utcnow(), job_id, paper_id),
                        )
                        if already_done and result["invited"]:
                            self._audit(conn, paper_id, job["created_by"], "allocation_job.refill",
                                        {"job_id": job_id, "reason": "job_retry", "invited": result["invited"]})
                        conn.commit()
                    except Exception as exc:
                        conn.rollback()
                        with self.connect() as fail_conn:
                            fail_conn.execute(
                                "UPDATE allocation_items SET status='failed',error=?,updated_at=? WHERE job_id=? AND paper_id=?",
                                (str(exc), utcnow(), job_id, paper_id),
                            )
                            fail_conn.execute(
                                "UPDATE allocation_jobs SET status='failed',updated_at=? WHERE id=?",
                                (utcnow(), job_id),
                            )
                            fail_conn.commit()
                        failed_at = paper_id
                        break

            if failed_at is None:
                with self.connect() as done_conn:
                    done_conn.execute(
                        "UPDATE allocation_jobs SET status='completed',updated_at=? WHERE id=?",
                        (utcnow(), job_id),
                    )
                    done_conn.commit()
            return self.get_allocation_job(job["created_by"], job_id)
        finally:
            lock.release()

    def retry_allocation_job(self, chair_id: str, job_id: int) -> dict:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            if not conn.execute("SELECT 1 FROM allocation_jobs WHERE id=?", (job_id,)).fetchone():
                raise BusinessError("分配任务不存在", 404, "not_found")
        return self._run_allocation_job(job_id, retry=True)

    def get_allocation_job(self, chair_id: str, job_id: int) -> dict:
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            job = conn.execute("SELECT * FROM allocation_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise BusinessError("分配任务不存在", 404, "not_found")
            items = conn.execute(
                "SELECT * FROM allocation_items WHERE job_id=? ORDER BY paper_id", (job_id,)
            ).fetchall()
            return {
                "id": job["id"],
                "status": job["status"],
                "reason": job["reason"],
                "created_by": job["created_by"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "items": [
                    {
                        "paper_id": row["paper_id"],
                        "status": row["status"],
                        "result": json.loads(row["result"]) if row["result"] else None,
                        "error": row["error"],
                    }
                    for row in items
                ],
            }

    def respond_assignment(self, reviewer_id: str, assignment_id: int, accepted: bool) -> dict:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
                if not row or row["reviewer_id"] != reviewer_id:
                    raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
                if row["status"] != "invited":
                    raise BusinessError("邀请已经处理", 409, "invitation_already_answered")
                status = "accepted" if accepted else "declined"
                conn.execute("UPDATE assignments SET status=?,updated_at=? WHERE id=?", (status, utcnow(), assignment_id))
                self._audit(conn, row["paper_id"], reviewer_id, "assignment.respond", {"assignment_id": assignment_id, "status": status})
                refill = []
                if not accepted:
                    paper = conn.execute("SELECT * FROM papers WHERE id=?", (row["paper_id"],)).fetchone()
                    if paper["status"] in ASSIGNABLE_STATUSES:
                        refill = [self._allocate_paper_locked(conn, row["paper_id"], SYSTEM_USER, "refill_decline")]
                        refill.extend(self._sweep_locked(conn, SYSTEM_USER, "refill_decline"))
                return {"id": assignment_id, "status": status, "refill": refill}
            except Exception:
                conn.rollback()
                raise

    def update_load_limit(self, chair_id: str, reviewer_id: str, load_limit: int) -> dict:
        """调整评审人负载；下调导致超额时，最新的待回复邀请立即失效并补人。"""
        if isinstance(load_limit, bool) or not isinstance(load_limit, int) or load_limit < 0:
            raise BusinessError("负载上限必须是非负整数", 422, "invalid_load_limit")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                chair = self._user(conn, chair_id)
                self._require(chair, "chair")
                reviewer = self._user(conn, reviewer_id)
                self._require(reviewer, "reviewer")
                conn.execute("UPDATE users SET load_limit=? WHERE id=?", (load_limit, reviewer_id))
                self._audit(conn, None, chair_id, "reviewer.load_limit",
                            {"reviewer_id": reviewer_id, "load_limit": load_limit})
                revoked: list[int] = []
                load = self._reviewer_load(conn, reviewer_id)
                # 已接受的承诺不能撤，只撤待回复邀请，从最新的开始。
                while load > load_limit:
                    row = conn.execute(
                        "SELECT id,paper_id FROM assignments WHERE reviewer_id=? AND status='invited' ORDER BY id DESC LIMIT 1",
                        (reviewer_id,),
                    ).fetchone()
                    if row is None:
                        break
                    conn.execute(
                        "UPDATE assignments SET status='revoked',updated_at=? WHERE id=?",
                        (utcnow(), row["id"]),
                    )
                    conn.execute("DELETE FROM waitlist WHERE paper_id=? AND reviewer_id=?", (row["paper_id"], reviewer_id))
                    self._audit(
                        conn, row["paper_id"], chair_id, "assignment.revoke",
                        {"assignment_id": row["id"], "reviewer_id": reviewer_id,
                         "reason": "refill_load", "load_limit": load_limit},
                    )
                    revoked.append(row["id"])
                    load -= 1
                refill = self._sweep_locked(conn, chair_id, "refill_load") if revoked else []
                return {"reviewer_id": reviewer_id, "load_limit": load_limit,
                        "revoked_assignment_ids": revoked, "refill": refill}
            except Exception:
                conn.rollback()
                raise

    def submit_review(self, reviewer_id: str, assignment_id: int, score: int, text: str) -> dict:
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise BusinessError("评分必须是 1 到 5 的整数", 422, "invalid_score")
        if len(text.strip()) < 10:
            raise BusinessError("评审意见至少 10 字", 422, "review_too_short")
        with self.connect() as conn:
            reviewer = self._user(conn, reviewer_id)
            self._require(reviewer, "reviewer")
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row or row["reviewer_id"] != reviewer_id:
                raise BusinessError("分配不存在或不属于当前评审人", 404, "not_found")
            if row["status"] != "accepted":
                raise BusinessError("只有已接受邀请的评审人可以提交评审", 409, "invalid_assignment_state")
            conn.execute(
                "UPDATE assignments SET status='completed',score=?,review_text=?,updated_at=? WHERE id=?",
                (score, text.strip(), utcnow(), assignment_id),
            )
            self._audit(conn, row["paper_id"], reviewer_id, "review.submit", {"assignment_id": assignment_id, "score": score})
            return {"id": assignment_id, "status": "completed", "score": score}

    def submit_rebuttal(self, author_id: str, paper_id: int, content: str) -> dict:
        if len(content.strip()) < 10:
            raise BusinessError("Rebuttal 至少 10 字", 422, "rebuttal_too_short")
        with self.connect() as conn:
            author = self._user(conn, author_id)
            self._require(author, "author")
            paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if not paper or paper["author_id"] != author_id:
                raise BusinessError("论文不存在或不属于当前作者", 404, "not_found")
            completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
            if completed < 1:
                raise BusinessError("至少收到一份完整评审后才能提交 Rebuttal", 409, "reviews_not_ready")
            try:
                cur = conn.execute(
                    "INSERT INTO rebuttals(paper_id,author_id,content,created_at) VALUES(?,?,?,?)",
                    (paper_id, author_id, content.strip(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("每篇论文只能提交一次 Rebuttal", 409, "rebuttal_exists")
            self._audit(conn, paper_id, author_id, "rebuttal.submit", {"rebuttal_id": cur.lastrowid})
            return {"id": cur.lastrowid, "paper_id": paper_id, "content": content.strip()}

    def decide(self, chair_id: str, paper_id: int, decision: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise BusinessError("决定值不合法", 422, "invalid_decision")
        with self.connect() as conn:
            chair = self._user(conn, chair_id)
            self._require(chair, "chair")
            try:
                conn.execute("BEGIN IMMEDIATE")
                paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                if not paper or paper["status"] not in {"submitted", "under_review"}:
                    raise BusinessError("论文不存在或已经决定", 409, "paper_decided")
                completed = conn.execute("SELECT COUNT(*) FROM assignments WHERE paper_id=? AND status='completed'", (paper_id,)).fetchone()[0]
                if completed < 2:
                    raise BusinessError("至少需要两份已完成评审才能作出决定", 409, "insufficient_reviews")
                cur = conn.execute(
                    "INSERT INTO decisions(paper_id,decision,note,decided_by,created_at) VALUES(?,?,?,?,?)",
                    (paper_id, decision, note.strip(), chair_id, utcnow()),
                )
                conn.execute("UPDATE papers SET status='decided' WHERE id=?", (paper_id,))
                self._audit(conn, paper_id, chair_id, "decision.record", {"decision": decision, "note": note.strip()})
                return {"id": cur.lastrowid, "paper_id": paper_id, "decision": decision, "note": note.strip()}
            except Exception:
                conn.rollback()
                raise

    def history(self, user_id: str, paper_id: int) -> list[dict]:
        self.get_paper(user_id, paper_id)  # 权限检查。
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log WHERE paper_id=? ORDER BY id", (paper_id,)).fetchall()
            return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = "AcademicReview/1.0"

    def _store(self) -> ReviewStore:
        return self.server.store  # type: ignore[attr-defined]

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")

    def _user_id(self) -> str:
        return self.headers.get("X-User-Id", "")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if method == "GET" and path == "/":
            html = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        store = self._store()
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "api":
            raise BusinessError("接口不存在", 404, "not_found")
        if parts == ["api", "papers"] and method == "GET":
            return self._send(200, {"items": store.list_papers(self._user_id())})
        if parts == ["api", "papers"] and method == "POST":
            data = self._body()
            return self._send(201, store.submit_paper(self._user_id(), data.get("title", ""), data.get("abstract", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "papers"]:
            paper_id = int(parts[2])
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_paper(self._user_id(), paper_id))
            if len(parts) == 4 and parts[3] == "bids" and method == "POST":
                data = self._body()
                return self._send(201, store.bid(self._user_id(), paper_id, data.get("interest", ""), data.get("note", "")))
            if len(parts) == 4 and parts[3] == "conflicts" and method == "POST":
                data = self._body()
                return self._send(201, store.add_conflict(self._user_id(), paper_id, data.get("reviewer_id", ""), data.get("reason", "")))
            if len(parts) == 4 and parts[3] == "assignments" and method == "POST":
                data = self._body()
                # 单篇一次分配：按投标意愿凑够两名评审人，容量满排队，可幂等重放。
                return self._send(200, store.allocate_paper(self._user_id(), paper_id, data.get("reason", "manual")))
            if len(parts) == 4 and parts[3] == "waitlist" and method == "GET":
                return self._send(200, {"items": store.list_waitlist(self._user_id(), paper_id)})
            if len(parts) == 4 and parts[3] == "allocate-jobs" and method == "POST":
                data = self._body()
                raw_ids = data.get("paper_ids")
                paper_ids = [int(x) for x in raw_ids] if isinstance(raw_ids, list) else None
                return self._send(201, store.create_allocation_job(self._user_id(), paper_ids, data.get("reason", "auto_allocate")))
            if len(parts) == 4 and parts[3] == "rebuttal" and method == "POST":
                data = self._body()
                return self._send(201, store.submit_rebuttal(self._user_id(), paper_id, data.get("content", "")))
            if len(parts) == 4 and parts[3] == "decision" and method == "POST":
                data = self._body()
                return self._send(201, store.decide(self._user_id(), paper_id, data.get("decision", ""), data.get("note", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET":
                return self._send(200, {"items": store.history(self._user_id(), paper_id)})
        if len(parts) == 4 and parts[:2] == ["api", "assignments"] and method == "POST":
            assignment_id = int(parts[2])
            data = self._body()
            if parts[3] == "respond":
                return self._send(200, store.respond_assignment(self._user_id(), assignment_id, bool(data.get("accepted"))))
            if parts[3] == "review":
                return self._send(201, store.submit_review(self._user_id(), assignment_id, data.get("score"), data.get("text", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "allocation-jobs"]:
            job_id = int(parts[2])
            if len(parts) == 4 and parts[3] == "retry" and method == "POST":
                return self._send(200, store.retry_allocation_job(self._user_id(), job_id))
            if len(parts) == 3 and method == "GET":
                return self._send(200, store.get_allocation_job(self._user_id(), job_id))
        if len(parts) == 4 and parts[:2] == ["api", "reviewers"] and parts[3] == "load-limit" and method == "POST":
            reviewer_id = parts[2]
            data = self._body()
            return self._send(200, store.update_load_limit(self._user_id(), reviewer_id, data.get("load_limit")))
        raise BusinessError("接口不存在", 404, "not_found")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except BusinessError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except ValueError:
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store: ReviewStore):
        self.store = store
        super().__init__(address, ReviewHandler)


def parse_args():
    parser = argparse.ArgumentParser(description="学术会议同行评审系统")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--init", action="store_true", help="初始化数据库")
    parser.add_argument("--seed", action="store_true", help="写入演示用户")
    parser.add_argument("--no-init", action="store_true", help="启动时不自动初始化")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    store = ReviewStore(args.db)
    if args.init or args.seed or not args.no_init:
        store.init_schema()
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    server = ReviewServer(("127.0.0.1", args.port), store)
    print(f"评审系统运行于 http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
