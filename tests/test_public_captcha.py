from __future__ import annotations

import time
import unittest

from flask import Flask, session

from fnos_media_import.public_web import (
    _captcha_hash,
    _public_security_config,
    _verify_public_captcha,
)


class PublicCaptchaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)
        self.app.secret_key = "captcha-test-secret"

    def test_enabled_captcha_always_uses_simple_provider(self) -> None:
        result = _public_security_config(
            {
                "captcha_enabled": True,
                "captcha_provider": "turnstile",
                "captcha_site_key": "unused-site-key",
            }
        )

        self.assertEqual(result["captcha"], {"enabled": True, "provider": "simple"})

    def test_simple_captcha_is_single_use(self) -> None:
        answer = "12"
        with self.app.test_request_context("/"):
            session["public_captcha_hash"] = _captcha_hash(answer, self.app.secret_key)
            session["public_captcha_expires_at"] = int(time.time()) + 60

            ok, message = _verify_public_captcha(
                {"captcha_answer": answer},
                {"captcha_enabled": True},
                "127.0.0.1",
                self.app.secret_key,
            )
            self.assertTrue(ok)
            self.assertEqual(message, "")

            ok, message = _verify_public_captcha(
                {"captcha_answer": answer},
                {"captcha_enabled": True},
                "127.0.0.1",
                self.app.secret_key,
            )
            self.assertFalse(ok)
            self.assertEqual(message, "请先完成验证码")


if __name__ == "__main__":
    unittest.main()
