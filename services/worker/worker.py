#!/usr/bin/env python3
"""Reliable Redis task worker for the host-only Android Farm control plane.

The worker deliberately exposes a very small command vocabulary.  Queue data
can select an action and a canonical device id; it can never provide a shell
fragment, executable, path, environment variable, or application argument.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable

try:
    import redis
except ImportError:  # Unit tests can exercise the pure contract without redis-py.
    redis = None


SCHEMA_VERSION = 1
ALLOWED_ACTIONS = frozenset({"up", "down", "restart", "check", "status", "health"})
DEVICE_RE = re.compile(r"num[0-9]{2,6}\Z")
SAFE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
PREFIX = "android-farm:tasks"
ROOT = Path(__file__).resolve().parents[2]
PROVISIONER = ROOT / "provisioner.py"
HEALTHCHECK = ROOT / "ops" / "healthcheck.py"


def _now() -> int:
    return int(time.time())


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def redact(value: object) -> str:
    """Return a bounded log-safe representation, including URL credential removal."""
    text = str(value)
    text = re.sub(r"(?i)(password|passwd|token|secret)(\s*[=:]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@", r"\1[REDACTED]@", text)
    return text[:500]


@dataclass(frozen=True)
class Task:
    schema_version: int
    task_id: str
    idempotency_key: str
    action: str
    device: str | None
    created_at: int
    attempt: int = 0
    max_attempts: int = 4

    def validate(self) -> "Task":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported task schema_version")
        try:
            uuid.UUID(self.task_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("task_id must be a UUID") from exc
        if not SAFE_KEY_RE.fullmatch(self.idempotency_key):
            raise ValueError("invalid idempotency_key")
        if self.action not in ALLOWED_ACTIONS:
            raise ValueError("unsupported action")
        if self.action == "status":
            if self.device is not None:
                raise ValueError("status must not select a device")
        elif not isinstance(self.device, str) or not DEVICE_RE.fullmatch(self.device):
            raise ValueError("action requires a canonical numXX device")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, int) or self.created_at <= 0:
            raise ValueError("created_at must be a positive integer")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise ValueError("attempt must be a non-negative integer")
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if self.attempt > self.max_attempts:
            raise ValueError("attempt exceeds max_attempts")
        return self

    def to_json(self) -> str:
        self.validate()
        return _json(self.__dict__)

    @classmethod
    def from_json(cls, raw: str) -> "Task":
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("task is not valid JSON") from exc
        expected = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("task fields do not match the versioned schema")
        return cls(**value).validate()

    @classmethod
    def create(cls, action: str, device: str | None, idempotency_key: str,
               max_attempts: int = 4, clock: Callable[[], int] = _now) -> "Task":
        return cls(SCHEMA_VERSION, str(uuid.uuid4()), idempotency_key, action, device,
                   clock(), 0, max_attempts).validate()


@dataclass(frozen=True)
class WorkerConfig:
    provisioner_config: Path = Path("/etc/android-farm/provisioner.json")
    lease_seconds: int = 3600
    command_timeout: int = 3540
    block_seconds: int = 5
    backoff_base: int = 10
    backoff_max: int = 300
    result_ttl_seconds: int = 7 * 24 * 60 * 60

    def validate(self) -> "WorkerConfig":
        if not self.provisioner_config.is_absolute():
            raise ValueError("provisioner config path must be absolute")
        if not 60 <= self.lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 60 and 3600")
        if not 1 <= self.command_timeout < self.lease_seconds:
            raise ValueError("command_timeout must be positive and shorter than the lease")
        if not 3600 <= self.result_ttl_seconds <= 90 * 24 * 60 * 60:
            raise ValueError("result_ttl_seconds must be between one hour and 90 days")
        return self


def command_for(task: Task, config: WorkerConfig) -> list[str]:
    """Build a fixed argv vector.  No queued string becomes an executable or option."""
    task.validate()
    config.validate()
    base = [sys.executable, str(PROVISIONER), "--config", str(config.provisioner_config)]
    if task.action == "status":
        return [*base, "status", "--json"]
    if task.action == "health":
        # Module mode keeps the package-relative imports of ops/ working.
        return [sys.executable, "-m", "ops.healthcheck", "--config", str(config.provisioner_config),
                "--device", task.device]
    return [*base, task.action, "--id", task.device]


def task_result(state: str, task: Task, **fields) -> str:
    return _json({"schema_version": SCHEMA_VERSION, "state": state, "task_id": task.task_id,
                  "action": task.action, "device": task.device, "attempt": task.attempt,
                  "updated_at": _now(), **fields})


class JsonLogger:
    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("android-farm-worker")

    def emit(self, level: int, event: str, task: Task | None = None, **fields) -> None:
        record = {"event": event, "timestamp": _now()}
        if task:
            record.update(task_id=task.task_id, action=task.action, device=task.device,
                          attempt=task.attempt)
        record.update({key: redact(value) for key, value in fields.items()})
        self.logger.log(level, _json(record))


class RedisQueue:
    """Redis reliable-list queue with leases, delayed retry, DLQ and idempotency."""
    pending = f"{PREFIX}:pending"
    processing = f"{PREFIX}:processing"
    scheduled = f"{PREFIX}:scheduled"
    leases = f"{PREFIX}:leases"
    running = f"{PREFIX}:running"
    claims = f"{PREFIX}:claims"
    results = f"{PREFIX}:results"
    dlq = f"{PREFIX}:dead-letter"
    idempotency = f"{PREFIX}:idempotency"
    task_keys = f"{PREFIX}:task-keys"
    terminal = f"{PREFIX}:terminal-expiry"
    archives = f"{PREFIX}:terminal-raw"

    def __init__(self, client, config: WorkerConfig, logger: JsonLogger | None = None):
        self.client = client
        self.config = config.validate()
        self.log = logger or JsonLogger()

    def enqueue(self, task: Task) -> tuple[str, bool]:
        raw = task.to_json()
        script = """
        local existing = redis.call('HGET', KEYS[1], ARGV[1])
        if existing then return {existing, '0'} end
        redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
        redis.call('HSET', KEYS[2], ARGV[2], ARGV[3])
        redis.call('LPUSH', KEYS[3], ARGV[4])
        redis.call('HSET', KEYS[4], ARGV[2], ARGV[1])
        return {ARGV[2], '1'}
        """
        result = self.client.eval(script, 4, self.idempotency, self.results, self.pending,
                                  self.task_keys,
                                  task.idempotency_key, task.task_id,
                                  task_result("queued", task), raw)
        return str(result[0]), str(result[1]) == "1"

    def prune(self, now: int | None = None, limit: int = 200) -> int:
        """Expire terminal results, idempotency keys and retained DLQ payloads together."""
        current = now or _now()
        task_ids = self.client.zrangebyscore(self.terminal, "-inf", current, start=0, num=limit)
        removed = 0
        script = """
        if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
        local idkey = redis.call('HGET', KEYS[2], ARGV[1])
        local raw = redis.call('HGET', KEYS[3], ARGV[1])
        if raw then redis.call('LREM', KEYS[4], 1, raw) end
        if idkey and redis.call('HGET', KEYS[5], idkey) == ARGV[1] then
          redis.call('HDEL', KEYS[5], idkey)
        end
        redis.call('HDEL', KEYS[6], ARGV[1])
        redis.call('HDEL', KEYS[2], ARGV[1])
        redis.call('HDEL', KEYS[3], ARGV[1])
        redis.call('ZREM', KEYS[1], ARGV[1])
        return 1
        """
        for task_id in task_ids:
            removed += int(self.client.eval(
                script, 6, self.terminal, self.task_keys, self.archives,
                self.dlq, self.idempotency, self.results, task_id
            ))
        return removed

    def stats(self) -> dict[str, int]:
        """Return queue depth without exposing task payloads or credentials."""
        with self.client.pipeline(transaction=False) as pipe:
            pipe.llen(self.pending)
            pipe.llen(self.processing)
            pipe.zcard(self.scheduled)
            pipe.hlen(self.running)
            pipe.llen(self.dlq)
            pipe.hlen(self.results)
            pipe.zcard(self.terminal)
            values = pipe.execute()
        names = ("pending", "processing", "scheduled", "running", "dead_letter",
                 "results", "terminal_retained")
        return {name: int(value) for name, value in zip(names, values)}

    def promote_due(self, now: int | None = None, limit: int = 100) -> int:
        due = self.client.zrangebyscore(self.scheduled, "-inf", now or _now(), start=0, num=limit)
        moved = 0
        for raw in due:
            script = """
            if redis.call('ZREM', KEYS[1], ARGV[1]) == 1 then
              redis.call('LPUSH', KEYS[2], ARGV[1]); return 1
            end
            return 0
            """
            moved += int(self.client.eval(script, 2, self.scheduled, self.pending, raw))
        return moved

    def reclaim_expired(self, now: int | None = None, limit: int = 100) -> int:
        expired = self.client.zrangebyscore(self.leases, "-inf", now or _now(), start=0, num=limit)
        reclaimed = 0
        for task_id in expired:
            raw = self.client.hget(self.running, task_id)
            if not raw:
                self.client.zrem(self.leases, task_id)
                continue
            try:
                task = Task.from_json(raw)
                dead = task.attempt >= task.max_attempts
                result = task_result("dead" if dead else "retrying", task,
                                     error="lease expired at retry limit" if dead else "worker lease expired")
            except ValueError:
                dead = True
                result = _json({"schema_version": SCHEMA_VERSION, "state": "dead",
                                "task_id": task_id, "error": "invalid leased task", "updated_at": _now()})
            destination = self.dlq if dead else self.pending
            script = """
            local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
            if not score or tonumber(score) > tonumber(ARGV[3]) then return 0 end
            if redis.call('HGET', KEYS[2], ARGV[1]) ~= ARGV[2] then return 0 end
            redis.call('LREM', KEYS[3], 1, ARGV[2])
            redis.call('ZREM', KEYS[1], ARGV[1])
            redis.call('HDEL', KEYS[2], ARGV[1])
            redis.call('LPUSH', KEYS[4], ARGV[2])
            redis.call('HSET', KEYS[5], ARGV[1], ARGV[4])
            if ARGV[6] == '1' then
              redis.call('ZADD', KEYS[6], ARGV[5], ARGV[1])
              redis.call('HSET', KEYS[7], ARGV[1], ARGV[2])
            end
            return 1
            """
            reclaimed += int(self.client.eval(
                script, 7, self.leases, self.running, self.processing, destination,
                self.results, self.terminal, self.archives, task_id, raw,
                now or _now(), result,
                (now or _now()) + self.config.result_ttl_seconds, "1" if dead else "0"
            ))
        return reclaimed

    def reclaim_orphans(self, now: int | None = None, limit: int = 100) -> int:
        """Recover a worker crash between the atomic list move and lease setup."""
        cutoff = (now or _now()) - max(10, self.config.block_seconds * 3)
        values = self.client.hgetall(self.claims)
        reclaimed = 0
        for raw, claim in list(values.items())[:limit]:
            try:
                stale = int(str(claim).partition(":")[0]) <= cutoff
            except (TypeError, ValueError):
                stale = True
            if not stale:
                continue
            script = """
            if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
            local removed = redis.call('LREM', KEYS[2], 1, ARGV[1])
            redis.call('HDEL', KEYS[1], ARGV[1])
            if removed == 1 then redis.call('LPUSH', KEYS[3], ARGV[1]) end
            return removed
            """
            reclaimed += int(self.client.eval(script, 3, self.claims, self.processing,
                                               self.pending, raw, claim))
        return reclaimed

    def _discard_raw(self, task_id: str, raw: str) -> None:
        with self.client.pipeline() as pipe:
            pipe.lrem(self.processing, 1, raw)
            pipe.zrem(self.leases, task_id)
            pipe.hdel(self.running, task_id)
            pipe.hdel(self.claims, raw)
            pipe.execute()

    def reserve(self) -> tuple[Task, str] | None:
        # A Lua move plus claim timestamp closes the otherwise unavoidable
        # crash gap between BRPOPLPUSH and recording a visibility lease.
        claim = f"{_now()}:{uuid.uuid4()}"
        script = """
        local raw = redis.call('RPOP', KEYS[1])
        if not raw then return false end
        redis.call('LPUSH', KEYS[2], raw)
        redis.call('HSET', KEYS[3], raw, ARGV[1])
        return raw
        """
        raw = self.client.eval(script, 3, self.pending, self.processing, self.claims, claim)
        if raw is None:
            return None
        try:
            queued = Task.from_json(raw)
        except ValueError as exc:
            self.client.lrem(self.processing, 1, raw)
            self.client.hdel(self.claims, raw)
            self.client.lpush(self.dlq, raw)
            self.log.emit(logging.ERROR, "invalid-task", error=exc)
            return None
        if queued.attempt >= queued.max_attempts:
            with self.client.pipeline() as pipe:
                pipe.lrem(self.processing, 1, raw)
                pipe.hdel(self.claims, raw)
                pipe.lpush(self.dlq, raw)
                pipe.hset(self.results, queued.task_id,
                          task_result("dead", queued, error="retry limit reached before reservation"))
                pipe.hset(self.archives, queued.task_id, raw)
                pipe.zadd(self.terminal,
                          {queued.task_id: _now() + self.config.result_ttl_seconds})
                pipe.execute()
            return None
        task = replace(queued, attempt=queued.attempt + 1).validate()
        running_raw = task.to_json()
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        if redis.call('LREM', KEYS[2], 1, ARGV[1]) ~= 1 then return 0 end
        redis.call('HDEL', KEYS[1], ARGV[1])
        redis.call('LPUSH', KEYS[2], ARGV[3])
        redis.call('HSET', KEYS[3], ARGV[4], ARGV[3])
        redis.call('ZADD', KEYS[4], ARGV[5], ARGV[4])
        redis.call('HSET', KEYS[5], ARGV[4], ARGV[6])
        return 1
        """
        claimed = self.client.eval(script, 5, self.claims, self.processing, self.running,
                                   self.leases, self.results, raw, claim, running_raw,
                                   task.task_id, _now() + self.config.lease_seconds,
                                   task_result("running", task))
        if not claimed:
            return None
        return task, running_raw

    def acquire_device(self, task: Task) -> bool:
        key = f"{PREFIX}:lock:{task.device or 'global'}"
        return bool(self.client.set(key, task.task_id, nx=True, ex=self.config.lease_seconds + 60))

    def renew(self, task: Task) -> None:
        key = f"{PREFIX}:lock:{task.device or 'global'}"
        script = """
        if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
        redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
        redis.call('EXPIRE', KEYS[1], ARGV[3])
        return 1
        """
        renewed = self.client.eval(script, 2, key, self.leases, task.task_id,
                                   _now() + self.config.lease_seconds,
                                   self.config.lease_seconds + 60)
        if not renewed:
            raise RuntimeError("device lock was lost while renewing task lease")

    def release_device(self, task: Task) -> None:
        key = f"{PREFIX}:lock:{task.device or 'global'}"
        script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
        self.client.eval(script, 1, key, task.task_id)

    def finish(self, task: Task, raw: str, duration: float) -> bool:
        """Commit success only while this worker still owns the visibility lease.

        A lease reclaimer and a finishing worker can run at the same instant.  A
        normal pipeline does not compare ownership, so it could leave a retry in
        ``pending`` while also publishing success.  The Lua transition makes
        removal from ``processing`` and the terminal result one indivisible,
        ownership-checked operation.
        """
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        if redis.call('LREM', KEYS[2], 1, ARGV[2]) ~= 1 then return 0 end
        redis.call('HDEL', KEYS[1], ARGV[1])
        redis.call('ZREM', KEYS[3], ARGV[1])
        redis.call('HSET', KEYS[4], ARGV[1], ARGV[3])
        redis.call('ZADD', KEYS[5], ARGV[4], ARGV[1])
        return 1
        """
        committed = bool(self.client.eval(
            script, 5, self.running, self.processing, self.leases, self.results,
            self.terminal, task.task_id, raw,
            task_result("succeeded", task, duration_seconds=round(duration, 3)),
            _now() + self.config.result_ttl_seconds,
        ))
        self.release_device(task)
        return committed

    def fail(self, task: Task, raw: str, error: str) -> bool:
        safe_error = redact(error)
        dead = task.attempt >= task.max_attempts
        delay = min(self.config.backoff_max, self.config.backoff_base * (2 ** max(0, task.attempt - 1)))
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        if redis.call('LREM', KEYS[2], 1, ARGV[2]) ~= 1 then return 0 end
        redis.call('HDEL', KEYS[1], ARGV[1])
        redis.call('ZREM', KEYS[3], ARGV[1])
        if ARGV[3] == '1' then
          redis.call('LPUSH', KEYS[6], ARGV[2])
          redis.call('HSET', KEYS[7], ARGV[1], ARGV[2])
          redis.call('ZADD', KEYS[8], ARGV[6], ARGV[1])
        else
          redis.call('ZADD', KEYS[5], ARGV[5], ARGV[2])
        end
        redis.call('HSET', KEYS[4], ARGV[1], ARGV[4])
        return 1
        """
        result = (task_result("dead", task, error=safe_error) if dead else
                  task_result("retrying", task, retry_in_seconds=delay, error=safe_error))
        committed = bool(self.client.eval(
            script, 8, self.running, self.processing, self.leases, self.results,
            self.scheduled, self.dlq, self.archives, self.terminal,
            task.task_id, raw, "1" if dead else "0", result, _now() + delay,
            _now() + self.config.result_ttl_seconds,
        ))
        self.release_device(task)
        return committed

    def defer_locked(self, task: Task, raw: str) -> bool:
        # Waiting for another task on the same device is not an execution
        # attempt.  Put the pre-reservation attempt back so lock contention can
        # never exhaust retry policy or create an invalid attempt=max+1 task.
        queued_raw = replace(task, attempt=max(0, task.attempt - 1)).to_json()
        script = """
        if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
        if redis.call('LREM', KEYS[2], 1, ARGV[2]) ~= 1 then return 0 end
        redis.call('HDEL', KEYS[1], ARGV[1])
        redis.call('ZREM', KEYS[3], ARGV[1])
        redis.call('ZADD', KEYS[4], ARGV[4], ARGV[3])
        redis.call('HSET', KEYS[5], ARGV[1], ARGV[5])
        return 1
        """
        return bool(self.client.eval(
            script, 5, self.running, self.processing, self.leases,
            self.scheduled, self.results, task.task_id, raw, queued_raw,
            _now() + 5, task_result("waiting_for_device_lock", task),
        ))


class Worker:
    def __init__(self, queue: RedisQueue, executor: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.queue = queue
        self.executor = executor
        self.stopping = threading.Event()

    def _lease_heartbeat(self, task: Task, finished: threading.Event) -> None:
        interval = min(30, max(10, self.queue.config.lease_seconds // 3))
        while not self.stopping.is_set() and not finished.wait(interval):
            try:
                self.queue.renew(task)
            except Exception as exc:  # Redis outage is visible; command timeout still bounds execution.
                self.queue.log.emit(logging.ERROR, "lease-renewal-failed", task, error=exc)

    def execute_one(self, task: Task, raw: str) -> None:
        if not self.queue.acquire_device(task):
            if not self.queue.defer_locked(task, raw):
                self.queue.log.emit(logging.WARNING, "lock-defer-lost-lease", task)
            return
        started = time.monotonic()
        finished = threading.Event()
        heartbeat = threading.Thread(target=self._lease_heartbeat, args=(task, finished), daemon=True)
        heartbeat.start()
        try:
            argv = command_for(task, self.queue.config)
            result = self.executor(argv, cwd=ROOT, text=True, capture_output=True,
                                   timeout=self.queue.config.command_timeout, shell=False)
            finished.set()
            heartbeat.join(timeout=2)
            if result.returncode:
                raise RuntimeError(f"managed command failed with exit code {result.returncode}: {redact(result.stderr)}")
            committed = self.queue.finish(task, raw, time.monotonic() - started)
            if committed:
                digest = hashlib.sha256((result.stdout or "").encode("utf-8")).hexdigest()[:16]
                self.queue.log.emit(logging.INFO, "task-succeeded", task, output_sha256=digest)
            else:
                self.queue.log.emit(logging.WARNING, "completion-lost-lease", task)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            finished.set()
            heartbeat.join(timeout=2)
            committed = self.queue.fail(task, raw, str(exc))
            self.queue.log.emit(logging.ERROR if committed else logging.WARNING,
                                "task-failed" if committed else "failure-lost-lease",
                                task, error=exc)
        finally:
            finished.set()

    def run(self, once: bool = False) -> None:
        while not self.stopping.is_set():
            self.queue.prune()
            self.queue.promote_due()
            self.queue.reclaim_orphans()
            self.queue.reclaim_expired()
            reserved = self.queue.reserve()
            if reserved:
                self.execute_one(*reserved)
            else:
                self.stopping.wait(self.queue.config.block_seconds)
            if once:
                return


def redis_client(url: str):
    if redis is None:
        raise RuntimeError("redis-py is required; install services/worker/requirements.txt")
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5,
                                socket_timeout=10, health_check_interval=30)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--redis-url", default=os.environ.get("ANDROID_FARM_REDIS_URL", "redis://127.0.0.1:6379/0"))
    result.add_argument("--config", type=Path, default=Path("/etc/android-farm/provisioner.json"))
    result.add_argument("--result-ttl", type=int,
                        default=int(os.environ.get("ANDROID_FARM_RESULT_TTL_SECONDS", 604800)))
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("--action", required=True, choices=sorted(ALLOWED_ACTIONS))
    enqueue.add_argument("--device")
    enqueue.add_argument("--idempotency-key", required=True)
    enqueue.add_argument("--max-attempts", type=int, default=4)
    status = commands.add_parser("result")
    status.add_argument("--task-id", required=True)
    commands.add_parser("stats")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    client = redis_client(args.redis_url)
    config = WorkerConfig(provisioner_config=args.config.resolve(),
                          result_ttl_seconds=args.result_ttl).validate()
    queue = RedisQueue(client, config)
    if args.command == "enqueue":
        task = Task.create(args.action, args.device, args.idempotency_key, args.max_attempts)
        task_id, created = queue.enqueue(task)
        print(_json({"task_id": task_id, "created": created}))
        return 0
    if args.command == "result":
        try:
            uuid.UUID(args.task_id)
        except ValueError as exc:
            raise ValueError("task id must be a UUID") from exc
        value = client.hget(queue.results, args.task_id)
        print(value or _json({"task_id": args.task_id, "state": "unknown"}))
        return 0
    if args.command == "stats":
        print(_json(queue.stats()))
        return 0
    worker = Worker(queue)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: worker.stopping.set())
    worker.run(args.once)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(redact(exc), file=sys.stderr)
        raise SystemExit(1)
