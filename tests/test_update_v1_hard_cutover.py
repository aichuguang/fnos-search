from __future__ import annotations

import unittest
from types import SimpleNamespace

from fnos_media_import.services.update_service import (
    UpdateService,
    _path_within_openlist_root,
)


class _EmptyOpenList:
    configured = True

    def list_dir(self, _path: str, *, refresh: bool | None = None) -> list:
        del refresh
        return []

    def scan_videos(self, _root: str, **_kwargs) -> list:
        return []


def _service(resolver=None) -> UpdateService:
    service = object.__new__(UpdateService)
    service.db = SimpleNamespace()
    service.config = {}
    service.categories = {
        "tv": {
            "label": "legacy-label",
            "cloud139_target_path": "/legacy/cloud139/tv",
            "mobile_target_path": "/legacy/openlist/tv",
        }
    }
    service.openlist_session_resolver = resolver
    return service


class UpdateV1HardCutoverTests(unittest.TestCase):
    def test_missing_db_resolver_fails_closed_and_ignores_legacy_category_paths(self) -> None:
        service = _service()
        context = service._resolve_update_root_context(
            {
                "category": "tv",
                "title": "Example Show",
                "year": "2024",
                "raw_data": {"openlist_path": "/legacy/openlist/tv/Example Show"},
            }
        )

        self.assertEqual(context["category_root"], "")
        self.assertEqual(context["canonical_openlist_root"], "")
        self.assertTrue(context["needs_manual_selection"])

    def test_db_root_replaces_out_of_scope_persisted_subscription_path(self) -> None:
        client = _EmptyOpenList()
        service = _service(
            lambda _subscription, _category: {
                "client": client,
                "category_root": "/OpenList/Main/TV",
                "destination_id": "media_main",
                "mount_id": "mount_main",
                "namespace_id": "openlist:main:mount_main",
            }
        )
        context = service._resolve_update_root_context(
            {
                "category": "tv",
                "title": "Example Show",
                "year": "2024",
                "raw_data": {
                    "destination_id": "media_main",
                    "canonical_openlist_root": "/legacy/openlist/tv/Example Show",
                },
            }
        )

        self.assertEqual(context["category_root"], "/OpenList/Main/TV")
        self.assertTrue(
            context["canonical_openlist_root"].startswith("/OpenList/Main/TV/")
        )
        self.assertNotIn("legacy", context["canonical_openlist_root"].lower())
        self.assertEqual(context["destination_id"], "media_main")
        self.assertEqual(context["mount_id"], "mount_main")

    def test_candidate_paths_are_audit_only_and_never_enter_import_payload(self) -> None:
        service = _service()
        candidate = service._decorate_update_candidate(
            {"id": 7, "category": "tv", "title": "Example Show", "year": "2024"},
            {
                "title": "Example.Show.S01E02",
                "source_type": "mobile",
                "episode": 2,
                "import_payload": {
                    "destination_id": "attacker-target",
                    "target_path": "/attacker",
                    "target_path_override": "/attacker",
                    "cloud139_target_path_override": "/attacker",
                    "update_context": {"target_root_path": "/attacker"},
                    "organizer_context": {"target_root_path": "/attacker"},
                    "mount_id": "attacker-mount",
                    "remote": "attacker:",
                },
            },
            {
                "canonical_openlist_root": "/OpenList/Main/TV/Example Show (2024)",
                "canonical_year": "2024",
            },
            {(1, 2)},
        )

        self.assertIn("update_context", candidate)
        payload = candidate["import_payload"]
        for field in (
            "destination_id",
            "target_path",
            "target_path_override",
            "cloud139_target_path_override",
            "update_context",
            "organizer_context",
            "mount_id",
            "remote",
        ):
            self.assertNotIn(field, payload)

    def test_runtime_refresh_preserves_db_session_resolver(self) -> None:
        resolver = lambda _subscription, _category: None
        service = _service(resolver)
        service._build_discovery = lambda *_args: object()

        service.set_runtime(
            config={},
            categories={},
            search_service=object(),
            import_service=object(),
            quark_importer=object(),
            cloud139_importer=object(),
        )

        self.assertIs(service.openlist_session_resolver, resolver)

    def test_subscription_path_must_stay_within_db_category_root(self) -> None:
        self.assertTrue(
            _path_within_openlist_root(
                "/OpenList/Main/TV",
                "/OpenList/Main/TV/Example Show",
            )
        )
        self.assertFalse(
            _path_within_openlist_root(
                "/OpenList/Main/TV",
                "/OpenList/Main/Movies/Example Show",
            )
        )
        self.assertFalse(_path_within_openlist_root("", "/OpenList/Main/TV"))


if __name__ == "__main__":
    unittest.main()
