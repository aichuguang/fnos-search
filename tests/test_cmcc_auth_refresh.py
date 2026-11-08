from __future__ import annotations

import base64
import time
import unittest
from unittest.mock import patch

from fnos_media_import.cmcc.auth import CmccAuthExpired, CmccAuthProvider


def _authorization(phone: str, token: str, expires_ms: int) -> str:
    payload = f"pc:{phone}:{token}|a|b|{expires_ms}"
    return "Basic " + base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _decode(authorization: str) -> str:
    token = authorization.split(" ", 1)[1]
    return base64.b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")


class _Response:
    status_code = 200

    def __init__(self, body: str) -> None:
        self.content = body.encode("utf-8")


class CmccAuthRefreshTests(unittest.TestCase):
    def test_refreshes_when_token_is_within_refresh_window_and_persists(self) -> None:
        phone = "13800138000"
        old = _authorization(phone, "old-token", int(time.time() * 1000) + 10 * 86400 * 1000)
        new_expiry = int(time.time() * 1000) + 30 * 86400 * 1000
        patches: list[dict[str, str]] = []
        provider = CmccAuthProvider(
            {"authorization": old, "phone": phone},
            authorization_update_callback=lambda patch: patches.append(patch) or True,
        )

        with patch(
            "fnos_media_import.cmcc.auth.requests.post",
            return_value=_Response(
                f"<root><return>0</return><token>new-token|a|b|{new_expiry}</token></root>"
            ),
        ) as post:
            credentials = provider.get_credentials()

        self.assertEqual(len(post.call_args_list), 1)
        self.assertIn("<account>13800138000</account>", post.call_args.kwargs["data"].decode())
        self.assertIn("old-token|a|b|", post.call_args.kwargs["data"].decode())
        self.assertIn("new-token", _decode(credentials.authorization))
        self.assertEqual(patches, [{"authorization": credentials.authorization}])

    def test_does_not_refresh_when_token_is_outside_refresh_window(self) -> None:
        phone = "13800138000"
        authorization = _authorization(
            phone, "still-valid", int(time.time() * 1000) + 20 * 86400 * 1000
        )
        provider = CmccAuthProvider({"authorization": authorization, "phone": phone})

        with patch("fnos_media_import.cmcc.auth.requests.post") as post:
            credentials = provider.get_credentials()

        post.assert_not_called()
        self.assertEqual(credentials.authorization, authorization)

    def test_forced_reload_refreshes_even_when_token_is_far_from_expiry(self) -> None:
        phone = "13800138000"
        authorization = _authorization(
            phone, "still-valid", int(time.time() * 1000) + 20 * 86400 * 1000
        )
        patches: list[dict[str, str]] = []
        provider = CmccAuthProvider(
            {"access_token": authorization, "phone": phone},
            authorization_update_callback=lambda patch: patches.append(patch) or True,
        )

        with patch(
            "fnos_media_import.cmcc.auth.requests.post",
            return_value=_Response("<root><return>0</return><token>rotated|a|b|1</token></root>"),
        ) as post:
            credentials = provider.reload()

        self.assertEqual(len(post.call_args_list), 1)
        self.assertIn("rotated", _decode(credentials.authorization))
        self.assertEqual(patches[0]["access_token"], credentials.authorization)
        self.assertEqual(patches[0]["authorization"], credentials.authorization)

    def test_refresh_failure_raises_auth_expired(self) -> None:
        phone = "13800138000"
        authorization = _authorization(
            phone, "expired", int(time.time() * 1000) + 10 * 86400 * 1000
        )
        provider = CmccAuthProvider({"authorization": authorization, "phone": phone})

        with patch(
            "fnos_media_import.cmcc.auth.requests.post",
            return_value=_Response("<root><return>1</return><desc>token invalid</desc></root>"),
        ):
            with self.assertRaisesRegex(CmccAuthExpired, "token invalid"):
                provider.get_credentials()

    def test_legacy_unparseable_authorization_is_not_refreshed(self) -> None:
        provider = CmccAuthProvider({"authorization": "Basic test-credential", "phone": "13800138000"})

        with patch("fnos_media_import.cmcc.auth.requests.post") as post:
            credentials = provider.get_credentials()

        post.assert_not_called()
        self.assertEqual(credentials.authorization, "Basic test-credential")


if __name__ == "__main__":
    unittest.main()
