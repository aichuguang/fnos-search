from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.accounts.crypto import (
    PURPOSE_APP_SESSION,
    PURPOSE_PROVIDER_ACCOUNT,
    derive_key,
)
from fnos_media_import.app import create_app


ROOT_KEY = bytes.fromhex("ab" * 32)


class AppSessionKeyCutoverTests(unittest.TestCase):
    def test_app_session_key_is_stable_and_purpose_isolated(self) -> None:
        session_key = derive_key(ROOT_KEY, PURPOSE_APP_SESSION)

        self.assertEqual(session_key, derive_key(ROOT_KEY, PURPOSE_APP_SESSION))
        self.assertEqual(len(session_key), 32)
        self.assertNotEqual(
            session_key,
            derive_key(ROOT_KEY, PURPOSE_PROVIDER_ACCOUNT),
        )

    def test_missing_root_key_fails_before_runtime_composition(self) -> None:
        with (
            patch("fnos_media_import.app.load_config", return_value=object()),
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "FNOS_ROOT_KEY"),
        ):
            create_app(process_role="web")

    def test_app_secret_compatibility_sources_are_removed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "fnos_media_import" / "app.py").read_text(
            encoding="utf-8"
        )
        config_source = (root / "fnos_media_import" / "config.py").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "app_session_key = derive_key(root_key, PURPOSE_APP_SESSION).hex()",
            app_source,
        )
        self.assertNotIn("_resolve_secret_key", app_source)
        self.assertNotIn('db.set_app_settings({"app.secret_key"', app_source)
        self.assertNotIn('("app", "secret_key")', config_source)


if __name__ == "__main__":
    unittest.main()
