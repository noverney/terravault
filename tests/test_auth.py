"""Tests for terravault.auth."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from terravault.auth import CDSEAccessTokenProvider, CDSEDownloadAuthConfig, SentinelHubAuthConfig


class TestCDSEDownloadAuthConfig(unittest.TestCase):
    def test_from_env_prefers_terravault_prefix(self):
        cfg = CDSEDownloadAuthConfig.from_env(
            {
                "CDSE_USERNAME": "fallback-user",
                "CDSE_PASSWORD": "fallback-pass",
                "TERRAVAULT_CDSE_USERNAME": "preferred-user",
                "TERRAVAULT_CDSE_PASSWORD": "preferred-pass",
            }
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.username, "preferred-user")
        self.assertEqual(cfg.password, "preferred-pass")


class TestCDSEAccessTokenProvider(unittest.TestCase):
    def test_static_download_token_is_reused(self):
        provider = CDSEAccessTokenProvider(CDSEDownloadAuthConfig(access_token="abc"))
        self.assertEqual(provider.get_token(), "abc")
        self.assertEqual(provider.get_token(), "abc")

    def test_password_grant_is_cached(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "fresh-token", "expires_in": 3600}
        mock_response.raise_for_status.return_value = None
        mock_session.post.return_value = mock_response
        provider = CDSEAccessTokenProvider(
            CDSEDownloadAuthConfig(username="user", password="pass"),
            session_factory=lambda: mock_session,
        )

        self.assertEqual(provider.get_token(), "fresh-token")
        self.assertEqual(provider.get_token(), "fresh-token")
        self.assertEqual(mock_session.post.call_count, 1)

    def test_client_credentials_flow_is_supported(self):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "sh-token", "expires_in": 3600}
        mock_response.raise_for_status.return_value = None
        mock_session.post.return_value = mock_response
        provider = CDSEAccessTokenProvider(
            SentinelHubAuthConfig(client_id="cid", client_secret="secret"),
            session_factory=lambda: mock_session,
        )

        self.assertEqual(provider.get_token(), "sh-token")
        payload = mock_session.post.call_args.kwargs["data"]
        self.assertEqual(payload["grant_type"], "client_credentials")
        self.assertEqual(payload["client_id"], "cid")


if __name__ == "__main__":
    unittest.main()
