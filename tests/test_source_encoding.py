from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".py", ".js", ".css", ".html", ".json", ".md", ".yaml", ".yml", ".toml", ".sh"}
EXCLUDED_DIRS = {".git", ".claude", ".codex", ".venv", "node_modules", "data", "logs", "codex_tmp"}
MOJIBAKE_MARKERS = ("\ufffd", "\u951f\u65a4\u62f7", "\u9983", "\u9286")


class SourceEncodingTests(unittest.TestCase):
    def test_project_sources_are_utf8_without_bom_or_known_mojibake(self) -> None:
        failures: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts):
                continue
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                failures.append(f"{path.relative_to(ROOT)}: UTF-8 BOM")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                failures.append(f"{path.relative_to(ROOT)}: {exc}")
                continue
            marker = next((item for item in MOJIBAKE_MARKERS if item in text), "")
            if marker:
                failures.append(f"{path.relative_to(ROOT)}: suspicious marker {marker!r}")

        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
