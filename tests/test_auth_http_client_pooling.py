import unittest

import app.core.dependencies as dependencies


class AuthHttpClientPoolingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate the module-level singleton between tests.
        dependencies._auth_http_client = None

    def tearDown(self) -> None:
        dependencies._auth_http_client = None

    def test_get_auth_http_client_returns_same_instance_across_calls(self) -> None:
        # _verify_token runs on every admin request; the underlying httpx.Client
        # must be created once and reused so requests share a connection pool
        # instead of paying a fresh TCP+TLS handshake each time.
        first = dependencies._get_auth_http_client()
        second = dependencies._get_auth_http_client()
        self.assertIs(first, second)

    def test_get_auth_http_client_has_configured_timeout(self) -> None:
        client = dependencies._get_auth_http_client()
        self.assertEqual(client.timeout.connect, 10)


if __name__ == "__main__":
    unittest.main()
