from __future__ import annotations

import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import mock_open, patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fnos_rclone_worker.sh"


class RcloneWorkerSourceStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.content = SCRIPT_PATH.read_text(encoding="utf-8")

    def _embedded_python(self, marker: str) -> str:
        match = re.search(
            rf"<<'{re.escape(marker)}'\n(?P<code>.*?)\n{re.escape(marker)}",
            self.content,
            re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing embedded Python block: {marker}")
        return match.group("code")

    def _run_embedded_python(
        self,
        code: str,
        file_content: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch("builtins.open", mock_open(read_data=file_content)),
            patch.object(sys, "argv", ["worker", "snapshot-input"]),
            patch.dict(os.environ, environment or {}, clear=False),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exec(compile(code, "<worker-embedded-python>", "exec"), {})
        return stdout.getvalue(), stderr.getvalue()

    def _run_snapshot_formatter(self, payload: list[dict]) -> list[list[object]]:
        code = self._embedded_python("PY_REMOTE_SNAPSHOT")
        stdout, stderr = self._run_embedded_python(
            code,
            json.dumps(payload, ensure_ascii=False),
        )
        self.assertEqual(stderr, "")
        return [json.loads(line) for line in stdout.splitlines() if line]

    def _emit_snapshot_paths(self, records: list[list[object]]) -> list[str]:
        code = self._embedded_python("PY_REMOTE_PATHS")
        suffixes = "\n".join(
            (
                ".!qB",
                ".partial",
                ".part",
                ".parts",
                ".tmp",
                ".temp",
                ".download",
                ".downloading",
                ".crdownload",
                ".aria2",
                ".incomplete",
                ".unfinished",
                ".filepart",
            )
        )
        stdout, stderr = self._run_embedded_python(
            code,
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            environment={"RCLONE_INCOMPLETE_FILE_SUFFIXES": suffixes},
        )
        self.assertEqual(stderr, "")
        return stdout.splitlines()

    def test_snapshot_compares_path_size_and_modtime(self) -> None:
        base = {
            "Path": "job-42/剧集/第01集.mkv",
            "Size": 1024,
            "ModTime": "2026-07-30T01:02:03Z",
            "IsDir": False,
        }

        initial = self._run_snapshot_formatter([base])
        size_changed = self._run_snapshot_formatter([{**base, "Size": 2048}])
        modtime_changed = self._run_snapshot_formatter(
            [{**base, "ModTime": "2026-07-30T01:03:04Z"}]
        )

        self.assertEqual(initial, [[base["Path"], 1024, base["ModTime"]]])
        self.assertNotEqual(initial, size_changed)
        self.assertNotEqual(initial, modtime_changed)
        self.assertIn('if [ "$first" = "$second" ]', self.content)

    def test_incomplete_suffixes_are_filtered_before_manifest(self) -> None:
        suffixes = (
            ".PARTIAL",
            ".part",
            ".tmp",
            ".TEMP",
            ".download",
            ".crdownload",
            ".ARIA2",
            ".!QB",
        )
        payload = [
            {
                "Path": f"job-42/剧集/未完成-{index}.mkv{suffix}",
                "Size": index,
                "ModTime": "2026-07-30T01:02:03Z",
                "IsDir": False,
            }
            for index, suffix in enumerate(suffixes, start=1)
        ]
        payload.extend(
            (
                {
                    "Path": "job-42/剧集/Movie.part1.mkv",
                    "Size": 200,
                    "ModTime": "2026-07-30T01:02:03Z",
                    "IsDir": False,
                },
                {
                    "Path": "job-42/剧集/正常.mkv",
                    "Size": 300,
                    "ModTime": "2026-07-30T01:02:03Z",
                    "IsDir": False,
                },
            )
        )

        records = self._run_snapshot_formatter(payload)
        paths = self._emit_snapshot_paths(records)

        self.assertEqual(len(records), len(payload))
        self.assertEqual(
            paths,
            ["job-42/剧集/Movie.part1.mkv", "job-42/剧集/正常.mkv"],
        )
        snapshot_filter = self.content.index("relative_path.casefold().endswith(suffixes)")
        manifest_persistence = self.content.index('if ! persist_staging_manifest "$file_list"')
        self.assertLess(snapshot_filter, manifest_persistence)

    def test_stable_output_decodes_snapshot_back_to_relative_paths(self) -> None:
        records = [
            ["job-42/剧集/第01集.mkv", 1024, "2026-07-30T01:02:03Z"],
            ["job-42/剧集/第02集.srt", 128, "2026-07-30T01:02:04Z"],
        ]
        paths = self._emit_snapshot_paths(records)

        self.assertEqual(paths, [record[0] for record in records])
        self.assertTrue(all(not path.startswith("/") for path in paths))

    def test_listing_failures_are_not_masked_as_empty_directories(self) -> None:
        listing = self.content[
            self.content.index("list_remote_file_snapshots() {") : self.content.index(
                "emit_snapshot_paths() {"
            )
        ]
        stability = self.content[
            self.content.index("stable_file_list() {") : self.content.index("verify_remote_size() {")
        ]

        self.assertIn('rclone_cmd lsjson "$remote" -R --files-only', listing)
        self.assertNotIn("|| true", listing)
        self.assertIn('if ! first="$(list_remote_file_snapshots "$remote")"; then', stability)
        self.assertIn('if ! second="$(list_remote_file_snapshots "$remote")"; then', stability)
        self.assertIn("本轮拒绝按空目录继续", stability)


if __name__ == "__main__":
    unittest.main()
