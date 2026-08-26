"""``execution_plan_ref`` 哈希语义必须与 import_execution_plans 落库口径一致。

V1 里存在两种哈希：

* ``compute_plan_hash``：计划自身的完整性哈希，写入 ``integrity.plan_hash``，
  因此计算前必须剥离该字段（哈希不能包含自身）。
* ``compute_persisted_plan_hash``：落库口径，对整个 ``plan_json`` 字符串求哈希，
  ``integrity.plan_hash`` 包含在内。``import_execution_plans.plan_hash``、
  ``execution_plan_ref.plan_hash`` 都是这一口径。

两者天然不同。任何 ``execution_plan_ref`` 交叉校验若误用前者，都会把合法任务
判成非 V1 并转人工审核（本用例即为该回归）。
"""

from __future__ import annotations

import hashlib
import json
import unittest

from fnos_media_import.planning.job_plan import (
    JobPlanError,
    compute_persisted_plan_hash,
    compute_plan_hash,
    validate_plan_integrity,
    validated_v1_execution_plan_from_job,
)
from fnos_media_import.services.import_job_service import _canonical_plan_hash
from fnos_media_import.transfer.execution_repository import _json as _execution_plan_json
from tests._rclone_v1_fixtures import rclone_v1_job, rclone_v1_plan

MSG_HASH_DEFINITIONS_DIVERGED = "存储口径哈希与写入方实现不一致"
MSG_ENCODER_DIVERGED = "存储口径编码与 execution_repository 不一致"
MSG_SEMANTICS_COLLAPSED = "完整性哈希与存储口径哈希不应相同"


class ExecutionPlanRefHashContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = rclone_v1_plan(job_id=4201)

    def test_persisted_hash_matches_import_job_service_writer(self) -> None:
        """写入 execution_plan_ref 的实现与读取侧交叉校验必须同口径。"""

        self.assertEqual(
            compute_persisted_plan_hash(self.plan),
            _canonical_plan_hash(self.plan),
            MSG_HASH_DEFINITIONS_DIVERGED,
        )

    def test_persisted_hash_matches_execution_repository_encoder(self) -> None:
        """execution_repository 直接对 plan_json 字符串求哈希，编码必须一致。"""

        encoded = _execution_plan_json(self.plan)
        self.assertEqual(
            compute_persisted_plan_hash(self.plan),
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            MSG_ENCODER_DIVERGED,
        )
        # 落库字符串必须能原样解析回同一计划，否则哈希口径无意义。
        self.assertEqual(json.loads(encoded), self.plan)

    def test_integrity_hash_and_persisted_hash_are_distinct(self) -> None:
        """两种口径本就不同；若相同说明有人误改了 integrity 剥离逻辑。"""

        self.assertNotEqual(
            compute_plan_hash(self.plan),
            compute_persisted_plan_hash(self.plan),
            MSG_SEMANTICS_COLLAPSED,
        )
        self.assertEqual(validate_plan_integrity(self.plan), [])
        self.assertEqual(
            str(self.plan["integrity"]["plan_hash"]),
            compute_plan_hash(self.plan),
        )

    def test_job_with_writer_hash_passes_cross_check(self) -> None:
        """按写入口径生成 ref 的任务必须直接通过 V1 校验，不得转人工。"""

        job = rclone_v1_job(
            self.plan,
            raw_data={
                "execution_plan_ref": {
                    "schema_version": 1,
                    "plan_hash": _canonical_plan_hash(self.plan),
                    "strategy": str(self.plan["transfer"]["strategy"]),
                }
            },
        )
        self.assertEqual(validated_v1_execution_plan_from_job(job), self.plan)

    def test_job_with_tampered_ref_hash_still_rejected(self) -> None:
        """交叉校验仍必须失败关闭：ref 与计划不符时拒绝执行。"""

        job = rclone_v1_job(
            self.plan,
            raw_data={
                "execution_plan_ref": {
                    "schema_version": 1,
                    "plan_hash": "0" * 64,
                    "strategy": str(self.plan["transfer"]["strategy"]),
                }
            },
        )
        with self.assertRaisesRegex(JobPlanError, "execution_plan_ref"):
            validated_v1_execution_plan_from_job(job)

    def test_integrity_hash_in_ref_is_rejected(self) -> None:
        """明确锁定回归：把 integrity 口径哈希写进 ref 属于不合法引用。"""

        job = rclone_v1_job(
            self.plan,
            raw_data={
                "execution_plan_ref": {
                    "schema_version": 1,
                    "plan_hash": compute_plan_hash(self.plan),
                    "strategy": str(self.plan["transfer"]["strategy"]),
                }
            },
        )
        with self.assertRaisesRegex(JobPlanError, "execution_plan_ref"):
            validated_v1_execution_plan_from_job(job)


if __name__ == "__main__":
    unittest.main()
