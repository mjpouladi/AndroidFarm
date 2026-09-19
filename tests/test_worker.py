import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock

from services.worker.worker import (RedisQueue, Task, Worker, WorkerConfig,
                                    command_for, redact)


class WorkerContractTests(unittest.TestCase):
    def config(self):
        return WorkerConfig((Path.cwd() / "test-provisioner.json").resolve(), lease_seconds=120,
                            command_timeout=60, block_seconds=1)

    def test_schema_rejects_commands_and_noncanonical_devices(self):
        task = Task.create("up", "num01", "ticket:123")
        self.assertEqual(Task.from_json(task.to_json()), task)
        value = json.loads(task.to_json())
        value["action"] = "exec"
        with self.assertRaisesRegex(ValueError, "unsupported action"):
            Task.from_json(json.dumps(value))
        value["action"] = "up"
        value["device"] = "num01;reboot"
        with self.assertRaisesRegex(ValueError, "canonical"):
            Task.from_json(json.dumps(value))
        value["device"] = "num01"
        value["argv"] = ["sh", "-c", "id"]
        with self.assertRaisesRegex(ValueError, "fields"):
            Task.from_json(json.dumps(value))

    def test_allowlisted_argv_never_uses_shell_text(self):
        task = Task.create("check", "num09", "health:num09:1")
        argv = command_for(task, self.config())
        self.assertEqual(argv[-3:], ["check", "--id", "num09"])
        self.assertNotIn("sh", [Path(value).name for value in argv])
        status = command_for(Task.create("status", None, "status:1"), self.config())
        self.assertEqual(status[-2:], ["status", "--json"])

    def test_logs_redact_credentials_and_secret_values(self):
        value = redact("redis://farm:very-secret@127.0.0.1 password=hunter2 token:abc")
        self.assertNotIn("very-secret", value)
        self.assertNotIn("hunter2", value)
        self.assertNotIn("abc", value)
        self.assertIn("REDACTED", value)

    def test_retry_is_bounded_and_final_failure_enters_dlq(self):
        client = MagicMock()
        client.eval.return_value = 1
        queue = RedisQueue(client, self.config())
        retry = Task.create("down", "num01", "down:1", max_attempts=3)
        retry = Task(**{**retry.__dict__, "attempt": 1})
        queue.fail(retry, retry.to_json(), "temporary password=hidden")
        transition = client.eval.call_args_list[0].args
        self.assertEqual(transition[12], "0")
        state = transition[13]
        self.assertNotIn("hidden", state)

        client.reset_mock()
        dead = Task(**{**retry.__dict__, "attempt": 3})
        queue.fail(dead, dead.to_json(), "permanent")
        transition = client.eval.call_args_list[0].args
        self.assertEqual(transition[12], "1")
        self.assertIn(queue.dlq, transition)
        self.assertIn(queue.terminal, transition)

    def test_worker_executes_fixed_argv_with_shell_disabled(self):
        task = Task.create("down", "num01", "down:2")
        raw = Task(**{**task.__dict__, "attempt": 1}).to_json()
        task = Task.from_json(raw)
        queue = MagicMock()
        queue.config = self.config()
        queue.acquire_device.return_value = True
        queue.log = MagicMock()
        calls = []

        def executor(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        Worker(queue, executor).execute_one(task, raw)
        self.assertEqual(calls[0][0][-3:], ["down", "--id", "num01"])
        self.assertIs(calls[0][1]["shell"], False)
        queue.finish.assert_called_once()

    def test_device_lock_contention_does_not_consume_an_attempt(self):
        client = MagicMock()
        client.eval.return_value = 1
        queue = RedisQueue(client, self.config())
        original = Task.create("up", "num01", "start:lock", max_attempts=2)
        reserved = Task(**{**original.__dict__, "attempt": 2})
        queue.defer_locked(reserved, reserved.to_json())
        scheduled = client.eval.call_args.args[9]
        self.assertEqual(Task.from_json(scheduled).attempt, 1)

    def test_terminal_transition_refuses_a_stale_lease_owner(self):
        client = MagicMock()
        # The transition loses to reclaim; lock release also reports no owner.
        client.eval.side_effect = [0, 0]
        queue = RedisQueue(client, self.config())
        task = Task.create("down", "num01", "stale:1")
        task = Task(**{**task.__dict__, "attempt": 1})
        self.assertFalse(queue.finish(task, task.to_json(), 1.25))
        transition = client.eval.call_args_list[0].args
        self.assertIn("HGET", transition[0])
        self.assertIn("LREM", transition[0])

    def test_queue_stats_exposes_only_counts(self):
        client = MagicMock()
        pipeline = MagicMock()
        pipeline.__enter__.return_value = pipeline
        pipeline.__exit__.return_value = False
        pipeline.execute.return_value = [2, 1, 3, 1, 4, 9, 5]
        client.pipeline.return_value = pipeline
        stats = RedisQueue(client, self.config()).stats()
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["dead_letter"], 4)
        self.assertNotIn("payload", stats)


if __name__ == "__main__":
    unittest.main()
