from __future__ import annotations

from typing import Any

from .app_settings_repository import AppSettingsRepository


class AdminProfileRepository:
    """Stores administrator identity and site branding as separate settings."""

    PROFILE_KEY = "admin.profile"
    BRANDING_KEY = "site.branding"

    def __init__(self, settings: AppSettingsRepository) -> None:
        self._settings = settings

    def get_profile(self) -> dict[str, Any]:
        value = self._settings.get_all().get(self.PROFILE_KEY)
        return dict(value) if isinstance(value, dict) else {}

    def save_profile(self, profile: dict[str, Any]) -> None:
        self._settings.set_many({self.PROFILE_KEY: dict(profile)})

    def get_branding(self) -> dict[str, Any]:
        value = self._settings.get_all().get(self.BRANDING_KEY)
        return dict(value) if isinstance(value, dict) else {}

    def save_branding(self, branding: dict[str, Any]) -> None:
        self._settings.set_many({self.BRANDING_KEY: dict(branding)})
