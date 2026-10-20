import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "jobs.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    key TEXT PRIMARY KEY, input TEXT NOT NULL, job_id TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def write(db, job):
        job["updated_at"] = now()
        db.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?)",
            (job["id"], job["status"], json.dumps(job, ensure_ascii=False)),
        )

    def create(self, key, source):
        with self.connection() as db:
            prior = db.execute("SELECT input, job_id FROM requests WHERE key=?", (key,)).fetchone()
            if prior:
                if prior[0] != source:
                    raise ValueError("同一个请求标识不能提交不同链接")
                return json.loads(
                    db.execute("SELECT data FROM jobs WHERE id=?", (prior[1],)).fetchone()[0]
                )
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running', 'needs_login')"
                ).fetchone()[0]
                >= 20
            ):
                raise ValueError("待处理任务已达 20 个，请先完成现有采集")
            job = {
                "id": uuid4().hex,
                "source_url": source,
                "status": "queued",
                "message": "等待采集",
                "created_at": now(),
                "updated_at": now(),
                "product": None,
                "error_code": None,
                "attempt": 1,
                "download_ready": False,
            }
            self.write(db, job)
            db.execute("INSERT INTO requests VALUES (?, ?, ?)", (key, source, job["id"]))
            return job

    def get(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self):
        with self.connection() as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT data FROM jobs ORDER BY rowid DESC LIMIT 100")
            ]

    def update(self, job_id, **changes):
        with self.connection() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = json.loads(row[0])
            job.update(changes)
            self.write(db, job)
            return job

    def claim(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(
                status="running", message="正在打开商品页面", error_code=None, download_ready=False
            )
            self.write(db, job)
            return job

    def resume(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = json.loads(row[0])
            if job["status"] not in ("needs_login", "failed", "partial", "interrupted"):
                raise ValueError("当前状态不需要继续或重新采集")
            job.update(
                status="queued",
                message="等待继续采集",
                error_code=None,
                download_ready=False,
                attempt=job["attempt"] + 1,
            )
            self.write(db, job)
            return job

    def recover(self):
        with self.connection() as db:
            for (raw,) in db.execute("SELECT data FROM jobs WHERE status='running'").fetchall():
                job = json.loads(raw)
                job.update(
                    status="interrupted",
                    message="服务上次退出时采集尚未完成，请点击重新采集",
                    download_ready=False,
                )
                self.write(db, job)
