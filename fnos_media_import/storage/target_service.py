"""V1.0 存储目标服务：逻辑目标、分类三定位器、不可变修订与路径安全校验。

``StorageTargetService`` 只管理目标的配置身份（destination_id、分类路径、
展示信息与修订）。它不保存任何 Provider 凭证：目标修订只持久化 locator、
分类映射与展示信息（§8.4），凭证属于账号域。

安全边界（§8.5 / §3.1）：

- ``destination_id`` 只能是 ``[a-z0-9][a-z0-9_-]*``。
- 本地目标只允许落在 ``/storage-targets`` 受控根内，通过
  ``resolve_local_target`` 做 realpath 级包含校验，拒绝 ``..``/绝对路径/控制字符。
- 每个分类的最终路径非空且不是挂载根，除非显式 ``allow_mount_root`` 高风险确认。
- ``openlist_generic`` 目标必须通过 rclone/OpenList 定位器引用一个已存在挂载。
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from ..domain.models import (
    LocalLocator,
    NativeLocator,
    OpenListLocator,
    RcloneLocator,
    StorageTarget,
    StorageTargetBinding,
    TargetCategory,
)
from ..time_utils import utc_now_iso
from ..storage_paths import normalize_storage_path as normalize_path
from ..storage_paths import resolve_local_target

_DESTINATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ALLOWED_TARGET_KINDS = frozenset({"provider", "local", "openlist_generic"})
_ALLOWED_AUDIENCES = frozenset({"admin", "guest"})


class TargetError(Exception):
    """存储目标领域错误基类。"""


class TargetValidationError(TargetError, ValueError):
    """目标配置不满足安全或结构约束。"""


class TargetNotFoundError(TargetError):
    """目标不存在。"""


class TargetRevisionNotFoundError(TargetError):
    """目标修订不存在。"""


class TargetConflictError(TargetError):
    """目标在编辑期间被其他管理员修订。"""

    code = "TARGET_CONFLICT"
    status = 409


class TargetInUseError(TargetError):
    """目标仍被任务、订阅或规则引用，不能物理删除。"""


# ---------------------------------------------------------------------------
# locator / categories 序列化
# ---------------------------------------------------------------------------


def _encode_locator(locator: Any) -> dict[str, Any] | None:
    if locator is None:
        return None
    if isinstance(locator, NativeLocator):
        return {"kind": "native", "locator_kind": locator.kind, "value": locator.value}
    if isinstance(locator, RcloneLocator):
        return {
            "kind": "rclone",
            "connection_id": locator.connection_id,
            "mount_id": locator.mount_id,
            "relative_path": locator.relative_path,
        }
    if isinstance(locator, LocalLocator):
        return {"kind": "local", "root": locator.root, "relative_path": locator.relative_path}
    if isinstance(locator, OpenListLocator):
        return {
            "kind": "openlist",
            "connection_id": locator.connection_id,
            "mount_id": locator.mount_id,
            "path": locator.path,
        }
    raise TargetValidationError(f"未知 locator 类型：{type(locator)!r}")


def _decode_locator(raw: Any) -> Any:
    if not raw:
        return None
    kind = str(raw.get("kind") or "")
    if kind == "native":
        return NativeLocator(kind=str(raw.get("locator_kind") or "none"), value=str(raw.get("value") or ""))
    if kind == "rclone":
        return RcloneLocator(
            connection_id=str(raw.get("connection_id") or ""),
            mount_id=str(raw.get("mount_id") or ""),
            relative_path=str(raw.get("relative_path") or ""),
        )
    if kind == "local":
        return LocalLocator(root=str(raw.get("root") or ""), relative_path=str(raw.get("relative_path") or ""))
    if kind == "openlist":
        return OpenListLocator(
            connection_id=str(raw.get("connection_id") or ""),
            mount_id=str(raw.get("mount_id") or ""),
            path=str(raw.get("path") or ""),
        )
    raise TargetValidationError(f"未知 locator kind：{kind!r}")


def encode_categories(categories: dict[str, TargetCategory]) -> dict[str, Any]:
    """把 ``TargetCategory`` 字典编码为可持久化 JSON 字典。"""
    result: dict[str, Any] = {}
    for key, cat in categories.items():
        result[key] = {
            "public_label": cat.public_label,
            "native_locator": _encode_locator(cat.native_locator),
            "rclone_locator": _encode_locator(cat.rclone_locator),
            "organizer_locator": _encode_locator(cat.organizer_locator),
        }
    return result


def decode_categories(payload: Any) -> dict[str, TargetCategory]:
    """把 API/JSON 载荷解析为 ``TargetCategory`` 字典。"""
    categories: dict[str, TargetCategory] = {}
    for key, raw in (payload or {}).items():
        raw = raw or {}
        categories[str(key)] = TargetCategory(
            category_key=str(key),
            public_label=str(raw.get("public_label") or str(key)),
            native_locator=_decode_locator(raw.get("native_locator")),
            rclone_locator=_decode_locator(raw.get("rclone_locator")),
            organizer_locator=_decode_locator(raw.get("organizer_locator")),
        )
    return categories


def target_to_dict(target: StorageTarget, *, include_categories: bool = True) -> dict[str, Any]:
    """把目标输出为管理 API 字典。locator 不含任何凭证字段。"""
    result: dict[str, Any] = {
        "destination_id": target.destination_id,
        "name": target.name,
        "provider_kind": target.provider_kind,
        "account_scope": target.account_scope,
        "target_kind": target.target_kind,
        "enabled": target.enabled,
        "audience": target.audience,
        "current_revision": target.current_revision,
    }
    if include_categories:
        result["categories"] = encode_categories(target.categories)
    return result


def _category_primary_path(category: TargetCategory) -> str:
    """返回分类的最终路径（规范化后）；无路径时返回空字符串。"""
    loc = category.rclone_locator
    if isinstance(loc, RcloneLocator):
        return normalize_path(loc.relative_path)
    if isinstance(loc, LocalLocator):
        return normalize_path(loc.relative_path)
    if isinstance(loc, OpenListLocator):
        return normalize_path(loc.path)
    native = category.native_locator
    if native is not None and native.kind != "none":
        return normalize_path(native.value)
    if isinstance(category.organizer_locator, OpenListLocator):
        return normalize_path(category.organizer_locator.path)
    return ""


class StorageTargetService:
    """存储目标 CRUD、不可变修订、路径安全与 openlist_generic 挂载校验。"""

    def __init__(self, database: Any, openlist_dir_lister: Any = None):
        self.database = database
        # 可选：(mount_id, path) -> list[str] 目录列举回调，供后台目录选择器使用。
        self.openlist_dir_lister = openlist_dir_lister

    # ------------------------------------------------------------------ CRUD

    def create_target(
        self,
        *,
        destination_id: str,
        name: str,
        provider_kind: str,
        target_kind: str = "provider",
        account_scope: str | None = None,
        audience: str = "admin",
        categories: dict[str, TargetCategory] | None = None,
        allow_mount_root: bool = False,
        account_id: str | None = None,
        binding_locator: dict[str, Any] | None = None,
    ) -> StorageTarget:
        destination_id = str(destination_id or "").strip()
        name = str(name or "").strip()
        provider_kind = str(provider_kind or "").strip()
        target_kind = str(target_kind or "provider").strip()
        audience = str(audience or "admin").strip()

        if not _DESTINATION_ID_RE.fullmatch(destination_id):
            raise TargetValidationError(
                "destination_id 只能包含小写字母、数字、下划线和连字符，且以字母或数字开头"
            )
        if target_kind not in _ALLOWED_TARGET_KINDS:
            raise TargetValidationError(f"target_kind 必须是 {'/'.join(sorted(_ALLOWED_TARGET_KINDS))}")
        if audience not in _ALLOWED_AUDIENCES:
            raise TargetValidationError("audience 必须是 admin 或 guest")
        if not name:
            raise TargetValidationError("目标名称不能为空")
        if not provider_kind:
            raise TargetValidationError("provider_kind 不能为空")
        if target_kind == "provider" and account_scope is None:
            account_scope = "default"

        categories = categories or {}
        if not categories:
            raise TargetValidationError("目标至少需要一个分类")
        errors = self._validate_categories(
            categories,
            target_kind=target_kind,
            provider_kind=provider_kind,
            allow_mount_root=allow_mount_root,
        )
        if errors:
            raise TargetValidationError("；".join(errors))

        now = utc_now_iso()
        with self.database.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM storage_targets WHERE destination_id = ?", (destination_id,)
            ).fetchone()
            if exists:
                raise TargetValidationError(f"目标已存在：{destination_id}")
            conn.execute(
                "INSERT INTO storage_targets "
                "(destination_id, name, provider_kind, account_scope, target_kind, enabled, audience, "
                " current_revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, 1, ?, ?)",
                (destination_id, name, provider_kind, account_scope, target_kind, audience, now, now),
            )
            self._insert_revision(
                conn,
                destination_id,
                1,
                categories,
                display={"name": name, "audience": audience},
                now=now,
            )
            self._insert_binding(
                conn,
                destination_id=destination_id,
                target_revision=1,
                provider_kind=provider_kind,
                account_id=account_id,
                locator=binding_locator or {},
                now=now,
            )
        target = self.get_target(destination_id)
        if target is None:  # pragma: no cover - 防御
            raise TargetValidationError(f"创建失败：{destination_id}")
        return target

    def revise_target(
        self,
        destination_id: str,
        *,
        name: str | None = None,
        audience: str | None = None,
        categories: dict[str, TargetCategory] | None = None,
        allow_mount_root: bool = False,
        account_id: str | None = None,
        binding_locator: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> StorageTarget:
        target = self.get_target(destination_id)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        if expected_revision not in (None, ""):
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise TargetValidationError("expected_revision 必须是正整数") from exc
            if expected <= 0 or expected != int(target.current_revision):
                raise TargetConflictError(
                    "目标已被其他管理员修改，请刷新后重试："
                    f"expected={expected}, actual={target.current_revision}"
                )
        if categories is None and name is None and audience is None:
            raise TargetValidationError("没有要修订的内容")

        errors: list[str] = []
        if categories is not None:
            if not categories:
                errors.append("目标至少需要一个分类")
            errors.extend(
                self._validate_categories(
                    categories,
                    target_kind=target.target_kind,
                    provider_kind=target.provider_kind,
                    allow_mount_root=allow_mount_root,
                )
            )
        if name is not None and not str(name).strip():
            errors.append("目标名称不能为空")
        if audience is not None and str(audience) not in _ALLOWED_AUDIENCES:
            errors.append("audience 必须是 admin 或 guest")
        if errors:
            raise TargetValidationError("；".join(errors))

        new_name = name if name is not None else target.name
        new_audience = audience if audience is not None else target.audience
        new_categories = categories if categories is not None else target.categories
        now = utc_now_iso()
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT current_revision FROM storage_targets WHERE destination_id=?",
                (destination_id,),
            ).fetchone()
            if current_row is None:
                raise TargetNotFoundError(f"目标不存在：{destination_id}")
            actual_revision = int(current_row["current_revision"] or 0)
            if expected_revision not in (None, "") and actual_revision != int(expected_revision):
                raise TargetConflictError(
                    "目标已被其他管理员修改，请刷新后重试："
                    f"expected={expected_revision}, actual={actual_revision}"
                )
            if actual_revision != int(target.current_revision):
                raise TargetConflictError("目标在编辑期间被其他管理员修改，请刷新后重试")
            new_revision = actual_revision + 1
            self._insert_revision(
                conn,
                destination_id,
                new_revision,
                new_categories,
                display={"name": new_name, "audience": new_audience},
                now=now,
            )
            conn.execute(
                "UPDATE storage_targets SET name = ?, audience = ?, current_revision = ?, updated_at = ? "
                "WHERE destination_id = ? AND current_revision = ?",
                (new_name, new_audience, new_revision, now, destination_id, actual_revision),
            )
            if int(conn.execute("SELECT changes()").fetchone()[0] or 0) != 1:
                raise TargetConflictError("目标在编辑期间被其他管理员修改，请刷新后重试")
            previous_binding = self._binding_row(conn, destination_id, target.current_revision)
            effective_account_id = account_id if account_id is not None else (
                str(previous_binding["account_id"]) if previous_binding and previous_binding["account_id"] else None
            )
            effective_locator = binding_locator
            if effective_locator is None and previous_binding is not None:
                previous_revision = conn.execute(
                    "SELECT locator_json FROM storage_target_binding_revisions "
                    "WHERE binding_id=? AND revision=?",
                    (previous_binding["binding_id"], previous_binding["current_revision"]),
                ).fetchone()
                effective_locator = json.loads(previous_revision["locator_json"] or "{}") if previous_revision else {}
            self._insert_binding(
                conn,
                destination_id=destination_id,
                target_revision=new_revision,
                provider_kind=target.provider_kind,
                account_id=effective_account_id,
                locator=effective_locator or {},
                now=now,
            )
        updated = self.get_target(destination_id)
        if updated is None:  # pragma: no cover - 防御
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        return updated

    def update_target_meta(
        self,
        destination_id: str,
        *,
        enabled: bool | None = None,
        account_scope: str | None = None,
    ) -> StorageTarget:
        """更新运行期字段（启用状态 / 账号作用域），不产生新修订。"""
        target = self.get_target(destination_id)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        if enabled is None and account_scope is None:
            raise TargetValidationError("没有要更新的内容")
        now = utc_now_iso()
        with self.database.connect() as conn:
            if enabled is not None:
                conn.execute(
                    "UPDATE storage_targets SET enabled = ?, updated_at = ? WHERE destination_id = ?",
                    (1 if enabled else 0, now, destination_id),
                )
            if account_scope is not None:
                conn.execute(
                    "UPDATE storage_targets SET account_scope = ?, updated_at = ? WHERE destination_id = ?",
                    (account_scope, now, destination_id),
                )
        updated = self.get_target(destination_id)
        if updated is None:  # pragma: no cover - 防御
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        return updated

    def disable_target(self, destination_id: str) -> StorageTarget:
        target = self.get_target(destination_id)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        if not target.enabled:
            return target
        now = utc_now_iso()
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE storage_targets SET enabled = 0, updated_at = ? WHERE destination_id = ?",
                (now, destination_id),
            )
        updated = self.get_target(destination_id)
        if updated is None:  # pragma: no cover - 防御
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        return updated

    def delete_target(self, destination_id: str) -> bool:
        """物理删除目标。被任务、规则引用的目标拒绝删除（§16.3 TARGET_IN_USE）。"""
        target = self.get_target(destination_id)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        with self.database.connect() as conn:
            referenced = conn.execute(
                "SELECT 1 FROM source_destination_assignments WHERE destination_id = ? LIMIT 1",
                (destination_id,),
            ).fetchone()
            if referenced:
                raise TargetInUseError(f"目标被入库规则引用，不能删除：{destination_id}；请先禁用或删除相关规则")
            job = conn.execute(
                "SELECT 1 FROM import_jobs WHERE destination_id = ? LIMIT 1",
                (destination_id,),
            ).fetchone()
            if job:
                raise TargetInUseError(f"目标被入库任务引用，不能删除：{destination_id}")
            binding_rows = conn.execute(
                "SELECT binding_id FROM storage_target_account_bindings WHERE destination_id=?",
                (destination_id,),
            ).fetchall()
            for binding in binding_rows:
                conn.execute("DELETE FROM storage_target_binding_revisions WHERE binding_id=?", (binding["binding_id"],))
            conn.execute("DELETE FROM storage_target_account_bindings WHERE destination_id=?", (destination_id,))
            conn.execute("DELETE FROM storage_target_revisions WHERE destination_id = ?", (destination_id,))
            conn.execute("DELETE FROM storage_targets WHERE destination_id = ?", (destination_id,))
        return True

    # ------------------------------------------------------------- 查询/解析

    def get_target(self, destination_id: str, revision: int | None = None) -> StorageTarget | None:
        """读取目标。``revision`` 缺省时使用当前修订。"""
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM storage_targets WHERE destination_id = ?", (destination_id,)
            ).fetchone()
            if row is None:
                return None
            rev = revision if revision is not None else int(row["current_revision"])
            rev_row = conn.execute(
                "SELECT * FROM storage_target_revisions WHERE destination_id = ? AND revision = ?",
                (destination_id, rev),
            ).fetchone()
            if rev_row is None:
                raise TargetRevisionNotFoundError(f"目标修订不存在：{destination_id}@{rev}")
            return self._row_to_target(row, rev_row)

    def resolve_target(self, destination_id: str, revision: int | None = None) -> StorageTarget:
        """解析目标；不存在时抛出 ``TargetNotFoundError``。"""
        target = self.get_target(destination_id, revision=revision)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        return target

    def list_targets(self) -> list[StorageTarget]:
        targets: list[StorageTarget] = []
        with self.database.connect() as conn:
            rows = conn.execute("SELECT * FROM storage_targets ORDER BY destination_id").fetchall()
            for row in rows:
                rev_row = conn.execute(
                    "SELECT * FROM storage_target_revisions WHERE destination_id = ? AND revision = ?",
                    (row["destination_id"], row["current_revision"]),
                ).fetchone()
                if rev_row is None:
                    continue
                targets.append(self._row_to_target(row, rev_row))
        return targets

    def list_revisions(self, destination_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT revision, created_at FROM storage_target_revisions "
                "WHERE destination_id = ? ORDER BY revision",
                (destination_id,),
            ).fetchall()
        return [{"revision": int(r["revision"]), "created_at": r["created_at"]} for r in rows]

    def list_bindings(
        self, destination_id: str, target_revision: int | None = None
    ) -> list[StorageTargetBinding]:
        target = self.get_target(destination_id, target_revision)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_target_account_bindings "
                "WHERE destination_id=? AND target_revision=? ORDER BY binding_id",
                (destination_id, target.current_revision),
            ).fetchall()
            return [self._decode_binding(connection, row) for row in rows]

    def resolve_binding(
        self, destination_id: str, target_revision: int
    ) -> StorageTargetBinding:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_target_account_bindings "
                "WHERE destination_id=? AND target_revision=? AND enabled=1",
                (destination_id, int(target_revision)),
            ).fetchall()
            if len(rows) != 1:
                raise TargetValidationError(
                    f"目标 {destination_id}@{target_revision} 必须有且只有一个启用的账号 binding"
                )
            return self._decode_binding(connection, rows[0])

    def get_binding(
        self, binding_id: str, binding_revision: int | None = None
    ) -> StorageTargetBinding | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM storage_target_account_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                return None
            requested = int(binding_revision or row["current_revision"])
            revision = connection.execute(
                "SELECT * FROM storage_target_binding_revisions "
                "WHERE binding_id=? AND revision=?",
                (binding_id, requested),
            ).fetchone()
            if revision is None:
                return None
            copied = dict(row)
            copied["current_revision"] = requested
            return self._decode_binding(connection, copied)

    def test_target(self, destination_id: str) -> dict[str, Any]:
        """按分类做结构检测：最终路径、三定位器、挂载引用与本地包含。"""
        target = self.get_target(destination_id)
        if target is None:
            raise TargetNotFoundError(f"目标不存在：{destination_id}")
        categories: dict[str, Any] = {}
        all_errors: list[str] = []
        for key, cat in target.categories.items():
            cat_errors = self._validate_category(
                key,
                cat,
                target_kind=target.target_kind,
                provider_kind=target.provider_kind,
                allow_mount_root=False,
            )
            categories[key] = {
                "ok": not cat_errors,
                "errors": cat_errors,
                "final_path": _category_primary_path(cat) or None,
                "has_native": cat.native_locator is not None,
                "has_rclone": cat.rclone_locator is not None,
                "has_organizer": cat.organizer_locator is not None,
            }
            all_errors.extend(cat_errors)
        return {
            "destination_id": destination_id,
            "ok": not all_errors,
            "enabled": target.enabled,
            "errors": all_errors,
            "categories": categories,
        }

    def list_mount_directories(self, mount_id: str, path: str = "") -> list[str]:
        """后台目录选择器：把目录列举委托给注入的 lister。"""
        if self.openlist_dir_lister is None:
            raise TargetValidationError("未配置 OpenList 目录选择器")
        return self.openlist_dir_lister(mount_id, path)

    # ------------------------------------------------------------- 内部实现

    def _insert_revision(
        self,
        conn: Any,
        destination_id: str,
        revision: int,
        categories: dict[str, TargetCategory],
        *,
        display: dict[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO storage_target_revisions "
            "(destination_id, revision, categories_json, display_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                destination_id,
                revision,
                json.dumps(encode_categories(categories), ensure_ascii=False),
                json.dumps(display, ensure_ascii=False),
                now,
            ),
        )

    def _insert_binding(
        self,
        conn: Any,
        *,
        destination_id: str,
        target_revision: int,
        provider_kind: str,
        account_id: str | None,
        locator: dict[str, Any],
        now: str,
    ) -> None:
        account_revision: int | None = None
        if provider_kind not in {"generic", "local"}:
            if not account_id:
                raise TargetValidationError(f"{provider_kind} 目标必须绑定一个 Provider 账号")
            account = conn.execute(
                "SELECT provider_kind, enabled, current_revision, last_test_status "
                "FROM provider_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise TargetValidationError(f"绑定账号不存在：{account_id}")
            if str(account["provider_kind"]) != provider_kind:
                raise TargetValidationError("目标 Provider 与绑定账号 Provider 不一致")
            if not bool(account["enabled"]):
                raise TargetValidationError("目标不能绑定已停用账号")
            if str(account["last_test_status"] or "") != "ok":
                raise TargetValidationError("目标不能绑定检测未通过的账号")
            account_revision = int(account["current_revision"])
        elif account_id:
            raise TargetValidationError("generic/local 目标不能绑定 Provider 账号")
        revision_row = conn.execute(
            "SELECT categories_json FROM storage_target_revisions "
            "WHERE destination_id=? AND revision=?",
            (destination_id, int(target_revision)),
        ).fetchone()
        if revision_row is None:
            raise TargetValidationError("目标修订不存在，无法创建 binding")
        categories = decode_categories(json.loads(revision_row["categories_json"] or "{}"))
        for category in categories.values():
            for target_locator in (category.rclone_locator, category.organizer_locator):
                if not isinstance(target_locator, (RcloneLocator, OpenListLocator)):
                    continue
                mount = conn.execute(
                    "SELECT connection_id, provider_kind, account_id, identity_status, enabled "
                    "FROM openlist_mounts WHERE mount_id=?",
                    (target_locator.mount_id,),
                ).fetchone()
                if mount is None:
                    raise TargetValidationError(f"binding 引用的 mount 不存在：{target_locator.mount_id}")
                if str(mount["connection_id"]) != target_locator.connection_id:
                    raise TargetValidationError("binding 的 connection 与 mount 不一致")
                if not bool(mount["enabled"]) or str(mount["identity_status"]) not in {"verified", "declared"}:
                    raise TargetValidationError("binding 引用的 mount 未启用或身份未确认")
                if account_id and mount["account_id"] and str(mount["account_id"]) != account_id:
                    raise TargetValidationError("目标 binding 账号与 OpenList mount 账号不一致")
        binding_id = f"bind_{secrets.token_hex(8)}"
        conn.execute(
            "INSERT INTO storage_target_account_bindings "
            "(binding_id, destination_id, target_revision, account_id, provider_kind, enabled, "
            " current_revision, created_at, updated_at) VALUES (?,?,?,?,?,1,1,?,?)",
            (binding_id, destination_id, target_revision, account_id, provider_kind, now, now),
        )
        conn.execute(
            "INSERT INTO storage_target_binding_revisions "
            "(binding_id, revision, locator_json, account_revision, created_at) VALUES (?,1,?,?,?)",
            (binding_id, json.dumps(locator or {}, ensure_ascii=False, sort_keys=True), account_revision, now),
        )

    @staticmethod
    def _binding_row(conn: Any, destination_id: str, target_revision: int) -> Any:
        return conn.execute(
            "SELECT * FROM storage_target_account_bindings "
            "WHERE destination_id=? AND target_revision=? AND enabled=1",
            (destination_id, int(target_revision)),
        ).fetchone()

    @staticmethod
    def _decode_binding(conn: Any, row: Any) -> StorageTargetBinding:
        revision = conn.execute(
            "SELECT * FROM storage_target_binding_revisions WHERE binding_id=? AND revision=?",
            (row["binding_id"], row["current_revision"]),
        ).fetchone()
        if revision is None:
            raise TargetValidationError(f"binding 修订不存在：{row['binding_id']}")
        return StorageTargetBinding(
            binding_id=str(row["binding_id"]),
            destination_id=str(row["destination_id"]),
            target_revision=int(row["target_revision"]),
            account_id=str(row["account_id"]) if row["account_id"] else None,
            account_revision=int(revision["account_revision"]) if revision["account_revision"] else None,
            provider_kind=str(row["provider_kind"] or ""),
            locator=json.loads(revision["locator_json"] or "{}"),
            enabled=bool(row["enabled"]),
            current_revision=int(row["current_revision"]),
        )

    def _row_to_target(self, row: Any, rev_row: Any) -> StorageTarget:
        try:
            display = json.loads(rev_row["display_json"] or "{}")
        except json.JSONDecodeError:
            display = {}
        try:
            categories = decode_categories(json.loads(rev_row["categories_json"] or "{}"))
        except json.JSONDecodeError:
            categories = {}
        return StorageTarget(
            destination_id=row["destination_id"],
            name=str(display.get("name") or row["name"] or row["destination_id"]),
            provider_kind=row["provider_kind"],
            account_scope=row["account_scope"],
            target_kind=row["target_kind"],
            enabled=bool(row["enabled"]),
            audience=str(display.get("audience") or row["audience"] or "admin"),
            current_revision=int(rev_row["revision"]),
            categories=categories,
        )

    def _validate_categories(
        self,
        categories: dict[str, TargetCategory],
        *,
        target_kind: str,
        provider_kind: str,
        allow_mount_root: bool,
    ) -> list[str]:
        errors: list[str] = []
        for key, cat in categories.items():
            errors.extend(
                self._validate_category(
                    key,
                    cat,
                    target_kind=target_kind,
                    provider_kind=provider_kind,
                    allow_mount_root=allow_mount_root,
                )
            )
        return errors

    def _validate_category(
        self,
        category_key: str,
        category: TargetCategory,
        *,
        target_kind: str,
        provider_kind: str,
        allow_mount_root: bool,
    ) -> list[str]:
        errors: list[str] = []
        label = str(category.public_label or "").strip()
        if not label:
            errors.append(f"分类 {category_key} 缺少 public_label")

        has_executable = (
            category.native_locator is not None
            or category.rclone_locator is not None
            or (target_kind == "openlist_generic" and category.organizer_locator is not None)
        )
        if not has_executable:
            errors.append(f"分类 {category_key} 必须配置可执行的 native 或 rclone 定位器")

        # 引用的 OpenList 挂载必须真实存在。
        for loc in (category.rclone_locator, category.organizer_locator):
            if isinstance(loc, (RcloneLocator, OpenListLocator)):
                if not loc.connection_id or not loc.mount_id:
                    errors.append(f"分类 {category_key} 的 OpenList 定位器缺少 connection_id 或 mount_id")
                else:
                    mount_error = self._validate_mount_locator(loc, provider_kind, target_kind)
                    if mount_error:
                        errors.append(f"分类 {category_key}：{mount_error}")

        final_path = _category_primary_path(category)
        if not final_path:
            if not allow_mount_root:
                errors.append(
                    f"分类 {category_key} 的最终路径不能为空（挂载根目录写入需高风险确认）"
                )
        else:
            if final_path in {".", ".."} or any(
                segment in {"", ".", ".."} for segment in final_path.split("/")
            ):
                errors.append(f"分类 {category_key} 的最终路径不合法：{final_path}")

        # 本地目标必须落在受控根内（拒绝 .. 越界、不受控绝对路径、控制字符）。
        if isinstance(category.rclone_locator, LocalLocator):
            root = str(category.rclone_locator.root or "")
            relative = normalize_path(category.rclone_locator.relative_path)
            probe = relative if relative else "_probe"
            try:
                resolve_local_target(root, probe)
            except ValueError as exc:
                errors.append(f"分类 {category_key} 的本地路径不合法：{exc}")

        # openlist_generic 必须引用现有挂载。
        if target_kind == "openlist_generic":
            has_mount_locator = isinstance(category.rclone_locator, RcloneLocator) or isinstance(
                category.organizer_locator, OpenListLocator
            )
            if not has_mount_locator:
                errors.append(
                    f"分类 {category_key} 的通用 OpenList 目标必须通过 rclone 或 OpenList 定位器引用现有挂载"
                )
        return errors

    def _mount_exists(self, mount_id: str) -> bool:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM openlist_mounts WHERE mount_id = ?", (mount_id,)
            ).fetchone()
        return row is not None

    def _validate_mount_locator(self, locator: Any, provider_kind: str, target_kind: str) -> str:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT connection_id, provider_kind, enabled, identity_status FROM openlist_mounts WHERE mount_id=?",
                (locator.mount_id,),
            ).fetchone()
        if row is None:
            return f"引用不存在的挂载：{locator.mount_id}"
        if str(row["connection_id"] or "") != str(locator.connection_id or ""):
            return "OpenList connection_id 与 mount_id 不属于同一连接"
        if not row["enabled"] or str(row["identity_status"] or "") not in {"verified", "declared"}:
            return "OpenList mount 未启用或身份未确认"
        mount_provider = str(row["provider_kind"] or "")
        if target_kind == "openlist_generic" and mount_provider == "mobile":
            return "移动云 mount 不允许包装成 generic 目标，跨盘写入必须走移动 API"
        if provider_kind not in {"generic", "local"} and mount_provider and mount_provider != provider_kind:
            return f"mount Provider {mount_provider} 与目标 Provider {provider_kind} 不一致"
        return ""
