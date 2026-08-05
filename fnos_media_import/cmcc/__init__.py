"""CMCC official API upload integration.

The package is intentionally independent from rclone uploads: rclone keeps
downloading source files to the shared temp volume, then this module uploads
the local file through China Mobile Cloud Drive official APIs.
"""

from .auth import CmccAuthExpired, CmccAuthProvider
from .uploader import CmccUploadResult, CmccUploader

__all__ = [
    "CmccAuthExpired",
    "CmccAuthProvider",
    "CmccUploadResult",
    "CmccUploader",
]
