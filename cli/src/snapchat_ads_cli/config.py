"""Multi-account configuration and local state.

Reads account config from ~/.config/snapchat-ads-cli/accounts.toml and
resolves per-account state files (token, cache, history).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "snapchat-ads-cli"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.toml"
AUDIT_FILE = CONFIG_DIR / "audit.jsonl"

DEFAULT_API_VERSION = "v1"
BASE_URL = "https://adsapi.snapchat.com/v1"
OAUTH_BASE = "https://accounts.snapchat.com/login/oauth2"
DEFAULT_SCOPE = "snapchat-marketing-api"


def load_env_files() -> None:
    """Load Snap env vars from known .env files without overriding shell.

    The CLI is often launched through `uv run`, not a long-running shell that
    has sourced a .env file. This keeps explicit process env highest priority
    while making the documented config-dir `.env` usable. Set
    `SNAPCHAT_ADS_ENV_FILE` to add an extra file to the search list.
    """
    candidates = [
        Path.home() / ".config" / "snapchat-ads-cli" / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    extra = os.environ.get("SNAPCHAT_ADS_ENV_FILE")
    if extra:
        candidates.insert(0, Path(os.path.expanduser(extra)))
    for env_path in candidates:
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("\"'")
                if key.startswith("SNAPCHAT_") and value and not os.environ.get(key):
                    os.environ[key] = value
        except OSError as e:
            logger.warning("Failed to load env file %s: %s", env_path, e)


@dataclass
class AccountConfig:
    """Configuration for a single Snapchat ad account."""

    name: str
    organization_id: str = ""
    ad_account_id: str = ""
    pixel_id: str = ""
    token_source: str = ""
    cache_dir: str = ""
    history_dir: str = ""

    def resolve_path(self, path_str: str) -> Path | None:
        if not path_str:
            return None
        return Path(os.path.expanduser(path_str))

    def load_token(self) -> str | None:
        """Load access token from env, token file, or profile .env."""
        env_token = os.environ.get("SNAPCHAT_ACCESS_TOKEN")
        if env_token:
            return env_token

        token_path = self.resolve_path(self.token_source)
        if token_path and token_path.exists():
            try:
                data = json.loads(token_path.read_text())
                token = data.get("access_token")
                if token:
                    logger.debug("Loaded access token from %s", token_path)
                    return token
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read token from %s: %s", token_path, e)

        profile_env = self._find_profile_env()
        if profile_env:
            token = self._read_env_var(profile_env, "SNAPCHAT_ACCESS_TOKEN")
            if token:
                return token

        return None

    def load_refresh_token(self) -> str | None:
        """Load refresh token from token file or env."""
        env_token = os.environ.get("SNAPCHAT_REFRESH_TOKEN")
        if env_token:
            return env_token

        token_path = self.resolve_path(self.token_source)
        if token_path and token_path.exists():
            try:
                data = json.loads(token_path.read_text())
                return data.get("refresh_token")
            except (json.JSONDecodeError, OSError):
                pass

        profile_env = self._find_profile_env()
        if profile_env:
            return self._read_env_var(profile_env, "SNAPCHAT_REFRESH_TOKEN")

        return None

    def load_token_metadata(self) -> dict[str, Any]:
        """Load full token metadata (expiry, scope, etc.) from token file."""
        token_path = self.resolve_path(self.token_source)
        if token_path and token_path.exists():
            try:
                return json.loads(token_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save_token(self, payload: dict[str, Any]) -> Path:
        """Write token JSON to disk with mode 0600."""
        token_path = self.resolve_path(self.token_source)
        if not token_path:
            raise ValueError(
                "token_source not set in accounts.toml -- cannot persist token"
            )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps(payload, indent=2))
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
        return token_path

    def _find_profile_env(self) -> Path | None:
        """Find a `.env` file next to the token file, if any."""
        token_path = self.resolve_path(self.token_source)
        if not token_path:
            return None
        env_path = token_path.parent / ".env"
        if env_path.exists():
            return env_path
        return None

    @staticmethod
    def _read_env_var(env_path: Path, key: str) -> str | None:
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip("\"'")
        except OSError:
            pass
        return None


@dataclass
class AppConfig:
    """Top-level application config."""

    accounts: dict[str, AccountConfig] = field(default_factory=dict)
    api_version: str = DEFAULT_API_VERSION
    client_id_env: str = "SNAPCHAT_CLIENT_ID"
    client_secret_env: str = "SNAPCHAT_CLIENT_SECRET"
    redirect_uri_env: str = "SNAPCHAT_REDIRECT_URI"
    default_scope: str = DEFAULT_SCOPE

    @property
    def client_id(self) -> str | None:
        return os.environ.get(self.client_id_env)

    @property
    def client_secret(self) -> str | None:
        return os.environ.get(self.client_secret_env)

    @property
    def redirect_uri(self) -> str | None:
        return os.environ.get(self.redirect_uri_env)


def load_config() -> AppConfig:
    """Load full application config from accounts.toml."""
    load_env_files()
    config = AppConfig()

    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "rb") as f:
            data = tomllib.load(f)

        shared = data.get("shared", {})
        config.api_version = shared.get("api_version", DEFAULT_API_VERSION)
        config.client_id_env = shared.get("client_id_env", "SNAPCHAT_CLIENT_ID")
        config.client_secret_env = shared.get(
            "client_secret_env", "SNAPCHAT_CLIENT_SECRET"
        )
        config.redirect_uri_env = shared.get(
            "redirect_uri_env", "SNAPCHAT_REDIRECT_URI"
        )
        config.default_scope = shared.get("default_scope", DEFAULT_SCOPE)

        for key, acct_data in data.get("accounts", {}).items():
            config.accounts[key] = AccountConfig(
                name=acct_data.get("name", key),
                organization_id=acct_data.get("organization_id", ""),
                ad_account_id=acct_data.get("ad_account_id", ""),
                pixel_id=acct_data.get("pixel_id", ""),
                token_source=acct_data.get("token_source", ""),
                cache_dir=acct_data.get("cache_dir", ""),
                history_dir=acct_data.get("history_dir", ""),
            )
    else:
        logger.info(
            "No accounts.toml at %s -- run `snapchat-ads auth login` to scaffold one",
            ACCOUNTS_FILE,
        )
        ad_account_id = os.environ.get("SNAPCHAT_AD_ACCOUNT_ID", "")
        if ad_account_id or os.environ.get("SNAPCHAT_ORGANIZATION_ID"):
            config.accounts["default"] = AccountConfig(
                name="Default (env vars)",
                organization_id=os.environ.get("SNAPCHAT_ORGANIZATION_ID", ""),
                ad_account_id=ad_account_id,
                pixel_id=os.environ.get("SNAPCHAT_PIXEL_ID", ""),
            )

    return config


def get_account(config: AppConfig, account_key: str) -> AccountConfig:
    """Get account config by key, with validation."""
    if account_key not in config.accounts:
        available = list(config.accounts.keys())
        raise ValueError(
            f"Account '{account_key}' not found. Available: {available}. "
            f"Edit {ACCOUNTS_FILE} or run `snapchat-ads auth login`."
        )
    return config.accounts[account_key]


def init_default_config() -> Path:
    """Create default accounts.toml if it does not exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not ACCOUNTS_FILE.exists():
        default = {
            "accounts": {
                "default": {
                    "name": "My Brand",
                    "organization_id": "",
                    "ad_account_id": "",
                    "pixel_id": "",
                    "token_source": "~/.config/snapchat-ads-cli/.token.json",
                    "cache_dir": "~/.config/snapchat-ads-cli/cache/",
                    "history_dir": "~/.config/snapchat-ads-cli/history/",
                },
            },
            "shared": {
                "api_version": DEFAULT_API_VERSION,
                "client_id_env": "SNAPCHAT_CLIENT_ID",
                "client_secret_env": "SNAPCHAT_CLIENT_SECRET",
                "redirect_uri_env": "SNAPCHAT_REDIRECT_URI",
                "default_scope": DEFAULT_SCOPE,
            },
        }
        with open(ACCOUNTS_FILE, "wb") as f:
            tomli_w.dump(default, f)
        logger.info("Created default accounts.toml at %s", ACCOUNTS_FILE)
    return ACCOUNTS_FILE
