from __future__ import annotations

import re
import unittest
from typing import Any

from fnos_media_import.organizer.openlist_client import OpenListEndpointUnsupported, basename
from fnos_media_import.organizer.service import OrganizerService


def _go_replacement_to_python(value: str) -> str:
    literal_dollar = "\x00"
    converted = str(value).replace("$$", literal_dollar)
    converted = re.sub(
        r"\$\{(\d+)\}|\$(\d+)",
        lambda match: rf"\g<{match.group(1) or match.group(2)}>",
        converted,
    )
    return converted.replace(literal_dollar, "$")


class _Item:
    def __init__(self, name: str, *, path: str = "", is_dir: bool = False) -> None:
        self.name = name
        self.path = path
        self.is_dir = is_dir


class _FakeOpenList:
    def __init__(
        self,
        source_names: set[str],
        target_names: set[str],
        *,
        fail_list_calls: dict[str, set[int]] | None = None,
    ) -> None:
        self.source = set(source_names)
        self.target = set(target_names)
        self.source_identity = {name: name for name in source_names}
        self.target_identity = {name: name for name in target_names}
        self.renames: list[tuple[str, str]] = []
        self.batch_rename_calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.regex_rename_calls: list[tuple[str, str, str]] = []
        self.recursive_move_calls: list[tuple[str, str, str]] = []
        self.move_many_calls: list[tuple[str, str, list[str]]] = []
        self.fail_list_calls = fail_list_calls or {}
        self.list_calls: dict[str, int] = {}

    def list_dir(self, path: str, refresh: bool | None = None) -> list[_Item]:
        normalized = str(path or "").rstrip("/")
        self.list_calls[normalized] = self.list_calls.get(normalized, 0) + 1
        if self.list_calls[normalized] in self.fail_list_calls.get(normalized, set()):
            raise RuntimeError(f"list failed: {normalized}")
        if normalized == "/src":
            return [_Item(name) for name in sorted(self.source)]
        if normalized == "/dst":
            return [_Item(name) for name in sorted(self.target)]
        return []

    def rename(self, source_path: str, new_name: str, *, overwrite: bool = False) -> bool:
        old = basename(source_path)
        parent = str(source_path).replace("\\", "/").rsplit("/", 1)[0] or "/"
        names = self.target if parent == "/dst" else self.source
        identities = self.target_identity if parent == "/dst" else self.source_identity
        if old not in names:
            raise RuntimeError(f"missing source: {old}")
        if new_name in names and new_name != old:
            raise RuntimeError(f"target exists: {new_name}")
        identity = identities.pop(old)
        names.discard(old)
        names.add(new_name)
        identities[new_name] = identity
        self.renames.append((old, new_name))
        return True

    def batch_rename(self, source_dir: str, renames: list[tuple[str, str]], **kwargs: Any) -> bool:
        normalized = str(source_dir).replace("\\", "/").rstrip("/")
        self.batch_rename_calls.append((normalized, list(renames)))
        for source_name, target_name in renames:
            self.rename(f"{normalized}/{source_name}", target_name)
        return True

    def regex_rename(self, source_dir: str, source_regex: str, replacement: str, **kwargs: Any) -> bool:
        normalized = str(source_dir).replace("\\", "/").rstrip("/")
        self.regex_rename_calls.append((normalized, source_regex, replacement))
        names = list(self.target if normalized == "/dst" else self.source)
        compiled = re.compile(source_regex)
        python_replacement = _go_replacement_to_python(replacement)
        for name in names:
            if compiled.fullmatch(name):
                self.rename(f"{normalized}/{name}", compiled.sub(python_replacement, name))
        return True

    def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
        self.move_many_calls.append((str(source_dir).rstrip("/"), str(target_dir).rstrip("/"), list(names)))
        if str(source_dir).rstrip("/") == str(target_dir).rstrip("/"):
            return True
        moving_from_target = str(source_dir).rstrip("/") == "/dst"
        source_names = self.target if moving_from_target else self.source
        source_identities = self.target_identity if moving_from_target else self.source_identity
        target_names = self.source if moving_from_target else self.target
        target_identities = self.source_identity if moving_from_target else self.target_identity
        for name in names:
            if name in source_names:
                identity = source_identities.pop(name)
                source_names.discard(name)
                target_names.add(name)
                target_identities[name] = identity
        return True

    def exists(self, path: str) -> bool:
        normalized = str(path or "").replace("\\", "/")
        parent, name = normalized.rsplit("/", 1)
        return name in (self.target if parent == "/dst" else self.source)

    def move(self, source_path: str, target_dir: str, **kwargs: Any) -> bool:
        normalized = str(source_path or "").replace("\\", "/")
        source_dir, name = normalized.rsplit("/", 1)
        return self.move_many(source_dir, target_dir, [name], **kwargs)


class _TreeFakeOpenList:
    def __init__(self, files: set[str]) -> None:
        self.files = {self._normalize(path): path for path in files}
        self.batch_rename_calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.regex_rename_calls: list[tuple[str, str, str]] = []
        self.move_many_calls: list[tuple[str, str, list[str]]] = []
        self.recursive_move_calls: list[tuple[str, str, str]] = []
        self.recursive_move_snapshots: list[set[str]] = []

    @staticmethod
    def _normalize(path: str) -> str:
        text = "/" + str(path or "").replace("\\", "/").strip("/")
        return text.rstrip("/") or "/"

    def list_dir(self, path: str, refresh: bool | None = None) -> list[_Item]:
        root = self._normalize(path)
        prefix = root.rstrip("/") + "/"
        children: dict[str, bool] = {}
        for file_path in self.files:
            if not file_path.startswith(prefix):
                continue
            relative = file_path[len(prefix):]
            first, separator, _rest = relative.partition("/")
            children[first] = bool(separator)
        return [
            _Item(name, path=f"{prefix}{name}", is_dir=is_dir)
            for name, is_dir in sorted(children.items())
        ]

    def exists(self, path: str) -> bool:
        normalized = self._normalize(path)
        prefix = normalized.rstrip("/") + "/"
        return normalized in self.files or any(file_path.startswith(prefix) for file_path in self.files)

    def rename(self, source_path: str, new_name: str, *, overwrite: bool = False) -> bool:
        source = self._normalize(source_path)
        if source not in self.files:
            raise RuntimeError(f"missing source: {source}")
        target = f"{source.rsplit('/', 1)[0]}/{new_name}"
        if target in self.files and target != source:
            raise RuntimeError(f"target exists: {target}")
        identity = self.files.pop(source)
        self.files[target] = identity
        return True

    def batch_rename(self, source_dir: str, renames: list[tuple[str, str]], **kwargs: Any) -> bool:
        source = self._normalize(source_dir)
        self.batch_rename_calls.append((source, list(renames)))
        for old_name, new_name in renames:
            self.rename(f"{source}/{old_name}", new_name)
        return True

    def regex_rename(self, source_dir: str, source_regex: str, replacement: str, **kwargs: Any) -> bool:
        source = self._normalize(source_dir)
        self.regex_rename_calls.append((source, source_regex, replacement))
        compiled = re.compile(source_regex)
        python_replacement = _go_replacement_to_python(replacement)
        for file_path in list(self.files):
            if file_path.rsplit("/", 1)[0] != source:
                continue
            name = file_path.rsplit("/", 1)[1]
            if compiled.fullmatch(name):
                self.rename(file_path, compiled.sub(python_replacement, name))
        return True

    def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
        source = self._normalize(source_dir)
        target = self._normalize(target_dir)
        self.move_many_calls.append((source, target, list(names)))
        for name in names:
            source_path = f"{source}/{name}"
            if source_path not in self.files:
                continue
            target_path = f"{target}/{name}"
            if target_path in self.files:
                continue
            self.files[target_path] = self.files.pop(source_path)
        return True

    def move(self, source_path: str, target_dir: str, **kwargs: Any) -> bool:
        source = self._normalize(source_path)
        return self.move_many(source.rsplit("/", 1)[0], target_dir, [source.rsplit("/", 1)[1]], **kwargs)

    def recursive_move(self, source_dir: str, target_dir: str, *, conflict_policy: str = "cancel", **kwargs: Any) -> bool:
        source = self._normalize(source_dir)
        target = self._normalize(target_dir)
        self.recursive_move_calls.append((source, target, conflict_policy))
        moving = {path for path in self.files if path.startswith(source.rstrip("/") + "/")}
        self.recursive_move_snapshots.append({path.rsplit("/", 1)[1] for path in moving})
        for source_path in sorted(moving):
            target_path = f"{target}/{source_path.rsplit('/', 1)[1]}"
            if target_path in self.files:
                if conflict_policy == "cancel":
                    raise RuntimeError(f"target exists: {target_path}")
                if conflict_policy == "skip":
                    continue
            self.files[target_path] = self.files.pop(source_path)
        return True


def _svc(fake: Any) -> OrganizerService:
    service = OrganizerService.__new__(OrganizerService)
    service.openlist = fake
    service.organizer_config = {
        "bulk_operations_enabled": True,
        "regex_rename_min_items": 10,
        "bulk_reconcile_timeout_seconds": 0,
        "max_files_per_task": 500,
    }
    return service


def _op(op_id: int, source: str, target: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"id": op_id, "type": "move_file", "status": "pending", "source_path": source, "target_path": target, "raw_data": {}}
    data.update(extra)
    return data


class OrganizerBatchMoveTests(unittest.TestCase):
    def test_batch_execute_renames_and_moves_all(self) -> None:
        fake = _FakeOpenList(source_names={"a.mp4", "b.mp4", "c.mp4"}, target_names=set())
        svc = _svc(fake)
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4"),
            _op(2, "/src/b.mp4", "/dst/S01E02.mp4"),
            _op(3, "/src/c.mp4", "/dst/S01E03.mp4"),
        ]
        verdicts = svc._execute_move_file_batch(ops)
        self.assertEqual([v[1] for v in verdicts], ["done", "done", "done"])
        # 只调用一次批量 move，且带全部 3 个目标名
        self.assertEqual(len(fake.move_many_calls), 1)
        self.assertEqual(set(fake.move_many_calls[0][2]), {"S01E01.mp4", "S01E02.mp4", "S01E03.mp4"})
        # 普通目标名与源目录不冲突时只需一次精确批量重命名。
        self.assertEqual(len(fake.batch_rename_calls), 1)
        self.assertEqual(len(fake.renames), 3)
        # 目标目录最终包含全部，源目录清空
        self.assertEqual(fake.target, {"S01E01.mp4", "S01E02.mp4", "S01E03.mp4"})
        self.assertEqual(fake.source, set())
        # 多文件只保存一个批量 inverse，避免交叉名称在逐条回滚时互相冲突。
        inverse = verdicts[0][3]
        self.assertEqual((inverse or {}).get("type"), "move_file_batch")
        self.assertTrue(all(item[3] is None for item in verdicts[1:]))
        svc._execute_inverse(inverse or {})
        self.assertEqual(fake.source, {"a.mp4", "b.mp4", "c.mp4"})
        self.assertEqual(fake.target, set())

    def test_homogeneous_episode_names_use_one_regex_rename_and_one_move(self) -> None:
        source_names = {f"release-E{episode:02d}.1080i.ts" for episode in range(1, 51)}
        fake = _FakeOpenList(source_names=source_names, target_names=set())
        svc = _svc(fake)
        ops = [
            _op(
                episode,
                f"/src/release-E{episode:02d}.1080i.ts",
                f"/dst/Show (2020) - S01E{episode:02d}.ts",
            )
            for episode in range(1, 51)
        ]

        verdicts = svc._execute_move_file_batch(ops)

        self.assertTrue(all(item[1] == "done" for item in verdicts))
        self.assertEqual(len(fake.regex_rename_calls), 1)
        self.assertIn("${1}", fake.regex_rename_calls[0][2])
        self.assertEqual(fake.batch_rename_calls, [])
        self.assertEqual(len(fake.move_many_calls), 1)

    def test_regex_overmatch_falls_back_to_exact_batch_rename(self) -> None:
        source_names = {f"release-E{episode:02d}.1080i.ts" for episode in range(1, 13)} | {"release-E99.1080i.ts"}
        fake = _FakeOpenList(source_names=source_names, target_names=set())
        ops = [
            _op(
                episode,
                f"/src/release-E{episode:02d}.1080i.ts",
                f"/dst/Show (2020) - S01E{episode:02d}.ts",
            )
            for episode in range(1, 13)
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops)

        self.assertTrue(all(item[1] == "done" for item in verdicts))
        self.assertEqual(fake.regex_rename_calls, [])
        self.assertEqual(len(fake.batch_rename_calls), 1)
        self.assertIn("release-E99.1080i.ts", fake.source)

    def test_episode_width_change_falls_back_to_exact_batch_rename(self) -> None:
        source_names = {f"release-E{episode}.ts" for episode in range(1, 11)}
        fake = _FakeOpenList(source_names=source_names, target_names=set())
        ops = [
            _op(episode, f"/src/release-E{episode}.ts", f"/dst/Show - S01E{episode:02d}.ts")
            for episode in range(1, 11)
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops)

        self.assertTrue(all(item[1] == "done" for item in verdicts))
        self.assertEqual(fake.regex_rename_calls, [])
        self.assertEqual(len(fake.batch_rename_calls), 1)

    def test_unsupported_regex_endpoint_falls_back_to_batch_rename(self) -> None:
        class _NoRegex(_FakeOpenList):
            def regex_rename(self, *args: Any, **kwargs: Any) -> bool:
                raise OpenListEndpointUnsupported("404")

        source_names = {f"release-E{episode:02d}.ts" for episode in range(1, 11)}
        fake = _NoRegex(source_names=source_names, target_names=set())
        ops = [
            _op(episode, f"/src/release-E{episode:02d}.ts", f"/dst/Show - S01E{episode:02d}.ts")
            for episode in range(1, 11)
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops)

        self.assertTrue(all(item[1] == "done" for item in verdicts))
        self.assertEqual(len(fake.batch_rename_calls), 1)

    def test_unsupported_batch_rename_endpoint_falls_back_to_legacy_renames(self) -> None:
        class _NoBatchRename(_FakeOpenList):
            def batch_rename(self, *args: Any, **kwargs: Any) -> bool:
                raise OpenListEndpointUnsupported("404")

        fake = _NoBatchRename({"a.mp4", "b.mp4"}, set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(fake.renames, [("a.mp4", "x.mp4"), ("b.mp4", "y.mp4")])
        self.assertEqual(len(fake.move_many_calls), 1)

    def test_partial_batch_rename_failure_is_reconciled_and_rolled_back(self) -> None:
        class _PartialBatchRename(_FakeOpenList):
            def batch_rename(self, source_dir: str, renames: list[tuple[str, str]], **kwargs: Any) -> bool:
                self.batch_rename_calls.append((source_dir, list(renames)))
                if renames:
                    self.rename(f"{source_dir}/{renames[0][0]}", renames[0][1])
                raise TimeoutError("batch rename timed out")

        fake = _PartialBatchRename({"a.mp4", "b.mp4"}, set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertEqual(fake.source_identity, {"a.mp4": "a.mp4", "b.mp4": "b.mp4"})
        self.assertEqual(fake.target_identity, {})

    def test_partial_first_phase_cross_rename_does_not_swap_file_identity(self) -> None:
        class _PartialFirstPhase(_FakeOpenList):
            def batch_rename(self, source_dir: str, renames: list[tuple[str, str]], **kwargs: Any) -> bool:
                self.batch_rename_calls.append((source_dir, list(renames)))
                if renames:
                    self.rename(f"{source_dir}/{renames[0][0]}", renames[0][1])
                raise TimeoutError("first rename phase timed out")

        fake = _PartialFirstPhase({"a.mp4", "b.mp4"}, set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/b.mp4"), _op(2, "/src/b.mp4", "/dst/a.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertEqual(fake.source_identity, {"a.mp4": "a.mp4", "b.mp4": "b.mp4"})
        self.assertEqual(fake.target_identity, {})

    def test_noop_success_response_cannot_advance_cross_rename_phase(self) -> None:
        class _NoOpSuccess(_FakeOpenList):
            def batch_rename(self, source_dir: str, renames: list[tuple[str, str]], **kwargs: Any) -> bool:
                self.batch_rename_calls.append((source_dir, list(renames)))
                return True

        fake = _NoOpSuccess({"a.mp4", "b.mp4"}, set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/b.mp4"), _op(2, "/src/b.mp4", "/dst/a.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertEqual(len(fake.batch_rename_calls), 1)
        self.assertEqual(fake.move_many_calls, [])
        self.assertEqual(fake.source_identity, {"a.mp4": "a.mp4", "b.mp4": "b.mp4"})

    def test_batch_skips_already_done_and_fails_conflict(self) -> None:
        fake = _FakeOpenList(source_names={"a.mp4", "b.mp4"}, target_names={"S01E01.mp4"})
        svc = _svc(fake)
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4"),  # 目标在、源在 → 冲突
            _op(2, "/src/b.mp4", "/dst/S01E02.mp4"),  # 正常
            _op(3, "/src/gone.mp4", "/dst/S01E03.mp4"),  # 源不存在
        ]
        verdicts = svc._execute_move_file_batch(ops)
        by_id = {v[0]["id"]: v[1] for v in verdicts}
        self.assertEqual(by_id[1], "failed")
        self.assertEqual(by_id[2], "done")
        self.assertEqual(by_id[3], "failed")

    def test_collect_stops_on_different_target_dir(self) -> None:
        svc = _svc(_FakeOpenList(set(), set()))
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4"),
            _op(2, "/src/b.mp4", "/dst/S01E02.mp4"),
            _op(3, "/src/c.mp4", "/other/S01E03.mp4"),
        ]
        batch = svc._collect_move_file_batch(ops, 0)
        self.assertEqual([op["id"] for op in batch], [1, 2])

    def test_collect_includes_multiple_source_directories_with_same_target(self) -> None:
        svc = _svc(_FakeOpenList(set(), set()))
        ops = [
            _op(1, "/src/a/a.mp4", "/dst/S01E01.mp4", raw_data={"staging_file": True}),
            _op(2, "/src/b/b.mp4", "/dst/S01E02.mp4", raw_data={"staging_file": True}),
        ]
        batch = svc._collect_move_file_batch(ops, 0)
        self.assertEqual([op["id"] for op in batch], [1, 2])

    def test_collect_stops_on_delete_duplicate_flag(self) -> None:
        svc = _svc(_FakeOpenList(set(), set()))
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4"),
            _op(2, "/src/b.mp4", "/dst/S01E02.mp4", raw_data={"delete_source_if_target_exists": True}),
        ]
        batch = svc._collect_move_file_batch(ops, 0)
        self.assertEqual([op["id"] for op in batch], [1])

    def test_collect_includes_fail_if_target_exists_ops(self) -> None:
        # 真实暂存任务的 move_file 常带 fail_if_target_exists=True，批量预检已覆盖该语义
        svc = _svc(_FakeOpenList(set(), set()))
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4", raw_data={"staging_file": True, "fail_if_target_exists": True}),
            _op(2, "/src/b.mp4", "/dst/S01E02.mp4", raw_data={"staging_file": True, "fail_if_target_exists": True}),
        ]
        batch = svc._collect_move_file_batch(ops, 0)
        self.assertEqual([op["id"] for op in batch], [1, 2])

    def test_batch_execute_fail_if_target_exists_conflict(self) -> None:
        # fail_if_target_exists 语义：目标在+源在 → 失败，不覆盖
        fake = _FakeOpenList(source_names={"a.mp4", "b.mp4"}, target_names={"S01E01.mp4"})
        svc = _svc(fake)
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4", raw_data={"fail_if_target_exists": True}),
            _op(2, "/src/b.mp4", "/dst/S01E02.mp4", raw_data={"fail_if_target_exists": True}),
        ]
        verdicts = svc._execute_move_file_batch(ops)
        by_id = {v[0]["id"]: v[1] for v in verdicts}
        self.assertEqual(by_id[1], "failed")  # 目标在+源在 → 失败
        self.assertEqual(by_id[2], "done")  # 正常移动

    def test_collect_stops_on_duplicate_target_name(self) -> None:
        svc = _svc(_FakeOpenList(set(), set()))
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4"),
            _op(2, "/src/b.mp4", "/dst/S01E01.mp4"),  # 同名目标，拒绝批量防错乱
            _op(3, "/src/c.mp4", "/dst/S01E02.mp4"),
        ]
        batch = svc._collect_move_file_batch(ops, 0)
        self.assertEqual([op["id"] for op in batch], [1])

    def test_collect_stops_on_case_insensitive_duplicate_target_name(self) -> None:
        svc = _svc(_FakeOpenList(set(), set()))
        ops = [
            _op(1, "/src/a.mp4", "/dst/S01E01.mp4"),
            _op(2, "/src/b.mp4", "/dst/s01e01.MP4"),
        ]
        batch = svc._collect_move_file_batch(ops, 0)
        self.assertEqual([op["id"] for op in batch], [1])

    def test_batch_move_api_failure_reconciled(self) -> None:
        class _FailingMove(_FakeOpenList):
            def move_many(self, *args: Any, **kwargs: Any) -> bool:
                return False  # 模拟批量请求失败，不落盘

        svc = _svc(_FailingMove(source_names={"a.mp4"}, target_names=set()))
        ops = [_op(1, "/src/a.mp4", "/dst/S01E01.mp4")]
        verdicts = svc._execute_move_file_batch(ops)
        # 改名成功但移动未落盘 → 对账判 failed（源仍在、目标缺）
        self.assertEqual(verdicts[0][1], "failed")
        self.assertIn("未生效", verdicts[0][2])
        self.assertIn("OpenList 批量移动返回未成功", verdicts[0][2])

    def test_source_and_target_both_missing_is_reported_unknown(self) -> None:
        class _LostMove(_FakeOpenList):
            def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
                self.move_many_calls.append((source_dir, target_dir, list(names)))
                for name in names:
                    self.source.discard(name)
                    self.source_identity.pop(name, None)
                return True

        fake = _LostMove({"a.mp4", "b.mp4"}, set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertTrue(all("状态未知" in item[2] for item in verdicts))

    def test_successful_async_move_is_polled_until_source_disappears(self) -> None:
        class _DelayedVisibleMove(_FakeOpenList):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.pending_names: list[str] = []

            def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
                self.move_many_calls.append((source_dir, target_dir, list(names)))
                self.pending_names = list(names)
                return True

            def list_dir(self, path: str, refresh: bool | None = None) -> list[_Item]:
                normalized = str(path or "").rstrip("/")
                if normalized == "/src" and self.list_calls.get(normalized, 0) >= 2 and self.pending_names:
                    super().move_many("/src", "/dst", self.pending_names)
                    self.pending_names = []
                return super().list_dir(path, refresh=refresh)

        fake = _DelayedVisibleMove({"a.mp4", "b.mp4"}, set())
        svc = _svc(fake)
        svc.organizer_config["bulk_reconcile_timeout_seconds"] = 0.03

        verdicts = svc._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])

    def test_timeout_after_server_completed_is_reconciled_done(self) -> None:
        class _TimeoutAfterMove(_FakeOpenList):
            def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
                super().move_many(source_dir, target_dir, names, **kwargs)
                raise TimeoutError("request timed out after server completed")

        fake = _TimeoutAfterMove({"a.mp4", "b.mp4"}, set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])

    def test_post_move_source_read_failure_never_reports_done(self) -> None:
        fake = _FakeOpenList(
            source_names={"a.mp4", "b.mp4"},
            target_names=set(),
            fail_list_calls={"/src": {3}},
        )
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )
        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertTrue(all("状态未知" in item[2] for item in verdicts))

    def test_post_move_target_read_failure_never_reports_done(self) -> None:
        fake = _FakeOpenList(
            source_names={"a.mp4", "b.mp4"},
            target_names=set(),
            fail_list_calls={"/dst": {2}},
        )
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )
        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertTrue(all("状态未知" in item[2] for item in verdicts))

    def test_original_and_unrelated_renamed_name_both_present_is_rejected(self) -> None:
        fake = _FakeOpenList(source_names={"a.mp4", "x.mp4", "b.mp4"}, target_names=set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )
        by_id = {item[0]["id"]: item for item in verdicts}
        self.assertEqual(by_id[1][1], "failed")
        self.assertIn("同时存在", by_id[1][2])
        self.assertEqual(fake.source_identity["a.mp4"], "a.mp4")
        self.assertEqual(fake.source_identity["x.mp4"], "x.mp4")

    def test_cross_rename_to_different_directory_preserves_file_identity(self) -> None:
        fake = _FakeOpenList(source_names={"a.mp4", "b.mp4"}, target_names=set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/b.mp4"), _op(2, "/src/b.mp4", "/dst/a.mp4")]
        )
        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(fake.target_identity["b.mp4"], "a.mp4")
        self.assertEqual(fake.target_identity["a.mp4"], "b.mp4")
        inverse = next(item[3] for item in verdicts if item[3])

        _svc(fake)._execute_inverse(inverse or {})

        self.assertEqual(fake.source_identity, {"a.mp4": "a.mp4", "b.mp4": "b.mp4"})
        self.assertEqual(fake.target_identity, {})

    def test_same_directory_independent_renames_do_not_call_move_many(self) -> None:
        fake = _FakeOpenList(source_names={"a.mp4", "b.mp4"}, target_names=set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/src/x.mp4"), _op(2, "/src/b.mp4", "/src/y.mp4")]
        )
        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(fake.source, {"x.mp4", "y.mp4"})
        self.assertEqual(fake.move_many_calls, [])

    def test_same_directory_cross_rename_is_rejected_before_mutation(self) -> None:
        fake = _FakeOpenList(source_names={"a.mp4", "b.mp4"}, target_names=set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/src/b.mp4"), _op(2, "/src/b.mp4", "/src/a.mp4")]
        )
        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertEqual(fake.source_identity, {"a.mp4": "a.mp4", "b.mp4": "b.mp4"})
        self.assertEqual(fake.renames, [])

    def test_same_directory_case_only_renames_use_temporary_names(self) -> None:
        fake = _FakeOpenList(source_names={"A.mp4", "B.mp4"}, target_names=set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/A.mp4", "/src/a.mp4"), _op(2, "/src/B.mp4", "/src/b.mp4")]
        )
        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(fake.source, {"a.mp4", "b.mp4"})
        self.assertEqual(fake.source_identity["a.mp4"], "A.mp4")
        self.assertEqual(fake.source_identity["b.mp4"], "B.mp4")
        self.assertEqual(len(fake.batch_rename_calls), 2)

    def test_bulk_switch_off_preserves_legacy_two_phase_renames(self) -> None:
        fake = _FakeOpenList({"a.mp4", "b.mp4"}, set())
        svc = _svc(fake)
        svc.organizer_config["bulk_operations_enabled"] = False

        verdicts = svc._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(fake.batch_rename_calls, [])
        self.assertEqual(len(fake.renames), 4)

    def test_bulk_switch_off_disables_batch_collection(self) -> None:
        svc = _svc(_FakeOpenList(set(), set()))
        svc.organizer_config["bulk_operations_enabled"] = False
        ops = [
            _op(1, "/src/a.mp4", "/dst/x.mp4"),
            _op(2, "/src/b.mp4", "/dst/y.mp4"),
        ]

        self.assertEqual(svc._collect_move_file_batch(ops, 0), [])

    def test_single_case_only_rename_and_inverse_use_temporary_name(self) -> None:
        fake = _FakeOpenList(source_names={"A.mp4"}, target_names=set())
        svc = _svc(fake)

        inverse = svc._execute_operation(_op(1, "/src/A.mp4", "/src/a.mp4"))

        self.assertEqual(fake.source, {"a.mp4"})
        self.assertEqual(
            inverse,
            {"type": "move_file", "source_path": "/src/a.mp4", "target_path": "/src/A.mp4"},
        )
        svc._execute_inverse(inverse or {})
        self.assertEqual(fake.source, {"A.mp4"})
        self.assertEqual(fake.source_identity["A.mp4"], "A.mp4")

    def test_copy_without_source_removal_is_not_reported_done(self) -> None:
        class _CopyOnlyMove(_FakeOpenList):
            def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
                self.move_many_calls.append((source_dir, target_dir, list(names)))
                for name in names:
                    if name in self.source:
                        self.target.add(name)
                        self.target_identity[name] = self.source_identity[name]
                return True

        fake = _CopyOnlyMove(source_names={"a.mp4", "b.mp4"}, target_names=set())
        verdicts = _svc(fake)._execute_move_file_batch(
            [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
        )
        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertTrue(all("同时存在" in item[2] for item in verdicts))

    def test_partial_move_reconciles_per_file_and_done_inverse_is_executable(self) -> None:
        class _PartialMove(_FakeOpenList):
            def __init__(self, *args: Any, return_value: bool, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.return_value = return_value

            def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
                super().move_many(source_dir, target_dir, names[:1], **kwargs)
                return self.return_value

        for return_value in (True, False):
            with self.subTest(return_value=return_value):
                fake = _PartialMove({"a.mp4", "b.mp4"}, set(), return_value=return_value)
                svc = _svc(fake)
                verdicts = svc._execute_move_file_batch(
                    [_op(1, "/src/a.mp4", "/dst/x.mp4"), _op(2, "/src/b.mp4", "/dst/y.mp4")]
                )
                self.assertEqual([item[1] for item in verdicts], ["done", "failed"])
                inverse = verdicts[0][3]
                self.assertEqual((inverse or {}).get("type"), "move_file")
                self.assertIsNone(verdicts[1][3])

                svc._execute_inverse(inverse or {})

                # 只回滚已确认移动的第一项；第二项仍保持失败后的改名状态，
                # 不能把“部分回滚”冒充为整个批次已恢复。
                self.assertEqual(fake.source_identity, {"a.mp4": "a.mp4", "y.mp4": "b.mp4"})
                self.assertEqual(fake.target_identity, {})

    def test_partial_cross_move_is_never_given_an_unexecutable_inverse(self) -> None:
        class _PartialCrossMove(_FakeOpenList):
            def __init__(self, *args: Any, return_value: bool, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.return_value = return_value

            def move_many(self, source_dir: str, target_dir: str, names: list[str], **kwargs: Any) -> bool:
                super().move_many(source_dir, target_dir, names[:1], **kwargs)
                return self.return_value

        for return_value in (True, False):
            with self.subTest(return_value=return_value):
                fake = _PartialCrossMove({"a.mp4", "b.mp4"}, set(), return_value=return_value)
                verdicts = _svc(fake)._execute_move_file_batch(
                    [_op(1, "/src/a.mp4", "/dst/b.mp4"), _op(2, "/src/b.mp4", "/dst/a.mp4")]
                )
                self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
                self.assertTrue(all(item[3] is None for item in verdicts))
                self.assertEqual(fake.source_identity, {"a.mp4": "b.mp4"})
                self.assertEqual(fake.target_identity, {"b.mp4": "a.mp4"})


class OrganizerRecursiveMoveTests(unittest.TestCase):
    def test_exclusive_nested_staging_tree_uses_one_recursive_move(self) -> None:
        fake = _TreeFakeOpenList(
            {
                "/stage/show/cd1/release-E01.ts",
                "/stage/show/cd2/release-E02.ts",
            }
        )
        ops = [
            _op(
                1,
                "/stage/show/cd1/release-E01.ts",
                "/dst/Show - S01E01.ts",
                raw_data={"staging_file": True},
            ),
            _op(
                2,
                "/stage/show/cd2/release-E02.ts",
                "/dst/Show - S01E02.ts",
                raw_data={"staging_file": True},
            ),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(len(fake.recursive_move_calls), 1)
        self.assertEqual(fake.move_many_calls, [])
        self.assertEqual(
            fake.recursive_move_snapshots[0],
            {"Show - S01E01.ts", "Show - S01E02.ts"},
        )
        self.assertEqual(
            set(fake.files),
            {"/dst/Show - S01E01.ts", "/dst/Show - S01E02.ts"},
        )
        inverse = next(item[3] for item in verdicts if item[3])
        _svc(fake)._execute_inverse(inverse or {})
        self.assertEqual(
            set(fake.files),
            {
                "/stage/show/cd1/release-E01.ts",
                "/stage/show/cd2/release-E02.ts",
            },
        )

    def test_duplicate_recursive_targets_are_rejected_before_mutation(self) -> None:
        fake = _TreeFakeOpenList(
            {
                "/stage/show/cd1/release-E01.ts",
                "/stage/show/cd2/release-E02.ts",
            }
        )
        ops = [
            _op(1, "/stage/show/cd1/release-E01.ts", "/dst/Show.ts", raw_data={"staging_file": True}),
            _op(2, "/stage/show/cd2/release-E02.ts", "/dst/show.TS", raw_data={"staging_file": True}),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")

        self.assertEqual([item[1] for item in verdicts], ["failed", "failed"])
        self.assertEqual(fake.recursive_move_calls, [])
        self.assertEqual(fake.move_many_calls, [])

    def test_partial_successful_rename_response_aborts_recursive_move(self) -> None:
        class _PartialSuccessfulRename(_TreeFakeOpenList):
            def batch_rename(self, source_dir: str, renames: list[tuple[str, str]], **kwargs: Any) -> bool:
                source = self._normalize(source_dir)
                self.batch_rename_calls.append((source, list(renames)))
                if renames:
                    self.rename(f"{source}/{renames[0][0]}", renames[0][1])
                return True

        original_files = {
            "/stage/show/cd1/release-E01.ts",
            "/stage/show/cd1/release-E02.ts",
            "/stage/show/cd2/release-E03.ts",
        }
        fake = _PartialSuccessfulRename(original_files)
        ops = [
            _op(1, "/stage/show/cd1/release-E01.ts", "/dst/Show - S01E01.ts", raw_data={"staging_file": True}),
            _op(2, "/stage/show/cd1/release-E02.ts", "/dst/Show - S01E02.ts", raw_data={"staging_file": True}),
            _op(3, "/stage/show/cd2/release-E03.ts", "/dst/Show - S01E03.ts", raw_data={"staging_file": True}),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")

        self.assertEqual([item[1] for item in verdicts], ["failed", "failed", "failed"])
        self.assertTrue(all("批量改名" in item[2] for item in verdicts))
        self.assertEqual(fake.recursive_move_calls, [])
        self.assertEqual(set(fake.files), original_files)

    def test_partial_recursive_move_only_rolls_back_confirmed_success(self) -> None:
        class _PartialRecursiveMove(_TreeFakeOpenList):
            def recursive_move(
                self,
                source_dir: str,
                target_dir: str,
                *,
                conflict_policy: str = "cancel",
                **kwargs: Any,
            ) -> bool:
                source = self._normalize(source_dir)
                target = self._normalize(target_dir)
                self.recursive_move_calls.append((source, target, conflict_policy))
                moving = sorted(path for path in self.files if path.startswith(source.rstrip("/") + "/"))
                source_path = moving[0]
                target_path = f"{target}/{source_path.rsplit('/', 1)[1]}"
                self.files[target_path] = self.files.pop(source_path)
                raise TimeoutError("recursive move timed out after one file")

        fake = _PartialRecursiveMove(
            {
                "/stage/show/cd1/release-E01.ts",
                "/stage/show/cd2/release-E02.ts",
            }
        )
        ops = [
            _op(1, "/stage/show/cd1/release-E01.ts", "/dst/Show - S01E01.ts", raw_data={"staging_file": True}),
            _op(2, "/stage/show/cd2/release-E02.ts", "/dst/Show - S01E02.ts", raw_data={"staging_file": True}),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")

        self.assertEqual([item[1] for item in verdicts], ["done", "failed"])
        inverse = verdicts[0][3]
        self.assertEqual((inverse or {}).get("type"), "move_file")
        self.assertIsNone(verdicts[1][3])

        _svc(fake)._execute_inverse(inverse or {})

        self.assertIn("/stage/show/cd1/release-E01.ts", fake.files)
        self.assertIn("/stage/show/cd2/Show - S01E02.ts", fake.files)
        self.assertFalse(any(path.startswith("/dst/") for path in fake.files))

    def test_unknown_file_disables_recursive_move_and_groups_by_source(self) -> None:
        fake = _TreeFakeOpenList(
            {
                "/stage/show/cd1/release-E01.ts",
                "/stage/show/cd2/release-E02.ts",
                "/stage/show/readme.txt",
            }
        )
        ops = [
            _op(
                1,
                "/stage/show/cd1/release-E01.ts",
                "/dst/Show - S01E01.ts",
                raw_data={"staging_file": True},
            ),
            _op(
                2,
                "/stage/show/cd2/release-E02.ts",
                "/dst/Show - S01E02.ts",
                raw_data={"staging_file": True},
            ),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(fake.recursive_move_calls, [])
        self.assertEqual(len(fake.move_many_calls), 2)
        self.assertIn("/stage/show/readme.txt", fake.files)
        inverse = next(item[3] for item in verdicts if item[3])
        self.assertEqual((inverse or {}).get("type"), "move_file_batch")
        self.assertTrue(all(item[3] is None for item in verdicts[1:]))

        _svc(fake)._execute_inverse(inverse or {})

        self.assertEqual(
            set(fake.files),
            {
                "/stage/show/cd1/release-E01.ts",
                "/stage/show/cd2/release-E02.ts",
                "/stage/show/readme.txt",
            },
        )

    def test_grouped_fallback_batch_inverse_vacates_dependent_names_first(self) -> None:
        original_files = {
            "/stage/show/cd1/Show - S01E02.ts",
            "/stage/show/cd2/release-E02.ts",
            "/stage/show/readme.txt",
        }
        fake = _TreeFakeOpenList(original_files)
        ops = [
            _op(
                1,
                "/stage/show/cd1/Show - S01E02.ts",
                "/dst/Show - S01E01.ts",
                raw_data={"staging_file": True},
            ),
            _op(
                2,
                "/stage/show/cd2/release-E02.ts",
                "/dst/Show - S01E02.ts",
                raw_data={"staging_file": True},
            ),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")
        inverse = next(item[3] for item in verdicts if item[3])

        _svc(fake)._execute_inverse(inverse or {})

        self.assertEqual(set(fake.files), original_files)

    def test_grouped_fallback_batch_inverse_breaks_cross_directory_name_cycle(self) -> None:
        original_files = {
            "/stage/show/cd1/Show - S01E02.ts",
            "/stage/show/cd2/Show - S01E01.ts",
            "/stage/show/readme.txt",
        }
        fake = _TreeFakeOpenList(original_files)
        ops = [
            _op(
                1,
                "/stage/show/cd1/Show - S01E02.ts",
                "/dst/Show - S01E01.ts",
                raw_data={"staging_file": True},
            ),
            _op(
                2,
                "/stage/show/cd2/Show - S01E01.ts",
                "/dst/Show - S01E02.ts",
                raw_data={"staging_file": True},
            ),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")
        inverse = next(item[3] for item in verdicts if item[3])

        _svc(fake)._execute_inverse(inverse or {})

        self.assertEqual(set(fake.files), original_files)

    def test_single_source_directory_never_uses_recursive_move(self) -> None:
        fake = _TreeFakeOpenList({"/stage/show/release-E01.ts", "/stage/show/release-E02.ts"})
        ops = [
            _op(1, "/stage/show/release-E01.ts", "/dst/Show - S01E01.ts", raw_data={"staging_file": True}),
            _op(2, "/stage/show/release-E02.ts", "/dst/Show - S01E02.ts", raw_data={"staging_file": True}),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(fake.recursive_move_calls, [])
        self.assertEqual(len(fake.move_many_calls), 1)

    def test_unsupported_recursive_endpoint_falls_back_after_rename(self) -> None:
        class _NoRecursive(_TreeFakeOpenList):
            def recursive_move(self, *args: Any, **kwargs: Any) -> bool:
                raise OpenListEndpointUnsupported("404")

        fake = _NoRecursive(
            {
                "/stage/show/cd1/release-E01.ts",
                "/stage/show/cd2/release-E02.ts",
            }
        )
        ops = [
            _op(1, "/stage/show/cd1/release-E01.ts", "/dst/Show - S01E01.ts", raw_data={"staging_file": True}),
            _op(2, "/stage/show/cd2/release-E02.ts", "/dst/Show - S01E02.ts", raw_data={"staging_file": True}),
        ]

        verdicts = _svc(fake)._execute_move_file_batch(ops, staging_root="/stage")

        self.assertEqual([item[1] for item in verdicts], ["done", "done"])
        self.assertEqual(len(fake.move_many_calls), 2)


if __name__ == "__main__":
    unittest.main()
