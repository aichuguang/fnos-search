"""校验后台“变更增量”计算在结构化字段上整块提交。

角标配置是 enabled/text 互相约束的整体：后端 normalize_result_provider_badges
在 enabled=True 而 text 缺失时直接抛错。若前端只发生变化的子键，勾选开关但文案
未改动的行会丢掉 text，保存必然失败。这里直接跑真实的 admin-settings.js，
避免用 Python 复刻一份 diff 逻辑却与前端脱节。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

SETTINGS_JS = Path("static/admin-settings.js")
ADMIN_JS = Path("static/admin.js")

BADGE_PATH = "pansou.result_provider_badges"

_HARNESS = """
globalThis.window = globalThis;
globalThis.document = { getElementById: () => null, querySelectorAll: () => [] };
require(%(module)s);
const patch = window.FnosAdminSettings.changedConfigPatch(
    %(candidate)s,
    %(baseline)s,
    { atomicPaths: %(atomic)s },
);
process.stdout.write(JSON.stringify(patch));
"""


def _changed_config_patch(
    candidate: dict,
    baseline: dict,
    atomic_paths: list[str] | None = None,
) -> dict:
    script = _HARNESS % {
        "module": json.dumps(str(SETTINGS_JS.resolve()).replace("\\", "/")),
        "candidate": json.dumps(candidate, ensure_ascii=False),
        "baseline": json.dumps(baseline, ensure_ascii=False),
        "atomic": json.dumps(atomic_paths or []),
    }
    completed = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(completed.stdout)


@unittest.skipIf(shutil.which("node") is None, "node 不可用，跳过前端补丁逻辑校验")
class AdminConfigPatchTests(unittest.TestCase):
    def _badges(self, **overrides: dict) -> dict:
        badges = {
            "quark": {"enabled": False, "text": ""},
            "mobile": {"enabled": False, "text": "秒入库"},
            "tianyi": {"enabled": False, "text": ""},
            "magnet": {"enabled": False, "text": "快速入库"},
        }
        badges.update(overrides)
        return {"pansou": {"result_provider_badges": badges}}

    def test_atomic_path_keeps_unchanged_sibling_fields(self) -> None:
        baseline = self._badges()
        candidate = self._badges(mobile={"enabled": True, "text": "秒入库"})

        patch = _changed_config_patch(candidate, baseline, [BADGE_PATH])

        badges = patch["pansou"]["result_provider_badges"]
        # 只有 enabled 变了，但 text 必须一起送出，否则后端按缺省值校验会拒绝保存。
        self.assertEqual(badges["mobile"], {"enabled": True, "text": "秒入库"})
        self.assertEqual(sorted(badges), ["magnet", "mobile", "quark", "tianyi"])
        for provider in ("quark", "tianyi", "magnet"):
            self.assertIn("text", badges[provider], provider)
            self.assertIn("enabled", badges[provider], provider)

    def test_unchanged_atomic_value_is_not_sent(self) -> None:
        baseline = self._badges()
        patch = _changed_config_patch(self._badges(), baseline, [BADGE_PATH])
        self.assertEqual(patch, {})

    def test_non_atomic_paths_still_send_only_changed_leaves(self) -> None:
        patch = _changed_config_patch(
            {"pansou": {"timeout": 45, "base_url": "http://same"}},
            {"pansou": {"timeout": 20, "base_url": "http://same"}},
            [BADGE_PATH],
        )
        self.assertEqual(patch, {"pansou": {"timeout": 45}})

    def test_admin_js_marks_badge_map_as_atomic(self) -> None:
        source = ADMIN_JS.read_text(encoding="utf-8")
        block = re.search(
            r"const ADVANCED_ATOMIC_CONFIG_PATHS = Object\.freeze\(\[(.*?)\]\)",
            source,
            re.S,
        )
        self.assertIsNotNone(block, "缺少 ADVANCED_ATOMIC_CONFIG_PATHS 声明")
        self.assertIn(BADGE_PATH, block.group(1))
        # 两个保存入口（单独保存 / 全量保存事务）都必须带上整块提交路径。
        self.assertEqual(source.count("atomicPaths: ADVANCED_ATOMIC_CONFIG_PATHS"), 2)


if __name__ == "__main__":
    unittest.main()
