from __future__ import annotations

from typing import Any, Callable

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

    def update_profile_atomic(self, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        def _update(value: Any, _existed: bool) -> dict[str, Any]:
            return updater(dict(value) if isinstance(value, dict) else {})

        _, _, updated = self._settings.update_atomic(self.PROFILE_KEY, _update)
        return dict(updated)

    def get_branding(self) -> dict[str, Any]:
        value = self._settings.get_all().get(self.BRANDING_KEY)
        return dict(value) if isinstance(value, dict) else {}

    def save_branding(self, branding: dict[str, Any]) -> None:
        self._settings.set_many({self.BRANDING_KEY: dict(branding)})
