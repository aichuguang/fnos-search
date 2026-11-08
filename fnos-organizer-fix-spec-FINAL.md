# fnos 入库整理链路 · 完整修补文档（最终版）

版本：v5 FINAL · 2026-11-08
范围：`fnos_media_import`（parser / organizer / service / subscriptions / 迁移脚本）
目标：**同一部剧永远只有一个目录；解析错误不写盘；身份不依赖数据库；视频格式不过度限制。**

---

# 目录

1. 背景：今天暴露的问题
2. 目标与不变量
3. 命名规范
4. 视频格式白名单
5. 身份体系
6. 数据模型
7. parser.py 改造
8. identity.py（新增）
9. library_scan.py（新增）
10. service.py：目录解析与写盘
11. 订阅（追更）链路
12. apply 闸门
13. 日志与告警
14. 存量迁移（A–F 阶段）
15. 测试计划
16. 上线顺序与回滚
17. 验收清单
18. 附录 A：已知脏数据
19. 附录 B：Bug 复现证据

---

# 1. 背景：今天暴露的问题

| # | 现象 | 根因 |
|---|---|---|
| 1 | 同一部剧出现两个目录，各占一半集数 | 目录名由“本次任务标题/源目录名”生成，追更时标题写法不同 → 新建目录 |
| 2 | `52-4K.mp4` 被整理成 `S52E04` | 季-集正则 `(\d{1,2})-(\d{1,4})` 把画质 `4K` 的 4 当集号 |
| 3 | `12-4K.高码率.mp4` → `S12E04` | 同上 |
| 4 | `04-超清.mp4` → `S04E04` | 同上 |
| 5 | 库里出现 `mobile share <hash>` 目录 | 139 转存容器层未剥离，直接拼进目标路径 |
| 6 | 库里出现 `job 110` 目录 | 任务目录名被当作剧名 |
| 7 | 任务标题 `更新至` / `job 110` | 标题候选未剔除状态词 |
| 8 | `.iso` / `.nfo` / `.ass` 进入库 | 未做后缀白名单 |
| 9 | 任务标题《传教士》，内容是《海军罪案调查处》 | 解析标题与任务标题无一致性校验 |
| 10 | 追更时找不到原目录 | 没有基于稳定身份的目录定位机制 |

---

# 2. 目标与不变量

## 2.1 五条不变量

| # | 不变量 | 违反处理 |
|---|---|---|
| I1 | 同一个身份（任意命名空间）只能对应一个目录 | 拒绝写盘 + 告警 |
| I2 | 目录名必须形如 `标题 (年份) [<ns>-<id>]` | 拒绝写盘 |
| I3 | 只有首次入库允许 `mkdir`；追更找不到目录必须报错 | 转 review |
| I4 | 目标路径不得出现容器目录 | 拒绝写盘 |
| I5 | 库里只允许白名单视频后缀 | 丢弃并记日志 |

## 2.2 设计原则

1. **网盘目录是唯一事实来源**，任务表只是缓存。
2. **身份先于路径**：先定身份，再算目录。
3. **宁停不建**：不确定就停，绝不新建第二个目录。
4. **确定性**：同一输入永远得到同一身份、同一目录名。

---

# 3. 命名规范

## 3.1 目录

```
/移动云/电视剧/伟大的长征 (2026) [tmdb-297253]/
/移动云/电影/敦煌英雄 (2026) [tmdb-1089598]/
/移动云/动漫/仙逆 (2023) [tmdb-94664]/
/移动云/电视剧/某冷门剧 (2024) [local-9f3a1c7d20]/
```

规则：

- 标题：TMDB `zh-CN` 标题，空白归一
- 年份：tv 首播年 / movie 上映年
- 标签：`[<ns>-<id>]`，紧跟年份后，一个空格分隔
- 禁止：全角括号、多余后缀、`Season` 出现在剧目录层

## 3.2 季目录

```
Season 01/
Season 02/
```

固定两位数字。

## 3.3 文件

```
伟大的长征 (2026) - S01E05.mkv
伟大的长征 (2026) - S01E05 - 4K.mkv      ← 同集不同画质
```

---

# 4. 视频格式白名单

## 4.1 接收

```python
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".wmv", ".flv", ".mov", ".m4v",
    ".ts", ".m2ts", ".mts", ".webm", ".rmvb", ".rm",
    ".mpg", ".mpeg", ".3gp", ".ogv", ".divx", ".vob",
}
```

## 4.2 拒绝

```python
BLOCKED_EXTS = {
    ".iso", ".img", ".rar", ".zip", ".7z",      # 镜像/压缩
    ".nfo", ".srt", ".ass", ".ssa", ".sub",     # 字幕/元数据
    ".torrent", ".txt", ".jpg", ".png", ".jpeg",
}
```

## 4.3 判定

```python
def classify_ext(path):
    ext = splitext(path)[1].lower()
    if ext in BLOCKED_EXTS: return "blocked"
    if ext in VIDEO_EXTS:   return "video"
    return "unknown"        # 记录日志后丢弃
```

---

# 5. 身份体系

## 5.1 命名空间

| 前缀 | 来源 | 使用场景 |
|---|---|---|
| `tmdb` | TMDB numeric id | 默认首选 |
| `imdb` | `tt\d+` | TMDB 缺失时有 IMDB |
| `tvdb` | numeric | 剧集，TMDB external_ids |
| `douban` | numeric | 国产剧/冷门剧兜底 |
| `local` | 确定性哈希 | 全网查不到 |

## 5.2 选择优先级

```
tmdb > imdb > tvdb > douban > local
```

## 5.3 local ID（确定性）

```python
def local_identity(title: str, year: int | None) -> str:
    key = f"{normalize_title(title)}|{year or ''}"
    return "local-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
```

性质：

- 同一部剧永远同一 ID
- 与数据库、任务记录无关
- 后续查到 TMDB 时升级：`[local-x] → [tmdb-y]`，目录仍唯一

## 5.4 身份集合

一个 job 可能同时携带多个身份，用于匹配：

```python
job.identity_keys = [
    ("tmdb", 297253),
    ("imdb", "tt1234567"),
    ("local", "local-9f3a1c7d20"),
]
```

---

# 6. 数据模型

## 6.1 import_jobs

```sql
ALTER TABLE import_jobs ADD COLUMN identity_ns      TEXT;
ALTER TABLE import_jobs ADD COLUMN identity_value   TEXT;
ALTER TABLE import_jobs ADD COLUMN identity_season  INTEGER;
ALTER TABLE import_jobs ADD COLUMN identity_title   TEXT;
ALTER TABLE import_jobs ADD COLUMN identity_year    INTEGER;
ALTER TABLE import_jobs ADD COLUMN canonical_dir    TEXT;   -- 缓存
ALTER TABLE import_jobs ADD COLUMN is_first_import  INTEGER DEFAULT 0;
```

## 6.2 subscriptions

```sql
ALTER TABLE subscriptions ADD COLUMN identity_ns    TEXT;
ALTER TABLE subscriptions ADD COLUMN identity_value TEXT;
ALTER TABLE subscriptions ADD COLUMN canonical_dir  TEXT;
```

## 6.3 不建索引表

身份写在目录名里，`build_id_map()` 每次扫描一级目录即可（232 个目录 < 2 秒）。
如需优化，进程内 60 秒 TTL 缓存，不落库。

---

# 7. parser.py 改造

## 7.1 常量

```python
CONTAINER_PATTERNS = [
    r"^mobile\s*share\s+[0-9a-f]{8,}$",
    r"^job[-\s]?\d+$",
    r"^\d{1,3}$",
    r"^新建文件夹.*$",
    r"^_入库暂存$",
    r"^[0-9a-f]{16,}$",
]

QUALITY = (r"4K|2160P?|1080P?|720P?|HDR|DV|DoVi|WEB[-. ]?DL|BluRay|REMUX|"
           r"x264|x265|H\.?265|HEVC|高码率|超清|高清|臻彩")

STATUS_WORDS = r"(更新至第?\d+集?|全\d+集|持续更新|完结|合集|第[一二三四五六七八九十\d]+季完整版)"

ID_TAG = re.compile(r"\[(?P<ns>tmdb|imdb|tvdb|douban|local)-(?P<id>[A-Za-z0-9]+)\]", re.I)
```

## 7.2 is_container_dir

```python
def is_container_dir(name: str) -> bool:
    text = str(name or "").strip()
    return any(re.match(p, text, re.I) for p in CONTAINER_PATTERNS)
```

## 7.3 parse_dir_name

```python
def parse_dir_name(name: str) -> dict:
    text = str(name or "").strip()
    if is_container_dir(text):
        return {"container": True, "title": None, "year": None, "ns": None, "id": None}

    tag = ID_TAG.search(text)
    body = ID_TAG.sub("", text).strip()
    m = re.search(r"^(?P<title>.+?)\s*\((?P<year>(?:19|20)\d{2})\)", body)
    if m:
        title, year = m.group("title").strip(), int(m.group("year"))
    else:
        title, year = body.strip(), None
    return {
        "container": False,
        "title": re.sub(r"\s+", " ", title),
        "year": year,
        "ns": tag.group("ns").lower() if tag else None,
        "id": tag.group("id") if tag else None,
    }
```

## 7.4 parse_season

```python
def parse_season(value) -> int | None:
    text = str(value or "").strip()
    patterns = [
        r"(?i)(?<![A-Za-z])s(?:eason)?\s*0*(\d{1,2})(?=$|[\s._\-)\]】])",
        r"(?i)(?:^|[\s._\-[(【])season\s*0*(\d{1,2})(?=$|[\s._\-)\]】])",
        rf"第\s*(\d{{1,2}}|[{CHINESE_NUMBER_CHARS}]+)\s*季",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return _season_number(m.group(1))
    return None      # ← 删除“纯数字即季号”的分支
```

## 7.5 季-集规则收紧

```python
SEASON_EPISODE_RE = re.compile(
    r"(?:^|[\s._\-])0*(?P<s>\d{1,2})\s*-\s*0*(?P<e>\d{1,4})(?![\dA-Za-z\u4e00-\u9fff])"
)

def _season_episode(text: str):
    for m in SEASON_EPISODE_RE.finditer(text):
        if re.search(QUALITY, text[m.end(): m.end() + 6], re.I):
            continue                      # 5-4K / 12-1080P 不是季-集
        if not _valid_episode_number(int(m.group("e"))):
            continue
        return int(m.group("s")), int(m.group("e"))
    return None
```

## 7.6 parse_episode 优先级

```python
def parse_episode(text, season_hint=None):
    # 1 SxxExx
    # 2 第N集 / 第N话
    # 3 季-集（收紧后）
    # 4 EPxx / Episode xx
    # 5 尾部数字：05.mp4
    # 6 开头数字：05xxx.mp4
    # 7 裸数字：05.mp4
    # 全失败 → (season_hint or 1, None)
```

任何一步命中后，season 一律取 `season_hint`；为空取 `1`。

## 7.7 标题清洗

```python
def clean_title(value: str) -> str:
    text = re.sub(STATUS_WORDS, "", str(value or ""))
    text = re.sub(r"[\[\]【】()（）]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
```

## 7.8 一致性校验

```python
if similarity(clean_title(parsed.title), clean_title(job.title)) < 0.6:
    raise NeedManualReview("解析标题与任务标题不一致")
```

---

# 8. identity.py（新增）

```python
@dataclass
class Identity:
    ns: str          # tmdb/imdb/tvdb/douban/local
    value: str
    title: str
    year: int | None

def resolve_identity(title, year, media_type="tv") -> Identity:
    """TMDB → external_ids → 豆瓣 → local"""
    hit = tmdb_search(title, year, media_type)
    if hit and abs((hit.year or 0) - (year or hit.year)) <= 1:
        return Identity("tmdb", str(hit.id), hit.title_zh, hit.year)

    ext = tmdb_external_ids(hit.id) if hit else {}
    if ext.get("imdb_id"):
        return Identity("imdb", ext["imdb_id"], hit.title_zh, hit.year)
    if ext.get("tvdb_id"):
        return Identity("tvdb", str(ext["tvdb_id"]), hit.title_zh, hit.year)

    douban = douban_search(title, year)
    if douban:
        return Identity("douban", str(douban.id), douban.title, douban.year)

    lid = local_identity(title, year)
    return Identity("local", lid.split("-", 1)[1], clean_title(title), year)

def format_dir_name(identity: Identity) -> str:
    year = f" ({identity.year})" if identity.year else ""
    return f"{identity.title}{year} [{identity.ns}-{identity.value}]"
```

要点：

- 年份校验：差 > 1 年视为不匹配（防《大话西游》2016 版误配）
- 同名多候选：优先 `media_type` 一致 + 年份最近
- 全部失败也不阻塞：用 local 身份

---

# 9. library_scan.py（新增）

```python
def scan_roots(roots):
    for root in roots:
        for entry in openlist.list_dir(root):
            if not entry.is_dir:
                continue
            yield entry

def build_id_map(roots) -> dict[tuple[str, str], str]:
    mapping = {}
    for entry in scan_roots(roots):
        parsed = parse_dir_name(entry.name)
        if not parsed["ns"]:
            continue
        key = (parsed["ns"], parsed["id"])
        if key in mapping:
            raise DuplicateIdentity(key, [mapping[key], entry.path])
        mapping[key] = entry.path
    return mapping
```

调用：每次 organizer run 开始扫一次；迁移脚本同样复用。

---

# 10. service.py：目录解析与写盘

## 10.1 resolve_target_dir

```python
def resolve_target_dir(job, parsed, id_map):
    # 1. 身份精确命中
    for key in job.identity_keys:
        if key in id_map:
            return id_map[key]

    # 2. 历史目录（无标签）→ 改名补标签
    legacy = find_legacy_dir(parsed.title, parsed.year)
    if legacy:
        new_path = rename_with_identity(legacy, job.primary_identity)
        id_map[job.primary_identity] = new_path
        return new_path

    # 3. 历史 local 目录升级
    lid = ("local", local_identity(parsed.title, parsed.year).split("-", 1)[1])
    if lid in id_map:
        return id_map[lid]

    # 4. 首次入库才新建
    if not job.is_first_import:
        raise NeedManualReview(f"追更找不到身份目录：{job.primary_identity}")

    path = openlist.mkdir(join(root_for(job.category), format_dir_name(job.identity_spec)))
    id_map[job.primary_identity] = path
    return path
```

## 10.2 plan_writes

```python
def plan_writes(target_dir, parsed_files, existing):
    actions = []
    for f in parsed_files:
        kind = classify_ext(f.path)
        if kind != "video":
            actions.append(Drop(f, f"后缀 {kind}"))
            continue
        if is_container_dir(f.parent):
            actions.append(Drop(f, "容器目录"))
            continue

        key = (f.season, f.episode)
        name = f"… - S{f.season:02d}E{f.episode:02d}{f.ext}"
        if key not in existing:
            actions.append(Write(target_dir, name))
        elif same_quality(existing[key], f):
            actions.append(Skip(f, "同集同画质"))
        else:
            actions.append(Write(target_dir, f"… - S{f.season:02d}E{f.episode:02d} - {quality_tag(f)}{f.ext}"))
    return actions
```

## 10.3 禁止行为

- 因“已存在”新建目录
- 覆盖已有文件
- 把容器目录写进目标路径
- 写入白名单外文件

---

# 11. 订阅（追更）链路

首次入库成功后：

```python
subscription.identity_ns = job.identity_ns
subscription.identity_value = job.identity_value
subscription.canonical_dir = target_dir      # 仅缓存
```

追更任务创建：

```python
job.identity_ns = subscription.identity_ns
job.identity_value = subscription.identity_value
job.is_first_import = False
```

**追更一律禁止 mkdir。**

---

# 12. apply 闸门

```python
def validate(actions, job, id_map):
    errors = []
    for a in actions:
        if a.kind != "write":
            continue
        if not ID_TAG.search(basename(a.target_dir)):
            errors.append("目标目录缺身份标签")
        if is_container_dir(basename(a.target_dir)):
            errors.append("容器目录")
        if a.season > 10 and not job.has_explicit_season:
            errors.append(f"季号可疑 S{a.season}")
        if classify_ext(a.name) != "video":
            errors.append("非视频后缀")
    if errors:
        raise NeedManualReview(errors)
```

| 检查 | 处理 |
|---|---|
| 目标目录无 `[ns-id]` 且非首次 | 拒绝 |
| 同身份多目录 | 拒绝 + 告警 |
| 季号 > 10 且无季标 | 拒绝 |
| 路径含容器目录 | 拒绝 |
| 后缀不在白名单 | 丢弃 |
| 标题相似度 < 0.6 | 人工 |
| local 身份前后不一致 | 记 bug |

---

# 13. 日志与告警

必须输出：

```
resolve: tmdb=297253 -> /移动云/电视剧/伟大的长征 (2026) [tmdb-297253]
mkdir: tmdb=xxxx (first_import)
reject: no-dir tmdb=xxxx
duplicate: tmdb=xxxx -> [dir1, dir2]
parse: 52-4K.mp4 -> S01E52 rule=season-episode-tight
drop: file.iso reason=blocked_ext
```

告警：`duplicate` / `reject` / `mkdir` 聚合后推飞书（每 5 分钟）。

---

# 14. 存量迁移（A–F）

## A 扫描报告（只读）

```json
{
  "dir": "/移动云/电视剧/伟大的长征",
  "has_id": false,
  "container": false,
  "seasons": {"1": [15,16,...45]},
  "file_count": 29,
  "suspects": ["season_gt_10", "no_id"]
}
```

## B 补身份

```
能解析 TMDB → rename 成 标题 (年份) [tmdb-id]
只能解析标题/年份 → [local-哈希]
无法解析 → unknown 列表，人工
```

## C 合并同身份多目录

```
以文件多的为准
缺集 → move
重复集 → 加画质后缀
空目录 → 删除
```

## D 展开容器

```
mobile share <hash>/ → 取内部真实剧目录
job N/、纯数字/ → 同上
空壳 → 删除
```

## E 非视频清理

```
iso/nfo/ass/srt → /移动云/_非视频_YYYYMMDD/
```

## F 校验

```
每个 identity 恰好一个目录
每个 Season 内集号连续（允许缺口但需报告）
```

---

# 15. 测试计划

## 15.1 解析单测

| 输入 | season | episode |
|---|---|---|
| `S01E05.mp4` | 1 | 5 |
| `第5集.mp4` | 1 | 5 |
| `05.mp4` | 1 | 5 |
| `5-4K.mp4` | 1 | 5 |
| `52-4K.mp4` | 1 | 52 |
| `12-1080P.mkv` | 1 | 12 |
| `04-超清.avi` | 1 | 4 |
| `第2季 第5集.rmvb` | 2 | 5 |
| `S02E05.mkv` | 2 | 5 |
| `2026.mp4` | — | — |
| `x264-10bit.mp4` | — | — |
| `1080P.mp4` | — | — |

## 15.2 目录解析

| 输入 | 结果 |
|---|---|
| `伟大的长征 (2026) [tmdb-297253]` | title/2026/tmdb/297253 |
| `伟大的长征 (2026)` | title/2026/None |
| `mobile share f62d…` | container |
| `job 110` | container |
| `52` | container |

## 15.3 后缀

| 文件 | 结果 |
|---|---|
| `a.avi` `a.ts` `a.m2ts` `a.rmvb` `a.webm` | 接收 |
| `a.iso` `a.rar` `a.nfo` `a.srt` | 拒绝 |

## 15.4 集成

| 场景 | 期望 |
|---|---|
| 首次入库 | 建带标签目录 |
| 同剧追更 10 次 | 0 新目录 |
| 同身份两目录 | 拒绝 + 告警 |
| 追更无目录 | 拒绝，转 review |
| 删光 job 表后追更 | 仍命中 |
| TMDB 查不到 | 建 `[local-…]`，重复入库同一目录 |

---

# 16. 上线顺序与回滚

1. parser 规则 + 单测（影子模式：只记日志不改行为）
2. join：`parse_dir_name` / `is_container_dir` / `build_id_map`
3. `resolve_target_dir` 切到身份优先（保留 legacy fallback）
4. 闸门从 warn 切 reject
5. 订阅写 identity 字段
6. 迁移脚本 dry-run → 执行
7. 飞牛重扫

回滚开关：

```
organizer.identity_mode = legacy | shadow | enforce
```

---

# 17. 验收清单

- [ ] 15.1 的 12 条解析用例全绿
- [ ] 15.2 目录用例全绿
- [ ] 15.3 后缀用例全绿
- [ ] 新入库 10 部：目录 100% 带 `[ns-id]`
- [ ] 追更 10 次：0 新目录
- [ ] 同身份 0 重复目录
- [ ] 容器目录 0 进入库
- [ ] iso/nfo/ass 0 进入库
- [ ] 删光 job/subscription 后追更仍命中
- [ ] 存量 232 目录：有身份标签比例 100%（除 unknown）
- [ ] 12 组历史重复目录合并完成

---

# 18. 附录 A：已知脏数据（迁移阶段要处理）

## 重复目录（12 组）

| 剧 | 重复形式 | 文件数 |
|---|---|---|
| 伟大的长征 | `伟大的长征` / `伟大的长征 (2026)` | 29 / 24 |
| 走马上任 | `走马上任` / `走马上任 (2026)` | 9 / 5 |
| 尚公主 | `尚公主` / `尚公主 (2026)` | 1 / 4 |
| 逐玉 | `逐玉` / `逐玉 (2026)` | 40 / 1 |
| 雀骨 | `雀骨` / `雀骨 (2026)` | 14 / 10 |
| 九门 | `九门 (2026)` / `九门(2026)` | 19 / 1 |
| 在人间 | `在人间 (2025)` / `在人间(2025)` | 1 / 7 |
| 藏海传 | `藏海传 (2025)` / `藏海传(2025)` | 40 / 14 |
| 永夜星河 | `永夜星河` / `永夜星河 (2024)` | 30 / 1 |
| 剑来 | `剑来` / `剑来 (2024)` | 8 / 0 |
| 仙逆 | `仙逆` / `仙逆 (2023)` | 1 / 38 |
| 完美世界 | `完美世界` / `完美世界 (2021)` | 244 / 0 |

## 容器目录

```
/移动云/电视剧/mobile share 53fa073c…（双峰）
/移动云/电视剧/mobile share f62d7b77…（黑钱胜地）
/移动云/电视剧/mobile share 7979e058…（使女的故事）
/移动云/动漫/mobile share 90421e4b…（星辰变）
/移动云/动漫/mobile share 7b09d5f1…（时光代理人）
/移动云/动漫/mobile share 313f498e…
```

## 错误季

```
伟大的长征 (2026)/Season 52、Season 53  → 已修
走马上任 (2026)/Season 14                → 已修
谷雨 (2026)/Season 04                    → 待清理（与 S01E04 重复）
学园孤岛 (2015)/Season 07                → 仅 .ass，待删除
```

## 非视频

```
伟大的长征 (2026)/Season 01/…E01–E12.iso（12 个）
双峰/…/xxx.nfo
学园孤岛/…/S07E97.ass
```

## 标题错配

```
task 124：标题《传教士 (2016)》，实际内容《海军罪案调查处》561 文件
task 77 ：标题《更新至》，实际内容《走马上任》
```

---

# 19. 附录 B：Bug 复现证据

```
parse_file_name("52-4K.mp4")   -> season=52, episode=4
parse_file_name("53-4K.mp4")   -> season=53, episode=4
parse_file_name("12-4K.高码率.mp4") -> season=12, episode=4
parse_file_name("04-超清.mp4") -> season=4,  episode=4
parse_file_name("S01E52.mp4")  -> season=1,  episode=52   （正常）
parse_file_name("第52集.mp4")  -> season=None, episode=52 （正常）
```

命中规则代码位置：

```python
# organizer/parser.py  parse_episode()
(r"(?:^|[\s._-])0*(\d{1,2})\s*-\s*0*(\d{1,4})(?:\D|$)", "命中 季-集 规则", True)
```

修复方式见 7.5。
