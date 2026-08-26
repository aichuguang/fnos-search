from __future__ import annotations

import logging
import os
from pathlib import Path


def install_file_log_handler(root_logger: logging.Logger, base_dir: Path) -> None:
    """Install optional rotating UTF-8 file logging without blocking startup."""

    from logging.handlers import RotatingFileHandler

    log_file = os.getenv("LOG_FILE", "") or str(base_dir / "logs" / "app.log")
    log_path = Path(log_file)
    file_handler: logging.Handler | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        target_path = os.path.normcase(os.path.abspath(log_path))
        for existing in list(root_logger.handlers):
            if not isinstance(existing, RotatingFileHandler):
                continue
            existing_path = os.path.normcase(
                os.path.abspath(str(getattr(existing, "baseFilename", "")))
            )
            if existing_path != target_path:
                continue
            root_logger.removeHandler(existing)
            existing.close()
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        root_logger.addHandler(file_handler)
    except Exception as exc:  # noqa: BLE001
        if file_handler is not None:
            try:
                root_logger.removeHandler(file_handler)
            except Exception:  # noqa: BLE001
                pass
            try:
                file_handler.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            root_logger.warning(
                "持久日志不可用，已降级为控制台/内存日志：%s（%s）",
                log_path,
                exc,
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = ["install_file_log_handler"]
