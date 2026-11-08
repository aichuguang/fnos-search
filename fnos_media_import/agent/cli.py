"""Command line management for agent tokens.

Run it inside the web container so it sees the same SQLite file:

    docker exec -it fnos-media-import python -m fnos_media_import.agent.cli create --name hermes
    docker exec -it fnos-media-import python -m fnos_media_import.agent.cli list
    docker exec -it fnos-media-import python -m fnos_media_import.agent.cli revoke --id 2
    docker exec -it fnos-media-import python -m fnos_media_import.agent.cli audit --limit 20

The plaintext token is printed exactly once by ``create``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from ..database import Database
from .audit import AgentAuditLog
from .auth import ALL_SCOPES, DEFAULT_SCOPES, SCOPE_PROFILES, AgentTokenStore


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    configured = str(os.getenv("FNOS_AGENT_DB", "")).strip()
    if configured:
        return Path(configured).expanduser()
    for candidate in (Path("data/app.db"), Path("/app/data/app.db")):
        if candidate.exists():
            return candidate
    return Path("data/app.db")


def _store(path: Path) -> AgentTokenStore:
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}；请先用新版镜像启动一次 Web 容器以创建表结构")
    database = Database(path)
    # Only the agent DDL: never run the full migration chain from a CLI process
    # that may race the web container.
    with database.connect() as connection:
        Database._apply_agent_token_migration(connection)
    return AgentTokenStore(database)


def _cmd_create(args: argparse.Namespace) -> int:
    store = _store(_resolve_db_path(args.db))
    scopes = [item.strip() for item in (args.scopes or "").split(",") if item.strip()]
    if not scopes:
        scopes = list(SCOPE_PROFILES.get(str(args.profile or "read"), DEFAULT_SCOPES))
    unknown = sorted(set(scopes) - set(ALL_SCOPES))
    if unknown:
        print(f"未知作用域：{', '.join(unknown)}", file=sys.stderr)
        print(f"可用作用域：{', '.join(ALL_SCOPES)}", file=sys.stderr)
        return 2
    plaintext, record = store.create(
        args.name,
        scopes,
        rate_limit=int(args.rate_limit or 0),
        expires_at=str(args.expires_at or ""),
    )
    print("已创建 Agent 令牌（只显示这一次，请立刻保存）：")
    print("")
    print(f"  {plaintext}")
    print("")
    print(f"id={record['id']} name={record['name']} scopes={','.join(record['scopes'])}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    store = _store(_resolve_db_path(args.db))
    rows = store.list_tokens()
    if not rows:
        print("暂无 Agent 令牌")
        return 0
    for row in rows:
        state = "revoked" if row["revoked_at"] else "active"
        print(
            f"id={row['id']} name={row['name']} prefix={row['token_prefix']} "
            f"state={state} scopes={','.join(row['scopes'])} "
            f"last_used={row['last_used_at'] or '-'}"
        )
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    store = _store(_resolve_db_path(args.db))
    if store.revoke(int(args.id)):
        print(f"已撤销令牌 id={args.id}")
        return 0
    print(f"令牌不存在或已撤销：id={args.id}", file=sys.stderr)
    return 1


def _cmd_audit(args: argparse.Namespace) -> int:
    store = _store(_resolve_db_path(args.db))
    rows = AgentAuditLog(store.database).recent(limit=int(args.limit or 20))
    if not rows:
        print("暂无调用记录")
        return 0
    for row in rows:
        print(
            f"{row['created_at']} tool={row['tool']} outcome={row['outcome']} "
            f"token={row['token_name']} code={row['error_code'] or '-'} "
            f"ms={row['duration_ms']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fnos-agent", description="管理 fnos-search 的 Agent 令牌")
    parser.add_argument("--db", help="SQLite 路径，默认自动探测 data/app.db")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="创建令牌")
    create.add_argument("--name", required=True, help="令牌名称，例如 hermes")
    create.add_argument(
        "--profile",
        choices=sorted(SCOPE_PROFILES),
        default="read",
        help="权限档位：read 只读（默认）、operator 可写但不放行整理/回滚、full 含整理执行授权",
    )
    create.add_argument("--scopes", help="逗号分隔的作用域，优先级高于 --profile")
    create.add_argument("--rate-limit", type=int, default=0, help="每分钟调用上限，0 表示默认值")
    create.add_argument("--expires-at", default="", help="过期时间，ISO8601，留空表示永久")
    create.set_defaults(func=_cmd_create)

    listing = sub.add_parser("list", help="列出令牌")
    listing.set_defaults(func=_cmd_list)

    revoke = sub.add_parser("revoke", help="撤销令牌")
    revoke.add_argument("--id", type=int, required=True)
    revoke.set_defaults(func=_cmd_revoke)

    audit = sub.add_parser("audit", help="查看最近的工具调用审计")
    audit.add_argument("--limit", type=int, default=20)
    audit.set_defaults(func=_cmd_audit)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
