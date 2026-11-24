"""Typed runtime configuration and Google Cloud Secret Manager credential provider.

Eliminates hardcoded API keys or Workspace tokens by resolving secrets at runtime
from Google Cloud Secret Manager (`google-cloud-secret-manager`), with controlled
local `.env` fallback strictly for offline development and automated CI testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional in minimal test containers
    pass

try:
    from google.cloud import secretmanager  # type: ignore
except ImportError:  # pragma: no cover - graceful fallback when SDK not installed
    secretmanager = None

logger = logging.getLogger(__name__)


class SecretManagerProvider:
    """Fetches sensitive runtime credentials from Google Cloud Secret Manager."""

    def __init__(self, project_id: Optional[str] = None, enabled: bool = False) -> None:
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "")
        self.enabled = enabled and bool(self.project_id)
        self._cache: dict[str, str] = {}

    def get_secret(
        self,
        secret_id: str,
        env_fallback_key: Optional[str] = None,
        version: str = "latest",
    ) -> str:
        """Resolves a secret payload from GCP Secret Manager without logging secret values.

        Args:
            secret_id: Name of the secret in Google Cloud Secret Manager.
            env_fallback_key: Environment variable consulted when Secret Manager is disabled.
            version: Secret version identifier (defaults to 'latest').

        Returns:
            Resolved secret string (or empty string if unconfigured in demo mode).
        """
        if secret_id in self._cache:
            return self._cache[secret_id]

        if self.enabled and secretmanager is not None:
            try:
                client = secretmanager.SecretManagerServiceClient()
                resource_name = (
                    f"projects/{self.project_id}/secrets/{secret_id}/versions/{version}"
                )
                response = client.access_secret_version(request={"name": resource_name})
                payload = response.payload.data.decode("utf-8").strip()
                self._cache[secret_id] = payload
                return payload
            except Exception as exc:
                logger.warning(
                    "Secret Manager lookup failed for secret_id=%s; checking env fallback (%s)",
                    secret_id,
                    type(exc).__name__,
                )

        if env_fallback_key:
            val = os.getenv(env_fallback_key, "").strip()
            self._cache[secret_id] = val
            return val
        return ""


@dataclass(frozen=True)
class AgentSettings:
    """Immutable configuration governing the Meeting Notes & Recording Publisher Agent."""

    gcp_project_id: str = field(
        default_factory=lambda: os.getenv("GCP_PROJECT_ID", "demo-enterprise-project")
    )
    gcp_location: str = field(
        default_factory=lambda: os.getenv("GCP_LOCATION", "us-central1")
    )
    use_secret_manager: bool = field(
        default_factory=lambda: os.getenv("USE_SECRET_MANAGER", "false").lower() == "true"
    )
    gemini_api_key_secret_id: str = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY_SECRET_ID", "gemini-api-key")
    )
    workspace_oauth_secret_id: str = field(
        default_factory=lambda: os.getenv(
            "WORKSPACE_OAUTH_SECRET_ID", "workspace-oauth-token-json"
        )
    )
    host_organizer_email: str = field(
        default_factory=lambda: os.getenv("HOST_ORGANIZER_EMAIL", "organizer@example.com")
    )
    required_attendee_group: str = field(
        default_factory=lambda: os.getenv(
            "REQUIRED_ATTENDEE_GROUP", "account-leads@example.com"
        )
    )
    target_chat_space_id: str = field(
        default_factory=lambda: os.getenv(
            "TARGET_CHAT_SPACE_ID", "spaces/AAAA_OFFICIAL_SPACE"
        )
    )
    target_chat_space_name: str = field(
        default_factory=lambda: os.getenv(
            "TARGET_CHAT_SPACE_NAME", "Account Leads Official Space"
        )
    )
    default_timezone: str = field(
        default_factory=lambda: os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
    )
    fast_model_name: str = field(
        default_factory=lambda: os.getenv("FAST_MODEL_NAME", "gemini-2.5-flash")
    )
    reasoning_model_name: str = field(
        default_factory=lambda: os.getenv("REASONING_MODEL_NAME", "gemini-2.5-pro")
    )
    max_context_tokens: int = field(
        default_factory=lambda: int(os.getenv("MAX_CONTEXT_TOKENS", "6000"))
    )
    preserve_default_drive_permissions: bool = field(
        default_factory=lambda: os.getenv(
            "PRESERVE_DEFAULT_DRIVE_PERMISSIONS", "true"
        ).lower()
        == "true"
    )
    require_hitl_approval: bool = field(
        default_factory=lambda: os.getenv("REQUIRE_HITL_APPROVAL", "true").lower()
        == "true"
    )
    enable_cloud_dlp_redaction: bool = field(
        default_factory=lambda: os.getenv("ENABLE_CLOUD_DLP_REDACTION", "false").lower()
        == "true"
    )
    data_mode: str = field(default_factory=lambda: os.getenv("DATA_MODE", "demo"))
    sqlite_db_path: str = field(
        default_factory=lambda: os.getenv(
            "SQLITE_DB_PATH", ".agent_data/publisher_state.sqlite3"
        )
    )

    def resolve_gemini_api_key(self) -> str:
        """Fetches the Gemini API key securely via SecretManagerProvider."""
        provider = SecretManagerProvider(
            project_id=self.gcp_project_id, enabled=self.use_secret_manager
        )
        return provider.get_secret(
            secret_id=self.gemini_api_key_secret_id,
            env_fallback_key="GEMINI_API_KEY",
        )

    def resolve_workspace_credentials_json(self) -> str:
        """Fetches Google Workspace OAuth/Service-Account JSON via SecretManagerProvider."""
        provider = SecretManagerProvider(
            project_id=self.gcp_project_id, enabled=self.use_secret_manager
        )
        return provider.get_secret(
            secret_id=self.workspace_oauth_secret_id,
            env_fallback_key="WORKSPACE_OAUTH_JSON",
        )


def load_settings() -> AgentSettings:
    """Returns a fresh AgentSettings snapshot."""
    return AgentSettings()
