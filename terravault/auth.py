"""Authentication helpers for Copernicus Data Space Ecosystem services."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import requests

CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
)
CDSE_PUBLIC_CLIENT_ID = "cdse-public"


def _first_env(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return None


@dataclass
class CDSEDownloadAuthConfig:
    """Authentication options for CDSE catalogue/product downloads."""

    access_token: str | None = None
    username: str | None = None
    password: str | None = None
    client_id: str = CDSE_PUBLIC_CLIENT_ID
    totp: str | None = None
    token_url: str = CDSE_TOKEN_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CDSEDownloadAuthConfig | None:
        env = env or {}
        access_token = _first_env(env, "TERRAVAULT_CDSE_ACCESS_TOKEN", "CDSE_ACCESS_TOKEN")
        username = _first_env(env, "TERRAVAULT_CDSE_USERNAME", "CDSE_USERNAME")
        password = _first_env(env, "TERRAVAULT_CDSE_PASSWORD", "CDSE_PASSWORD")
        totp = _first_env(env, "TERRAVAULT_CDSE_TOTP", "CDSE_TOTP")
        client_id = _first_env(env, "TERRAVAULT_CDSE_CLIENT_ID", "CDSE_DOWNLOAD_CLIENT_ID")

        if not any((access_token, username, password, totp, client_id)):
            return None

        return cls(
            access_token=access_token,
            username=username,
            password=password,
            client_id=client_id or CDSE_PUBLIC_CLIENT_ID,
            totp=totp,
        )


@dataclass
class SentinelHubAuthConfig:
    """OAuth client credentials for CDSE Sentinel Hub APIs."""

    client_id: str | None = None
    client_secret: str | None = None
    token_url: str = CDSE_TOKEN_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SentinelHubAuthConfig | None:
        env = env or {}
        client_id = _first_env(
            env,
            "TERRAVAULT_CDSE_SH_CLIENT_ID",
            "CDSE_SH_CLIENT_ID",
            "CDSE_CLIENT_ID",
        )
        client_secret = _first_env(
            env,
            "TERRAVAULT_CDSE_SH_CLIENT_SECRET",
            "CDSE_SH_CLIENT_SECRET",
            "CDSE_CLIENT_SECRET",
        )

        if not any((client_id, client_secret)):
            return None

        return cls(client_id=client_id, client_secret=client_secret)


class CDSEAccessTokenProvider:
    """Cache and refresh CDSE access tokens on demand."""

    def __init__(
        self,
        config: CDSEDownloadAuthConfig | SentinelHubAuthConfig,
        session_factory: Any | None = None,
    ) -> None:
        self.config = config
        self._session_factory = session_factory or requests.Session
        self._cached_token: str | None = None
        self._expires_at: float = 0.0

        static_token = getattr(config, "access_token", None)
        if static_token:
            self._cached_token = static_token
            self._expires_at = float("inf")

    def get_token(self) -> str:
        if self._cached_token and time.time() < self._expires_at:
            return self._cached_token

        session = self._session_factory()
        try:
            payload = self._token_payload()
            response = session.post(
                self.config.token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

        token = data.get("access_token")
        if not token:
            raise ValueError("CDSE token response did not include access_token")

        expires_in = int(data.get("expires_in", 3600))
        self._cached_token = token
        # Refresh at least a second before expiry. Short-lived test tokens must
        # never be cached beyond the lifetime reported by the provider.
        self._expires_at = time.time() + max(expires_in - 60, 1)
        return token

    def _token_payload(self) -> dict[str, str]:
        if isinstance(self.config, CDSEDownloadAuthConfig):
            if self.config.access_token:
                return {}
            if not self.config.username or not self.config.password:
                raise ValueError(
                    "CDSE download auth requires TERRAVAULT_CDSE_ACCESS_TOKEN or "
                    "TERRAVAULT_CDSE_USERNAME/TERRAVAULT_CDSE_PASSWORD."
                )
            payload = {
                "client_id": self.config.client_id or CDSE_PUBLIC_CLIENT_ID,
                "grant_type": "password",
                "username": self.config.username,
                "password": self.config.password,
            }
            if self.config.totp:
                payload["totp"] = self.config.totp
            return payload

        if not self.config.client_id or not self.config.client_secret:
            raise ValueError(
                "Sentinel Hub auth requires TERRAVAULT_CDSE_SH_CLIENT_ID and "
                "TERRAVAULT_CDSE_SH_CLIENT_SECRET."
            )
        return {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
        }


def build_cdse_session_factory(
    config: CDSEDownloadAuthConfig,
    session_factory: Any | None = None,
) -> Any:
    """Return a session factory that injects a CDSE bearer token."""

    provider = CDSEAccessTokenProvider(config, session_factory=session_factory)

    def _factory() -> requests.Session:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {provider.get_token()}"
        return session

    return _factory
