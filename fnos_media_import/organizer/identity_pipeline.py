"""Identity decisions and write preconditions shared by scan, edit and apply.

Nothing in planning writes to OpenList. The portable manifest is installed by
the existing journaled operation runner, after its library lock is acquired.
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
from typing import Any

from ..media_path_rules import sanitize_resource_dir_name
from .identity import (
    IDENTITY_MANIFEST_NAME,
    Identity,
    IdentityError,
    build_identity_manifest,
    format_dir_name,
    identity_from_mapping,
    identity_from_tmdb,
    make_local_identity,
    parse_dir_name,
    parse_identity_manifest,
    serialize_identity_manifest,
)
from .library_scan import resolve_target_dir, scan_library
from .openlist_client import OpenListError, VIDEO_EXTENSIONS
from .parser import is_container_dir


class IdentityReviewRequired(OpenListError):
    pass


def safe_identity_title(value: Any) -> str:
    return sanitize_resource_dir_name(unicodedata.normalize("NFKC", str(value or "")), fallback="")


def job_request(job: dict[str, Any] | None) -> dict[str, Any]:
    raw = (job or {}).get("raw_data")
    request = raw.get("request") if isinstance(raw, dict) else None
    return request if isinstance(request, dict) else {}


def request_identity(request: dict[str, Any]) -> Identity | None:
    value = request.get("media_identity")
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise IdentityReviewRequired("提交的媒体身份必须是对象")
    ns = value.get("ns") or ("tmdb" if value.get("tmdb_id") else "")
    identifier = value.get("value") or value.get("tmdb_id")
    if not ns or not identifier:
        raise IdentityReviewRequired("提交的媒体身份缺少有效命名空间或 ID")
    return Identity(value.get("media_type"), ns, identifier, safe_identity_title(value.get("title")), value.get("year"))


def plan_identity(
    client: Any, *, root: str, media_type: str, title: str, year: Any,
    tmdb_item: dict[str, Any] | None, request: dict[str, Any],
) -> dict[str, Any]:
    """Return a serializable, read-only identity decision for mapping raw_data."""
    requested = request_identity(request)
    title = safe_identity_title(title)
    if not title or (is_container_dir(title) and not tmdb_item and not (requested and requested.ns != "local")):
        raise IdentityReviewRequired("缺少可信作品标题，不能生成身份目录")
    if tmdb_item:
        hit = {**tmdb_item, "title": safe_identity_title(tmdb_item.get("title"))}
        identity = identity_from_tmdb(hit, expected_media_type=media_type, expected_year=year)
    elif requested:
        identity = requested
    else:
        identity = make_local_identity(title, year, media_type)
    if identity.media_type != media_type:
        raise IdentityReviewRequired("作品身份类型与目标媒体库不一致")
    index = scan_library(client, root, media_type=media_type, read_manifest=client.read_identity_manifest)
    aliases = []
    verified_keys = {identity.key, make_local_identity(identity.title, identity.year, media_type).key}
    if tmdb_item:
        external = tmdb_item.get("external_ids") or (tmdb_item.get("raw") or {}).get("external_ids") or {}
        for ns, field in (("imdb", "imdb_id"), ("tvdb", "tvdb_id")):
            if external.get(field):
                verified_keys.add(Identity(media_type, ns, external[field], identity.title, identity.year).key)
    for entry in index.entries:
        if identity.key in entry.manifest.keys or (requested and requested.key in entry.manifest.keys):
            verified_keys.update(entry.manifest.keys)
    for key in (request.get("media_identity") or {}).get("aliases") or []:
        normalized_key = build_identity_manifest(identity, [key]).aliases
        if normalized_key and normalized_key[0] not in verified_keys:
            raise IdentityReviewRequired("请求携带未经媒体库或元数据验证的身份别名")
        aliases.extend(normalized_key)
    aliases.extend(key for key in verified_keys if key[1] != "local" or key[2] == make_local_identity(identity.title, identity.year, media_type).value)
    if requested:
        if requested.media_type != media_type:
            raise IdentityReviewRequired("提交身份的媒体类型不一致")
        if requested.ns == identity.ns and requested.key != identity.key:
            raise IdentityReviewRequired("识别结果与提交的作品身份不一致")
        if tmdb_item and requested.ns not in {"tmdb", "local"}:
            external = tmdb_item.get("external_ids") or (tmdb_item.get("raw") or {}).get("external_ids") or {}
            external_value = external.get({"imdb": "imdb_id", "tvdb": "tvdb_id"}.get(requested.ns, ""))
            if not external_value or str(external_value).lower() != requested.value.lower():
                raise IdentityReviewRequired("TMDB 无法确认提交的外部身份，拒绝合并别名")
        aliases.append(requested.key)
    # Persist this deterministic fallback as an alias while the accepted title
    # and year are known. Never invent aliases from unrelated source filenames.
    aliases.append(make_local_identity(identity.title, identity.year, media_type).key)
    incoming = build_identity_manifest(identity, aliases)
    intent = request.get("organizer_intent", "create_or_match")
    if intent not in {"create_or_match", "update_existing"}:
        raise IdentityReviewRequired("无效的入库意图")
    decision = resolve_target_dir(identity, index, is_first_import=intent == "create_or_match", aliases=incoming.aliases)
    manifest = decision.manifest or incoming
    return {
        "version": 1,
        "scope_root": root,
        "intent": intent,
        "requested_identity": identity.to_dict(),
        "requested_aliases": [list(key) for key in incoming.aliases],
        "canonical_dir": decision.canonical_dir or root.rstrip("/") + "/" + format_dir_name(identity),
        "is_new": decision.is_new,
        "manifest": manifest.to_dict(),
        "previous_manifest": decision.previous_manifest.to_dict() if decision.previous_manifest else None,
        "errors": list(decision.errors),
    }


def mapping_identity(mapping: dict[str, Any]) -> dict[str, Any]:
    raw = mapping.get("raw_data")
    context = raw.get("organizer_identity") if isinstance(raw, dict) else None
    if not isinstance(context, dict) or context.get("version") != 1:
        raise IdentityReviewRequired("整理计划缺少新身份信息，请重新识别")
    return context


def validate_identity_mappings(mappings: list[dict[str, Any]], root: str) -> dict[str, dict[str, Any]]:
    """Validate every writable mapping, including hand-edited and companion ones."""
    contexts: dict[str, dict[str, Any]] = {}
    owners: set[str] = set()
    for mapping in mappings:
        status = str(mapping.get("status") or "")
        if status in {"delete_ad", "skipped_existing"}:
            raise IdentityReviewRequired("身份整理禁止自动删除未确认文件或重复源文件")
        if status != "ready":
            continue
        context = mapping_identity(mapping)
        if context.get("errors"):
            raise IdentityReviewRequired("；".join(str(error) for error in context["errors"]))
        manifest = parse_identity_manifest(context.get("manifest"))
        canonical = str(context.get("canonical_dir") or "")
        if context.get("scope_root") != root or posixpath.dirname(canonical) != root:
            raise IdentityReviewRequired("身份目录越出冻结目标媒体库")
        if parse_dir_name(posixpath.basename(canonical), media_type=manifest.identity.media_type) != manifest.identity:
            raise IdentityReviewRequired("作品目录名与身份清单不一致")
        target = str(mapping.get("target_path") or "")
        if not target.startswith(canonical + "/") or posixpath.normpath(target) != target or "\\" in target or re.search(r"[\x00-\x1f\x7f]", target):
            raise IdentityReviewRequired("目标文件不在已确认的作品目录内")
        relative = target[len(canonical) + 1:].split("/")
        if any(is_container_dir(part) for part in relative[:-1]):
            raise IdentityReviewRequired("目标路径含转存容器目录")
        key = target.casefold()
        if key in owners:
            raise IdentityReviewRequired("多个源文件指向同一目标文件")
        owners.add(key)
        raw = mapping.get("raw_data") or {}
        source_ext = posixpath.splitext(str(mapping.get("source_path") or ""))[1].lower()
        target_ext = posixpath.splitext(target)[1].lower()
        if raw.get("companion_file"):
            if source_ext != target_ext or target_ext not in {".srt", ".ass", ".ssa", ".sup", ".idx", ".sub", ".vtt", ".nfo", ".jpg", ".jpeg", ".png", ".webp"}:
                raise IdentityReviewRequired("不支持的关联附件类型")
            related = [m for m in mappings if m.get("status") == "ready" and not (m.get("raw_data") or {}).get("companion_file") and str(m.get("target_path") or "").startswith(canonical + "/")]
            if not related:
                raise IdentityReviewRequired("关联附件没有可入库的视频，拒绝创建仅字幕目录")
            if target_ext in {".srt", ".ass", ".ssa", ".sup", ".idx", ".sub", ".vtt"}:
                related = [m for m in related if posixpath.dirname(m["target_path"]) == posixpath.dirname(target) and posixpath.basename(target).startswith(posixpath.splitext(posixpath.basename(m["target_path"]))[0] + ".")]
            elif posixpath.dirname(target) != canonical:
                related = [m for m in related if posixpath.dirname(m["target_path"]) == posixpath.dirname(target)]
            if not related:
                raise IdentityReviewRequired("附件路径没有对应视频，拒绝跨季或任意目录写入")
        else:
            if source_ext not in VIDEO_EXTENSIONS or target_ext != source_ext:
                raise IdentityReviewRequired("目标必须保持原视频后缀，镜像和压缩包不允许入库")
            if mapping.get("media_type") != manifest.identity.media_type:
                raise IdentityReviewRequired("映射媒体类型与作品身份不一致")
            if manifest.identity.media_type == "tv":
                season, episode = mapping.get("season"), mapping.get("episode")
                if isinstance(season, bool) or not isinstance(season, int) or not 0 <= season <= 99:
                    raise IdentityReviewRequired("缺少有效季号")
                if isinstance(episode, bool) or not isinstance(episode, int) or not 1 <= episode <= 2000:
                    raise IdentityReviewRequired("缺少有效集号")
                if len(relative) != 2 or relative[0] != f"Season {season:02d}":
                    raise IdentityReviewRequired("剧集必须位于对应的 Season 目录")
                if not re.search(rf" - S{season:02d}E{episode:02d}(?= - |\.[^.]+$)", relative[-1]):
                    raise IdentityReviewRequired("目标文件名与季集信息不一致")
                parsed = raw.get("parsed") or {}
                if season > 10 and not (parsed.get("has_explicit_season") or raw.get("manual_episode_override")):
                    raise IdentityReviewRequired("季号大于 10 且无显式季号证据，需要修改确认")
            elif len(relative) != 1:
                raise IdentityReviewRequired("电影必须直接位于作品目录")
        previous = contexts.get(canonical)
        if previous is not None and previous != context:
            raise IdentityReviewRequired("同一作品的身份计划不一致，请重新识别")
        contexts[canonical] = context
    return contexts


def manifest_operation(context: dict[str, Any]) -> dict[str, Any]:
    manifest = parse_identity_manifest(context["manifest"])
    previous = context.get("previous_manifest")
    return {
        "type": "write_identity_manifest",
        "target_path": context["canonical_dir"] + "/" + IDENTITY_MANIFEST_NAME,
        "description": "保存可从媒体库恢复的作品身份与别名",
        "status": "pending",
        "reason": ["身份先于文件移动，失败时停止该任务"],
        "raw_data": {
            "content": serialize_identity_manifest(manifest),
            "previous_content": serialize_identity_manifest(parse_identity_manifest(previous)) if previous is not None else None,
        },
    }


def revalidate_identity_plan(client: Any, mappings: list[dict[str, Any]], operations: list[dict[str, Any]], root: str, *, staging_root: str = "") -> None:
    """Called under the library lock immediately before the first side effect."""
    contexts = validate_identity_mappings(mappings, root)
    indexes = {}
    for canonical, context in contexts.items():
        identity = identity_from_mapping(context["requested_identity"])
        if identity.media_type not in indexes:
            indexes[identity.media_type] = scan_library(client, root, media_type=identity.media_type, read_manifest=client.read_identity_manifest)
        decision = resolve_target_dir(identity, indexes[identity.media_type], is_first_import=bool(context["is_new"]) and context["intent"] == "create_or_match", aliases=context["requested_aliases"])
        if not decision.success or decision.canonical_dir != canonical:
            raise IdentityReviewRequired("媒体库身份目录已变化，必须重新识别：" + "; ".join(decision.errors))
        expected = parse_identity_manifest(context["manifest"])
        if decision.manifest != expected:
            raise IdentityReviewRequired("作品身份别名已变化，请重新识别后再执行")
    manifest_targets: set[str] = set()
    created_roots: set[str] = set()
    mapping_pairs = {(m.get("source_path"), m.get("target_path")) for m in mappings if m.get("status") == "ready"}
    allowed_dirs = set(contexts)
    for _source, target in mapping_pairs:
        directory = posixpath.dirname(target)
        while directory != root and directory.startswith(root + "/"):
            allowed_dirs.add(directory)
            directory = posixpath.dirname(directory)
    for op in operations:
        target = str(op.get("target_path") or "")
        if posixpath.normpath(target) != target or "\\" in target or re.search(r"[\x00-\x1f\x7f]", target):
            raise IdentityReviewRequired("操作目标路径不是安全的规范路径")
        kind = op.get("type")
        if kind == "write_identity_manifest":
            canonical = posixpath.dirname(target)
            context = contexts.get(canonical)
            if not context or target != canonical + "/" + IDENTITY_MANIFEST_NAME:
                raise IdentityReviewRequired("身份写入操作不属于当前计划")
            if canonical in manifest_targets:
                raise IdentityReviewRequired("重复的身份写入操作")
            if context["is_new"] and canonical not in created_roots:
                raise IdentityReviewRequired("身份写入必须在作品根创建操作之后")
            expected_op = manifest_operation(context)
            if op.get("raw_data") != expected_op["raw_data"]:
                raise IdentityReviewRequired("身份写入内容与已确认计划不一致")
            manifest_targets.add(canonical)
        elif kind == "move_file":
            if (op.get("source_path"), target) not in mapping_pairs:
                raise IdentityReviewRequired("文件操作不属于已确认映射")
            if not any(target.startswith(canonical + "/") for canonical in manifest_targets):
                raise IdentityReviewRequired("移动文件必须在身份写入操作之后")
        elif kind == "create_dir":
            if target not in allowed_dirs:
                raise IdentityReviewRequired("建目录操作越出身份根")
            if target in contexts:
                context = contexts[target]
                if op.get("raw_data") != {"identity_root": True, "allow_create": bool(context["is_new"]) and context["intent"] == "create_or_match"}:
                    raise IdentityReviewRequired("作品根创建条件已变化")
                created_roots.add(target)
        elif kind == "cleanup_empty_dir":
            if not staging_root or not target.startswith(staging_root.rstrip("/") + "/"):
                raise IdentityReviewRequired("清理操作越出冻结暂存目录")
        else:
            raise IdentityReviewRequired("身份整理不允许该操作：" + str(kind))
    if set(contexts) != manifest_targets:
        raise IdentityReviewRequired("计划缺少持久化身份写入操作")


def execute_manifest_write(client: Any, op: dict[str, Any]) -> dict[str, Any]:
    target = op["target_path"]
    raw = op.get("raw_data") or {}
    desired = parse_identity_manifest(raw.get("content"))
    before = raw.get("previous_content")
    current = client.read_identity_manifest(target)
    current_manifest = parse_identity_manifest(current) if current is not None else None
    previous_manifest = parse_identity_manifest(before) if before is not None else None
    if current_manifest not in (previous_manifest, desired):
        raise IdentityReviewRequired("身份附件已由其他任务修改，拒绝覆盖")
    if current_manifest != desired:
        if not client.write_identity_manifest(target, serialize_identity_manifest(desired)):
            raise IdentityReviewRequired("身份附件写入未成功")
        observed = client.read_identity_manifest(target)
        if observed is None or parse_identity_manifest(observed) != desired:
            raise IdentityReviewRequired("身份附件写后校验失败，未移动视频")
    return {"type": "restore_identity_manifest", "target_path": target, "previous_content": before, "written_content": serialize_identity_manifest(desired)}


def restore_manifest(client: Any, inverse: dict[str, Any]) -> None:
    target = inverse["target_path"]
    before = inverse.get("previous_content")
    current = client.read_identity_manifest(target)
    observed = parse_identity_manifest(current) if current is not None else None
    previous = parse_identity_manifest(before) if before is not None else None
    if observed == previous:
        return
    if observed != parse_identity_manifest(inverse.get("written_content")):
        raise IdentityReviewRequired("身份附件有后续修改，拒绝回滚覆盖")
    remaining = client.list_dir(posixpath.dirname(target), refresh=True)
    if any(item.name != IDENTITY_MANIFEST_NAME for item in remaining):
        raise IdentityReviewRequired("作品仍有文件可能依赖当前身份，拒绝撤销别名")
    if previous is None:
        if not client.remove_file(target):
            raise IdentityReviewRequired("身份附件回滚删除失败")
    elif not client.write_identity_manifest(target, serialize_identity_manifest(previous)):
        raise IdentityReviewRequired("身份附件回滚写入失败")
    final = client.read_identity_manifest(target)
    if (parse_identity_manifest(final) if final is not None else None) != previous:
        raise IdentityReviewRequired("身份附件回滚结果未确认")
