import base64
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import uuid

from services.api.jobs import JobQueue, QueueConflict
from services.api.server import Application, BasicAuth, MAX_BODY


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'private'
        self.operations = Mock()
        self.operations.snapshot.return_value = {'collected_at': 123, 'devices': [], 'errors': []}
        self.operations.validate_job.side_effect = lambda value: value
        self.operations.execute.return_value = {'completed': True}
        self.queue = JobQueue(self.directory, self.operations.execute, start=False)
        self.auth = Mock()
        self.auth.revision.return_value = 'test-generation'
        self.auth.verify.side_effect = lambda header: 'operator' if header == 'valid' else None
        self.app = Application(self.operations, self.queue, self.auth, ['https://farm.example.com'])

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def request(self, path='/api/v1/snapshot', method='GET', body=None, **overrides):
        raw = json.dumps(body or {}).encode()
        env = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': '',
               'HTTP_AUTHORIZATION': 'valid', 'HTTP_ORIGIN': 'https://farm.example.com',
               'HTTP_X_FARM_CSRF': self.app.csrf, 'CONTENT_TYPE': 'application/json',
               'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': io.BytesIO(raw),
               'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        env.update(overrides)
        headers = []
        output = self.app(env, lambda status, values: headers.extend([status, dict(values)]))
        return int(headers[0].split()[0]), headers[1], json.loads(b''.join(output))

    def upload(self, body=b'PK\x03\x04apk', **overrides):
        env = {'REQUEST_METHOD': 'POST', 'PATH_INFO': '/api/v1/artifacts/upload', 'QUERY_STRING': '',
               'HTTP_AUTHORIZATION': 'valid', 'HTTP_ORIGIN': 'https://farm.example.com',
               'HTTP_X_FARM_CSRF': self.app.csrf, 'CONTENT_TYPE': 'application/vnd.android.package-archive',
               'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body),
               'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4()),
               'HTTP_X_FARM_ARTIFACT_LABEL': '%D9%88%D8%A7%D8%AA%D8%B3%E2%80%8C%D8%A7%D9%BE',
               'HTTP_X_FARM_ARTIFACT_FILENAME': 'WhatsApp.apk',
               'HTTP_X_FARM_ARTIFACT_PERMISSIONS': 'android.permission.CAMERA'}
        env.update(overrides)
        headers = []
        output = self.app(env, lambda status, values: headers.extend([status, dict(values)]))
        return int(headers[0].split()[0]), headers[1], json.loads(b''.join(output))

    def test_apk_upload_is_staged_privately_and_imported_through_the_queue(self):
        self.operations.stage_upload.return_value = {'upload': 'f' * 32}
        code, _, body = self.upload()
        self.assertEqual(code, 202)
        stream, length = self.operations.stage_upload.call_args.args
        self.assertEqual(length, 7)
        self.assertIsInstance(stream, io.BytesIO)
        submitted = self.operations.validate_job.call_args.args[0]
        self.assertEqual(submitted['action'], 'artifact-import')
        self.assertEqual(submitted['params'], {'upload': 'f' * 32, 'label': 'واتس‌اپ', 'filename': 'WhatsApp.apk',
                                               'permissions': ['android.permission.CAMERA']})
        self.assertEqual(body['job']['action'], 'artifact-import')
        self.assertEqual(self.queue.list()[0]['state'], 'queued')
        self.operations.discard_upload.assert_not_called()

    def test_apk_upload_rejects_wrong_type_size_origin_and_cleans_up_on_validation_failure(self):
        self.operations.stage_upload.return_value = {'upload': 'f' * 32}
        self.assertEqual(self.upload(CONTENT_TYPE='application/json')[0], 415)
        self.assertEqual(self.upload(CONTENT_LENGTH=str(256 * 1024 ** 2 + 1))[0], 413)
        self.assertEqual(self.upload(HTTP_ORIGIN='https://evil.example')[0], 403)
        self.assertEqual(self.upload(HTTP_X_FARM_CSRF='guess')[0], 403)
        self.assertEqual(self.upload(HTTP_AUTHORIZATION='')[0], 401)
        self.assertEqual(self.upload(QUERY_STRING='x=1')[0], 400)
        self.operations.stage_upload.assert_not_called()
        self.operations.validate_job.side_effect = ValueError('unsupported Android runtime permissions')
        code, _, body = self.upload(HTTP_X_FARM_ARTIFACT_PERMISSIONS='android.permission.ROOT')
        self.assertEqual(code, 400)
        self.assertIn('permissions', body['error']['message'])
        self.operations.discard_upload.assert_called_once_with('f' * 32)
        self.assertEqual(self.queue.list(), [])

    def test_auth_required_even_without_reverse_proxy(self):
        for path, method in [('/api/v1/snapshot', 'GET'), ('/api/v1/health', 'GET'),
                             ('/api/v1/jobs', 'POST')]:
            code, headers, _ = self.request(path, method, HTTP_AUTHORIZATION='')
            self.assertEqual(code, 401)
            self.assertIn('Basic', headers['WWW-Authenticate'])
        self.operations.snapshot.assert_not_called()
        self.assertEqual(self.queue.list(), [])

    def test_real_snapshot_is_private_and_polls_are_coalesced(self):
        for _ in range(2):
            code, headers, body = self.request()
            self.assertEqual(code, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(body['devices'], [])
            self.assertEqual(body['csrf_token'], self.app.csrf)
        self.operations.snapshot.assert_called_once()

    def test_csrf_and_cross_origin_requests_cannot_queue_work(self):
        cases = [{'HTTP_ORIGIN': ''}, {'HTTP_ORIGIN': 'https://evil.example'},
                 {'HTTP_X_FARM_CSRF': 'wrong'}, {'HTTP_SEC_FETCH_SITE': 'cross-site'}]
        for override in cases:
            self.assertEqual(self.request('/api/v1/jobs', 'POST', {'action': 'up'}, **override)[0], 403)
        self.assertEqual(self.queue.list(), [])

    def test_body_limits_content_type_and_unknown_routes(self):
        for override, expected in [({'CONTENT_LENGTH': str(MAX_BODY + 1)}, 413),
                                   ({'CONTENT_LENGTH': '-1'}, 400),
                                   ({'CONTENT_TYPE': 'text/plain'}, 415),
                                   ({'wsgi.input': io.BytesIO(b'{')}, 400)]:
            self.assertEqual(self.request('/api/v1/jobs', 'POST', {}, **override)[0], expected)
        for path in ['/api/v1/shell', '/api/v1/jobs/../snapshot', '/api/v1/docker/containers']:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.queue.list(), [])

    def test_credential_changes_require_current_password_before_queueing(self):
        payload = {'action': 'credential-rotate', 'params': {'target': 'web', 'username': 'qa-admin',
                   'password': 'new-private-canary', 'current_password': 'wrong'}}
        self.assertEqual(self.request('/api/v1/jobs', 'POST', payload)[0], 403)
        self.operations.validate_job.assert_not_called()
        self.assertEqual(self.queue.list(), [])
        supplied = 'Basic ' + base64.b64encode(b'operator:existing-test-password').decode()
        self.auth.verify.side_effect = lambda value: 'operator' if value in {'valid', supplied} else None
        payload['params']['current_password'] = 'existing-test-password'
        status, _, result = self.request('/api/v1/jobs', 'POST', payload)
        self.assertEqual(status, 202)
        self.assertNotIn('new-private-canary', json.dumps(result))
        self.assertNotIn('existing-test-password', json.dumps(result))
        self.assertEqual(len(self.queue.list()), 1)
        with self.queue.connection() as db:
            payload = json.loads(db.execute('SELECT payload FROM jobs').fetchone()[0])
        self.assertNotIn('current_password', payload['params'])

    def test_proxy_password_rotation_reauthentication_cannot_be_skipped(self):
        self.assertEqual(self.request('/api/v1/jobs', 'POST', {
            'action': 'proxy-credentials', 'params': {'id': 'qa-proxy', 'password': 'private-canary'}})[0], 403)
        self.assertEqual(self.queue.list(), [])

    def test_submit_is_durable_idempotent_and_only_worker_reports_success(self):
        key = str(uuid.uuid4())
        payload = {'action': 'up', 'device': 'num01', 'params': {}}
        first = self.request('/api/v1/jobs', 'POST', payload, HTTP_IDEMPOTENCY_KEY=key)
        second = self.request('/api/v1/jobs', 'POST', payload, HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual((first[0], second[0]), (202, 200))
        self.assertEqual(first[2]['job']['id'], second[2]['job']['id'])
        self.assertEqual(first[2]['job']['state'], 'queued')
        self.operations.execute.assert_not_called()
        self.assertTrue(self.queue.run_one())
        self.operations.execute.assert_called_once_with(payload)
        self.assertEqual(self.queue.list()[0]['state'], 'succeeded')
        conflicting = self.request('/api/v1/jobs', 'POST', {'action': 'down'}, HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(conflicting[0], 409)

    def test_credentials_are_never_returned_and_removed_after_execution(self):
        payload = {'action': 'proxy-add', 'params': {'password': 'private-canary', 'username': 'hidden'}}
        job, _ = self.queue.submit(str(uuid.uuid4()), payload, 'operator')
        self.assertNotIn('private-canary', json.dumps(job))
        self.assertNotIn('hidden', json.dumps(self.queue.list()))
        self.queue.run_one()
        with self.queue.connection() as db:
            self.assertIsNone(db.execute('SELECT payload FROM jobs').fetchone()[0])

    def test_queue_cancel_rejects_running_jobs_and_restart_never_replays(self):
        payload = {'action': 'down', 'device': 'num01'}
        job, _ = self.queue.submit(str(uuid.uuid4()), payload, 'operator')
        self.assertEqual(self.queue.cancel(job['id'])['state'], 'cancelled')
        with self.assertRaises(QueueConflict):
            self.queue.cancel(job['id'])
        job, _ = self.queue.submit(str(uuid.uuid4()), payload, 'operator')
        with self.queue.connection() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job['id'],))
        with self.assertRaises(QueueConflict):
            self.queue.cancel(job['id'])
        reopened = JobQueue(self.directory, self.operations.execute, start=False)
        self.assertEqual(reopened.list()[0]['state'], 'interrupted')
        self.assertFalse(reopened.run_one())
        self.operations.execute.assert_not_called()
        reopened.close()

    def test_failures_do_not_expose_arbitrary_host_output(self):
        self.operations.execute.side_effect = RuntimeError('password=private-canary')
        self.queue.submit(str(uuid.uuid4()), {'action': 'up'}, 'operator')
        self.queue.run_one()
        rows = self.queue.list()
        self.assertEqual(rows[0]['state'], 'failed')
        self.assertNotIn('private-canary', json.dumps(rows))
        self.operations.snapshot.side_effect = RuntimeError('token=private-canary')
        result = self.request()
        self.assertEqual(result[0], 503)
        self.assertNotIn('private-canary', json.dumps(result))

    def test_queue_has_backpressure(self):
        self.queue.max_pending = 1
        self.queue.submit(str(uuid.uuid4()), {'action': 'up'}, 'operator')
        with self.assertRaises(QueueConflict):
            self.queue.submit(str(uuid.uuid4()), {'action': 'down'}, 'operator')

    def test_independent_connections_cannot_claim_the_same_job(self):
        other = JobQueue(self.directory, self.operations.execute, start=False)
        self.queue.submit(str(uuid.uuid4()), {'action': 'up'}, 'operator')

        class SlowClaim:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                row = self.cursor.fetchone()
                time.sleep(.05)  # Widen the SELECT/UPDATE race in the old implementation.
                return row

        class Database:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, *args):
                cursor = self.inner.execute(sql, *args)
                return SlowClaim(cursor) if sql.startswith("SELECT * FROM jobs WHERE state='queued'") else cursor

        def delayed_connection(original):
            @contextmanager
            def connect():
                with original() as db:
                    yield Database(db)
            return connect

        self.queue.connection = delayed_connection(self.queue.connection)
        other.connection = delayed_connection(other.connection)
        threads = [threading.Thread(target=queue.run_one) for queue in (self.queue, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.operations.execute.assert_called_once()
        other.close()

    def test_database_fault_degrades_health_then_recovers_without_replay(self):
        self.queue.run_one = Mock(side_effect=[sqlite3.OperationalError('private details'), False])
        # Stop on second iteration after recovery, without a wall-clock sleep.
        self.queue.closed.wait = Mock(return_value=False)
        self.queue.wake.wait = Mock(side_effect=lambda _timeout: self.queue.closed.set())
        self.queue._loop()
        self.assertEqual(self.queue.run_one.call_count, 2)
        self.assertFalse(self.queue.degraded)
        self.queue.closed.clear()
        self.queue.degraded = True
        self.assertEqual(self.request('/api/v1/health')[0], 503)
        self.assertEqual(self.request('/api/v1/jobs', 'POST', {'action': 'up'})[0], 503)
        self.operations.execute.assert_not_called()


class BasicAuthTests(unittest.TestCase):
    def test_password_is_stdin_only_cache_invalidates_on_rotation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'users'
            path.write_text('operator:$2b$test')
            path.chmod(0o600)
            runner = Mock(return_value=Mock(returncode=0))
            auth = BasicAuth(path, runner=runner)
            header = 'Basic ' + base64.b64encode(b'operator:private-canary').decode()
            self.assertEqual(auth.verify(header), 'operator')
            self.assertEqual(auth.verify(header), 'operator')
            runner.assert_called_once()
            self.assertNotIn('private-canary', ' '.join(runner.call_args.args[0]))
            self.assertEqual(runner.call_args.kwargs['input'], 'private-canary\n')
            path.write_text('operator:$2b$new-password-hash')
            runner.return_value.returncode = 1
            self.assertIsNone(auth.verify(header))
            self.assertEqual(runner.call_count, 2)
            for raw in [b'operator:bad\npassword', b'--argument:password', b'no-separator']:
                self.assertIsNone(auth.verify('Basic ' + base64.b64encode(raw).decode()))
            self.assertEqual(runner.call_count, 2)


if __name__ == '__main__':
    unittest.main()
