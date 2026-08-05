from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fnos_media_import.app import _install_file_log_handler


class FileLoggingFallbackTests(unittest.TestCase):
    def test_log_file_pointing_to_directory_does_not_break_startup_logging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = logging.Logger("directory-log-fallback")
            fallback = logging.NullHandler()
            logger.addHandler(fallback)

            with patch.dict(os.environ, {"LOG_FILE": temp_dir}):
                _install_file_log_handler(logger, Path(temp_dir))

            self.assertEqual(logger.handlers, [fallback])

    def test_invalid_log_path_value_error_is_reported_without_breaking_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = logging.Logger("invalid-log-fallback")
            records: list[logging.LogRecord] = []

            class CaptureHandler(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    records.append(record)

            fallback = CaptureHandler()
            logger.addHandler(fallback)
            with (
                patch.dict(os.environ, {"LOG_FILE": str(Path(temp_dir) / "invalid.log")}),
                patch("logging.handlers.RotatingFileHandler", side_effect=ValueError("invalid path")),
            ):
                _install_file_log_handler(logger, Path(temp_dir))

            self.assertEqual(logger.handlers, [fallback])
            self.assertTrue(any("持久日志不可用" in record.getMessage() for record in records))

    def test_read_only_file_open_error_keeps_existing_fallback_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "read-only.log"
            log_path.write_text("existing", encoding="utf-8")
            log_path.chmod(0o444)
            logger = logging.Logger("read-only-log-fallback")
            fallback = logging.NullHandler()
            logger.addHandler(fallback)

            try:
                with (
                    patch.dict(os.environ, {"LOG_FILE": str(log_path)}),
                    patch(
                        "logging.handlers.RotatingFileHandler",
                        side_effect=PermissionError("read-only log file"),
                    ),
                ):
                    _install_file_log_handler(logger, Path(temp_dir))
            finally:
                log_path.chmod(0o666)

            self.assertEqual(logger.handlers, [fallback])

    def test_handler_registration_oserror_is_contained_and_handler_is_closed(self) -> None:
        class RejectingLogger(logging.Logger):
            def addHandler(self, handler: logging.Handler) -> None:  # noqa: N802
                if getattr(handler, "baseFilename", ""):
                    raise OSError("handler registration failed")
                super().addHandler(handler)

        with tempfile.TemporaryDirectory() as temp_dir:
            logger = RejectingLogger("registration-log-fallback")
            fallback = logging.NullHandler()
            logger.addHandler(fallback)
            log_path = Path(temp_dir) / "app.log"

            with patch.dict(os.environ, {"LOG_FILE": str(log_path)}):
                _install_file_log_handler(logger, Path(temp_dir))

            self.assertEqual(logger.handlers, [fallback])

    def test_reinstall_replaces_same_path_handler_instead_of_duplicating_it(self) -> None:
        from logging.handlers import RotatingFileHandler

        with tempfile.TemporaryDirectory() as temp_dir:
            logger = logging.Logger("idempotent-file-handler")
            fallback = logging.NullHandler()
            logger.addHandler(fallback)
            log_path = Path(temp_dir) / "app.log"

            with patch.dict(os.environ, {"LOG_FILE": str(log_path)}):
                _install_file_log_handler(logger, Path(temp_dir))
                _install_file_log_handler(logger, Path(temp_dir))

            file_handlers = [item for item in logger.handlers if isinstance(item, RotatingFileHandler)]
            self.assertEqual(len(file_handlers), 1)
            for handler in file_handlers:
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
