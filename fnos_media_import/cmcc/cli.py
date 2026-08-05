from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .auth import CmccAuthConfigError, CmccAuthExpired
from .client import CmccApiError, CmccClient
from .uploader import CmccUploader, _build_part_infos, sha256_file


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _print_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def cmd_upload(args: argparse.Namespace) -> int:
    uploader = CmccUploader()
    mode = args.mode or os.getenv("CMCC_UPLOAD_MODE", "rapid_first")
    progress_enabled = str(os.getenv("CMCC_UPLOAD_PROGRESS", "true") or "true").strip().lower() not in {"0", "false", "no", "off"}
    try:
        result = uploader.upload_file(
            local_file=args.local_file,
            target_parent_file_id=args.target_parent_file_id or "",
            target_parent_path=args.target_parent_path or "",
            target_name=args.target_name or "",
            target_relative_dir=args.target_relative_dir or "",
            category=args.category or "",
            mode=mode,
            rename_mode=args.rename_mode or os.getenv("CMCC_UPLOAD_RENAME_MODE", "auto_rename"),
            progress=_print_progress if progress_enabled else None,
        )
        _print_json(result.to_dict())
        if result.success:
            return 0
        if result.status == "rapid_miss":
            return 20
        return 1
    except CmccAuthExpired as exc:
        _print_json({"success": False, "status": "auth_expired", "message": str(exc)})
        return 12
    except CmccAuthConfigError as exc:
        _print_json({"success": False, "status": "auth_config_error", "message": str(exc)})
        return 11
    except CmccApiError as exc:
        status = "auth_expired" if exc.auth_expired else "failed"
        _print_json(
            {
                "success": False,
                "status": status,
                "message": str(exc),
                "code": exc.code,
                "http_status": exc.status_code,
            }
        )
        return 12 if exc.auth_expired else 1
    except Exception as exc:  # noqa: BLE001
        _print_json({"success": False, "status": "failed", "message": str(exc)})
        return 1


def _default_probe_sha(size: int) -> str:
    if size <= 0:
        return hashlib.sha256(b"").hexdigest()
    if size == 1:
        return hashlib.sha256(b"\0").hexdigest()
    return ""


def cmd_probe_create(args: argparse.Namespace) -> int:
    """Probe the WebAPI create-file request and print request/response JSON."""

    client = CmccClient()
    try:
        local_file = Path(args.local_file) if args.local_file else None
        if local_file:
            if not local_file.exists() or not local_file.is_file():
                raise CmccApiError(f"local file does not exist: {local_file}")
            size = local_file.stat().st_size
            file_name = args.file_name or local_file.name
            content_hash = args.sha256 or sha256_file(local_file, progress=_print_progress, progress_label=f"CMCC API probe hash progress: {file_name}")
        else:
            size = int(args.size)
            file_name = args.file_name or f".fnos-cmcc-probe-{int(time.time())}.bin"
            content_hash = args.sha256 or _default_probe_sha(size)

        parent_file_id = args.target_parent_file_id or ""
        parent_path = args.target_parent_path or ""
        if not parent_file_id and args.target_relative_dir:
            parent_path = CmccUploader._join_cloud_path(parent_path, args.target_relative_dir)

        create_path, payload, parent_id = client.build_create_file_request(
            name=file_name,
            size=size,
            parent_file_id=parent_file_id,
            parent_path=parent_path,
            content_hash=content_hash,
            rename_mode=args.rename_mode or os.getenv("CMCC_UPLOAD_RENAME_MODE", "auto_rename"),
            part_infos=_build_part_infos(size)[:100],
        )
        request_payload = {
            "method": "POST",
            "url": f"{client.host}{client._normalize_path(create_path)}",
            "headers": client.redacted_headers_for_debug(payload=payload),
            "json": payload,
            "sign_catalog_id": parent_id,
        }

        output: dict = {
            "success": False,
            "status": "request_built",
            "request": request_payload,
        }
        if args.no_send:
            output["message"] = "request built only; --no-send enabled"
            _print_json(output)
            return 0

        response = client.post_json(create_path, payload, retries=0, sign_catalog_id=parent_id)
        try:
            data = client.require_success(response, action="probe create file")
            upload_url = CmccUploader._extract_upload_url(data)
            output.update(
                {
                    "success": True,
                    "status": "created",
                    "message": "CMCC WebAPI create request returned success",
                    "response": response,
                    "data_keys": sorted(str(key) for key in data.keys()),
                    "upload_url_detected": bool(upload_url),
                    "file_id": str(data.get("fileId") or data.get("file_id") or ""),
                    "upload_id": str(data.get("uploadId") or data.get("upload_id") or ""),
                }
            )
        except CmccApiError as exc:
            output.update(
                {
                    "success": False,
                    "status": "api_rejected",
                    "message": str(exc),
                    "code": exc.code,
                    "response": response,
                }
            )
        _print_json(output)
        return 0 if output.get("success") else 1
    except CmccAuthExpired as exc:
        _print_json({"success": False, "status": "auth_expired", "message": str(exc)})
        return 12
    except CmccAuthConfigError as exc:
        _print_json({"success": False, "status": "auth_config_error", "message": str(exc)})
        return 11
    except CmccApiError as exc:
        status = "auth_expired" if exc.auth_expired else "failed"
        _print_json(
            {
                "success": False,
                "status": status,
                "message": str(exc),
                "code": exc.code,
                "http_status": exc.status_code,
            }
        )
        return 12 if exc.auth_expired else 1
    except Exception as exc:  # noqa: BLE001
        _print_json({"success": False, "status": "failed", "message": str(exc)})
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload a local file to CMCC cloud by official API")
    sub = parser.add_subparsers(dest="command", required=True)
    upload = sub.add_parser("upload", help="upload local file")
    upload.add_argument("--local-file", required=True)
    upload.add_argument("--target-parent-file-id", default="")
    upload.add_argument("--target-parent-path", default="")
    upload.add_argument("--target-relative-dir", default="")
    upload.add_argument("--target-name", default="")
    upload.add_argument("--category", default="")
    upload.add_argument("--mode", default="")
    upload.add_argument("--rename-mode", default="")
    upload.set_defaults(func=cmd_upload)
    probe = sub.add_parser("probe-create", help="print/send a CMCC WebAPI create-file probe")
    probe.add_argument("--local-file", default="")
    probe.add_argument("--file-name", default="")
    probe.add_argument("--size", type=int, default=1)
    probe.add_argument("--sha256", default="")
    probe.add_argument("--target-parent-file-id", default="")
    probe.add_argument("--target-parent-path", default="")
    probe.add_argument("--target-relative-dir", default="")
    probe.add_argument("--rename-mode", default="")
    probe.add_argument("--no-send", action="store_true", help="only print the redacted request; do not call WebAPI")
    probe.set_defaults(func=cmd_probe_create)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
