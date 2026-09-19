"""Credential diagnostics expose fixed categories, never HTTP error content."""
import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from services.api.credentials import CredentialsError, _http


class CredentialHttpErrorsTests(unittest.TestCase):
    def test_http_auth_policy_and_server_failures_have_distinct_safe_codes(self):
        for status, expected in ((401, 'grafana-auth'), (403, 'grafana-forbidden'),
                                 (302, 'grafana-http'), (500, 'grafana-http')):
            with self.subTest(status=status):
                body = io.BytesIO(b'private-server-response')
                opener = Mock()
                opener.open.side_effect = HTTPError('http://172.20.0.2:3000/api/user',
                                                    status, 'private-error-text', {}, body)
                with patch('services.api.credentials.urllib.request.build_opener', return_value=opener):
                    with self.assertRaises(CredentialsError) as failure:
                        _http('GET', 'http://172.20.0.2:3000/api/user', 'admin', 'fixture-password')
                self.assertEqual(failure.exception.code, expected)
                self.assertNotIn('private-', str(failure.exception))
                self.assertNotIn('fixture-password', str(failure.exception))
                self.assertTrue(body.closed)

    def test_network_failure_does_not_expose_url_or_exception_detail(self):
        opener = Mock()
        opener.open.side_effect = URLError('private-network-detail')
        with patch('services.api.credentials.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(CredentialsError) as failure:
                _http('GET', 'http://172.20.0.2:3000/api/user', 'admin', 'fixture-password')
        self.assertEqual(failure.exception.code, 'grafana-unreachable')
        self.assertNotIn('private-network-detail', str(failure.exception))


if __name__ == '__main__':
    unittest.main()
