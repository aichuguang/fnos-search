from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
        manifest_persistence = self.content.index(
            'if ! persist_staging_manifest "$copy_file_list"'
        )
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
        self.assertIn("refusing an empty manifest", stability)

    def test_cross_mount_copy_disables_webdav_server_side_copy(self) -> None:
        copy_file = self.content[
            self.content.index("copy_file() {") : self.content.index("copy_scoped_job() {")
        ]

        # Every OpenList mount shares the managed WebDAV remote.  Without
        # this flag rclone treats a cross-mount transfer as a same-backend
        # server-side COPY; OpenList then returns object-not-found when the
        # two paths belong to different storage drivers.  Streaming through
        # rclone is the required cross-provider behavior.
        self.assertEqual(copy_file.count("--disable Copy"), 2)

    def test_progress_lines_are_flushed_line_by_line(self) -> None:
        progress_filter = self._progress_filter_source()

        # progress_filter 的 stdout 接 Python 端捕获管道（非终端），awk 默认块
        # 缓冲。没有逐行 fflush 的话，5 秒一条的传输进度会积压在缓冲区里，
        # 管理后台长时间看不到任何进度（正是 2026-09-04 的线上问题）。
        self.assertGreaterEqual(progress_filter.count("fflush()"), 2)

    def test_progress_filter_translates_both_rclone_stats_field_layouts(self) -> None:
        awk = shutil.which("awk")
        if not awk:  # pragma: no cover - Windows 开发机可能没有 awk
            self.skipTest("awk not available on PATH")
        program = self._awk_program()
        sample = "\n".join(
            (
                # rclone 3 段格式（带时间戳与文件名前缀）
                "2026/09/04 19:40:00 NOTICE: 电影.mkv: 47% of 6.28GiB, 10.4MiB/s, ETA 9m32s",
                # rclone 4 段格式（百分比 + 已传/总量）
                "NOTICE: 电影.mkv: 45%, 3.04GiB/6.74GiB, 10.5MiB/s, ETA 5m32s",
                # 传输刚起步，ETA 未知
                "NOTICE: 电影.mkv: 0% of 6.74GiB, 0 B/s, ETA -",
                # 非统计行原样透传
                "2026/09/04 19:40:05 ERROR 电影.mkv: 404 not found",
            )
        ) + "\n"
        completed = subprocess.run(
            [awk, "-v", "progress_label=电影.mkv", "-v", "progress_direction=下载", program],
            input=sample,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            completed.stdout.splitlines(),
            [
                "下载中 电影.mkv：47% of 6.28GiB，速度 10.4MiB/s，预计剩余 9m32s",
                "下载中 电影.mkv：45%，3.04GiB/6.74GiB，速度 10.5MiB/s，预计剩余 5m32s",
                "下载中 电影.mkv：0% of 6.74GiB，速度 0 B/s，预计剩余 计算中",
                "2026/09/04 19:40:05 ERROR 电影.mkv: 404 not found",
            ],
        )

    def test_progress_filter_falls_back_without_direction(self) -> None:
        awk = shutil.which("awk")
        if not awk:  # pragma: no cover - Windows 开发机可能没有 awk
            self.skipTest("awk not available on PATH")
        program = self._awk_program()
        completed = subprocess.run(
            [awk, "-v", "progress_label=电影.mkv", program],
            input="NOTICE: 电影.mkv: 45%, 3.04GiB/6.74GiB, 10.5MiB/s, ETA 5m32s\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        self.assertEqual(
            completed.stdout.splitlines(),
            ["传输中 电影.mkv：45%，3.04GiB/6.74GiB，速度 10.5MiB/s，预计剩余 5m32s"],
        )

    def test_scoped_job_labels_progress_direction_by_target_kind(self) -> None:
        scoped = self.content[
            self.content.index("copy_scoped_job() {") : self.content.index("acquire_lock() {")
        ]
        # 中继本地目标标“下载”，远端目标标“上传”；文件级日志与重试标签同向。
        self.assertIn('copy_direction_label="下载"', scoped)
        self.assertIn('copy_direction_label="上传"', scoped)
        self.assertIn('log "${copy_direction_label}文件：', scoped)
        self.assertIn('retry_count "${copy_direction_label} ', scoped)
        # 中继链路结束≠任务完成：结束语必须说明“移交网盘上传”，不能让
        # 用户在 rclone 日志页误以为下载结束就是整个任务结束。
        self.assertIn(
            'log "rclone 下载完成：${copy_job_dir}，下载 ${copied_count} 个文件，失败 ${failed_count} 个，已移交网盘上传"',
            scoped,
        )
        copy_file = self.content[
            self.content.index("copy_file() {") : self.content.index("copy_scoped_job() {")
        ]
        self.assertIn(
            "export RCLONE_PROGRESS_LABEL RCLONE_PROGRESS_DIRECTION RCLONE_PROGRESS_MARKER",
            copy_file,
        )

    def test_progress_filter_writes_first_stats_marker(self) -> None:
        awk = shutil.which("awk")
        if not awk:  # pragma: no cover - Windows 开发机可能没有 awk
            self.skipTest("awk not available on PATH")
        program = self._awk_program()
        with tempfile.TemporaryDirectory() as tmp:
            # 用相对哨兵名并在临时目录内执行：Windows awk 会把带盘符的路径
            # 当成普通文件名写到当前目录，污染仓库根。
            completed = subprocess.run(
                [
                    awk,
                    "-v",
                    "progress_label=电影.mkv",
                    "-v",
                    "progress_direction=下载",
                    "-v",
                    "progress_marker=progress.marker",
                    program,
                ],
                input="NOTICE: 电影.mkv: 45%, 3.04GiB/6.74GiB, 10.5MiB/s, ETA 5m32s\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=True,
                cwd=tmp,
            )
            self.assertEqual(completed.stderr, "")
            marker = Path(tmp) / "progress.marker"
            self.assertTrue(marker.exists(), "首条统计行必须落盘哨兵文件")
            marker_created = self.content.index("print \"\" > progress_marker")
            stats_branch = self.content.index("n >= 3 && parts[n] ~ /^ETA[ \\t]/")
            self.assertLess(stats_branch, marker_created)

    def test_copy_file_runs_slow_source_watchdog_until_first_progress(self) -> None:
        copy_file = self.content[
            self.content.index("copy_file() {") : self.content.index("copy_file_once() {")
        ]
        # 源网盘准备下载链接期间 rclone 完全静默（2026-09-05 线上等待近 3 分钟），
        # 看门狗必须：10s 轮询哨兵、60s 无首字节提示一次、结束后回收进程与临时文件。
        self.assertIn('copy_progress_marker="$(mktemp "${TMPDIR:-/tmp}/fnos-rclone-progress.XXXXXX")"', copy_file)
        self.assertIn('rm -f "$copy_progress_marker"', copy_file)
        self.assertIn("RCLONE_PROGRESS_MARKER", copy_file)
        self.assertIn("sleep 10", copy_file)
        self.assertIn("if [ -e \"$copy_progress_marker\" ]; then", copy_file)
        self.assertIn("if [ \"$copy_waited\" -ge 60 ]", copy_file)
        self.assertIn("源端响应缓慢", copy_file)
        self.assertIn("通常是源网盘在准备下载链接", copy_file)
        self.assertIn('kill "$copy_watchdog_pid" 2>/dev/null || true', copy_file)
        self.assertIn('wait "$copy_watchdog_pid" 2>/dev/null || true', copy_file)
        self.assertIn('rm -f "$copy_rc_file" "$copy_progress_marker"', copy_file)

    def _progress_filter_source(self) -> str:
        return self.content[
            self.content.index("progress_filter() {") : self.content.index("copy_file() {")
        ]

    def _awk_program(self) -> str:
        source = self._progress_filter_source()
        match = re.search(
            r"awk -v progress_label=\"\$\{RCLONE_PROGRESS_LABEL:-\}\""
            r" -v progress_direction=\"\$\{RCLONE_PROGRESS_DIRECTION:-\}\""
            r" -v progress_marker=\"\$\{RCLONE_PROGRESS_MARKER:-\}\" '\n(.*?)'\n\}",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "progress_filter must embed an awk program")
        return match.group(1)


if __name__ == "__main__":
    unittest.main()
