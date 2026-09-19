"""Durable, bounded web operation queue with explicit interruption semantics.

Only one executor runs per API process. Existing provisioner host locks still
serialize operations with the CLI/Redis worker. In-flight work is never silently
replayed after a service restart; its result must be reviewed by the operator.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from ops.secureio import require_private_directory, require_private_file


class QueueConflict(ValueError):
    pass


class JobQueue:
    def __init__(self, directory: Path, execute, *, start=True, max_pending=50):
        self.directory = require_private_directory(directory, 'web job state', create=True)
        self.path = self.directory / 'jobs.sqlite3'
        if self.path.exists() or self.path.is_symlink():
            require_private_file(self.path, 'web job database')
        else:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.execute = execute
        self.max_pending = max_pending
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.degraded = False
        self.singleton = None
        # Gunicorn reloads can overlap generations even with workers=1. Hold
        # an OS lock before touching recovery state, not just a Python mutex.
        if start and os.name == 'posix':
            import fcntl
            lock_path = self.directory / 'executor.lock'
            if lock_path.exists() or lock_path.is_symlink():
                require_private_file(lock_path, 'web executor lock')
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(descriptor)
                raise RuntimeError('another web executor is still active') from None
            self.singleton = descriptor
        with self.connection() as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                action TEXT NOT NULL, device TEXT, actor TEXT NOT NULL, state TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                payload TEXT, result TEXT, error TEXT)''')
            # Queued requests may depend on now-stale operator intent as well.
            db.execute("UPDATE jobs SET state='interrupted', payload=NULL, updated_at=?, "
                       "error=? WHERE state IN ('queued','running')", (int(time.time()),
                       'سرویس از نو راه‌اندازی شد؛ وضعیت واقعی دستگاه را بررسی و درخواست را دوباره ثبت کنید.'))
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._loop, name='farm-web-jobs', daemon=True)
            self.thread.start()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row):
        return {key: (json.loads(row[key]) if key == 'result' and row[key] else row[key])
                for key in ('id', 'action', 'device', 'actor', 'state', 'created_at',
                            'updated_at', 'result', 'error')}

    def submit(self, key: str, payload: dict, actor: str):
        try:
            if str(uuid.UUID(key)) != key:
                raise ValueError()
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError('Idempotency-Key باید UUID معتبر باشد.') from exc
        serialized = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        now = int(time.time())
        with self.lock, self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint or existing['actor'] != actor:
                    raise QueueConflict('این کلید درخواست قبلاً برای عملیات دیگری استفاده شده است.')
                return self.public(existing), False
            pending = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
            if pending >= self.max_pending:
                raise QueueConflict('صف عملیات پر است؛ پس از پایان درخواست‌های فعلی تلاش کنید.')
            job_id = str(uuid.uuid4())
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                       (job_id, key, fingerprint, payload['action'], payload.get('device'), actor,
                        'queued', now, now, serialized))
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        self.wake.set()
        return self.public(row), True

    def list(self):
        with self.connection() as db:
            return [self.public(row) for row in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 200")]

    def cancel(self, job_id):
        with self.lock, self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise KeyError('درخواست پیدا نشد.')
            if row['state'] != 'queued':
                raise QueueConflict('فقط درخواست منتظر را می‌توان لغو کرد.')
            db.execute("UPDATE jobs SET state='cancelled', payload=NULL, updated_at=? WHERE id=?",
                       (int(time.time()), job_id))
            return self.public(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def run_one(self):
        with self.lock, self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return False
            db.execute("UPDATE jobs SET state='running', updated_at=? WHERE id=?",
                       (int(time.time()), row['id']))
        try:
            result = self.execute(json.loads(row['payload']))
            encoded = json.dumps(result, ensure_ascii=True)
            if len(encoded) > 16384:
                raise RuntimeError('operation result too large')
            state, error = 'succeeded', None
        except Exception as exc:
            # Neither arbitrary subprocess output nor submitted secrets go to
            # web responses/journal. Detailed host diagnosis remains private.
            state, encoded = 'failed', None
            error = 'عملیات در میزبان ناموفق بود؛ وضعیت دستگاه، پراکسی و پیش‌نیازها را بررسی کنید.'
            from .operations import OperationError
            if isinstance(exc, OperationError):
                error = str(exc)[:400]
        with self.lock, self.connection() as db:
            db.execute('UPDATE jobs SET state=?, result=?, error=?, payload=NULL, updated_at=? WHERE id=?',
                       (state, encoded, error, int(time.time()), row['id']))
        return True

    def _loop(self):
        while not self.closed.is_set():
            try:
                if self.degraded:
                    with self.lock, self.connection() as db:
                        db.execute("UPDATE jobs SET state='interrupted', payload=NULL, updated_at=?, error=? "
                                   "WHERE state='running'", (int(time.time()),
                                   'ذخیرهٔ نتیجه با خطا مواجه شد؛ پیش از تکرار، وضعیت واقعی را بررسی کنید.'))
                    self.degraded = False
                if not self.run_one():
                    self.wake.wait(1)
                    self.wake.clear()
            except Exception:
                # A failed DB write must neither kill the consumer silently nor
                # replay an operation whose host side effects may have finished.
                self.degraded = True
                self.closed.wait(2)

    def healthy(self):
        return not self.closed.is_set() and not self.degraded and (self.thread is None or self.thread.is_alive())

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.singleton is not None and (self.thread is None or not self.thread.is_alive()):
            os.close(self.singleton)
            self.singleton = None
