# 剧集缺集审计工具 — 实施规格（交给执行方）

> 本文是**可独立执行的实施规格**。执行方不需要额外上下文，按本文实现即可。
> 接口原始记录见 [`docs/fnos-media-api.md`](fnos-media-api.md)。

---

## 0. 任务与交付物

写一个**只读**的本地命令行脚本，扫描飞牛影视电视剧库，输出缺集报告。

| 项 | 要求 |
|---|---|
| 代码位置 | `scripts/audit_library.py`（单文件可执行）+ 少量客户端方法 |
| 运行方式 | `python scripts/audit_library.py --library 电视剧` |
| 输出 | 终端表格；`--csv` / `--json` 导出 |
| 性质 | 只读。不补集、不下载、不写回飞牛、不进 MCP |
| 依赖 | 复用仓库现有客户端，**不新增第三方依赖** |

---

## 1. 已确认的事实（不要重新探索，也不要推翻）

### 1.1 调用链

```
库   POST /v/api/v1/item/list        {ancestor_guid: 库guid, page, page_size, ...}
剧   GET  /v/api/v1/season/list/{剧guid}      → 季数组
季   GET  /v/api/v1/episode/list/{季guid}     → 集数组
```

- `item/list` 是 **POST + JSON body**，`data.total` 给总数，**没有** `pages` 字段，自行 `ceil(total/page_size)`。
- `season/list`、`episode/list` 返回**裸数组**，无分页。

### 1.2 铁律（违反任何一条都会产出错误报告）

1. **集数只数 `episode/list` 的行数。**
   实测「美人余」：剧级 `local_number_of_episodes=14`、季级 `=13`、实际 13 行。
   用字段会把"缺 1 集"算成"不缺"。
2. **不使用飞牛的 `number_of_episodes` / `status` 作为播出事实。**
   那是入库时抓取的 TMDB 快照，会过期。播出事实一律实时查 TMDB。
3. **缺集时 `episode/list` 不补占位行。**
   实测「美人余」缺第 13 集，`episode_number` 直接从 12 跳到 14。
   所以集合差集可以精确算出缺第几集。
4. **分集行的 `number_of_episodes` / `local_number_of_episodes` / `status` 全是 `0` / `"1"`，无意义。**
   只用 `season_number`、`episode_number`、`air_date`、`title`。
5. **`video_guid` / `duration > 0` 不能当"文件存在"的证据。**
   「九门」30 集里只有 4 集有，但 30 集都在。
6. **分集 `title` 为空是正常的**，不代表缺集。

### 1.3 脏数据分类（实测，必须处理）

| 现象 | 判别 | 处理 |
|---|---|---|
| 未匹配 TMDB | `status == "1"` 且 `number_of_episodes == 0` | 见 §3 重新匹配 |
| 本地集数 > TMDB 集数 | 重器 20/48、百花杀 36/38、父母爱情 44/45 | **不算缺**，标"本地多出" |
| 季数字段是脏的 | 黑冰 `number_of_seasons=1`、`local_number_of_seasons=10` | 不读季数字段 |
| 整季缺失 | 欢乐颂 TMDB 201 / 本地 42 | 单独归类"整季缺失" |

---

## 2. 需要新增的客户端方法

### 2.1 `fnos_media_import/media/fnos.py` → `FnosMediaRefresher`

复用现有的 `_request()`（已实现登录、token 刷新、authx 签名、签名模式回退）。

```python
API_ITEM_LIST    = "/v/api/v1/item/list"
API_ITEM_DETAIL  = "/v/api/v1/item/{guid}"
API_SEASON_LIST  = "/v/api/v1/season/list/{guid}"
API_EPISODE_LIST = "/v/api/v1/episode/list/{guid}"

def library_items(self, library_guid, *, page=1, page_size=100, media_types=None) -> dict
    # POST，body: {ancestor_guid, tags:{type:[...]}, sort_type:"DESC",
    #              sort_column:"create_time", exclude_grouped_video:1, page, page_size}
    # 返回 {"success", "items", "total", "page", "pages", "message"}

def season_list(self, show_guid) -> dict      # GET，返回 {"success","items":[...]}

def episode_list(self, season_guid) -> dict   # GET，返回 {"success","items":[...]}
```

要求：

- 失败时返回 `{"success": False, "code": ..., "message": ...}`，**不要抛裸异常**。
- `page_size` 上限未确认，先用 100，被截断则按实际返回条数继续翻页（以 `len(items) < page_size` 或 `已取 >= total` 作为终止条件）。

### 2.2 `fnos_media_import/organizer/tmdb.py` → `TmdbClient`

新增精确匹配（现有只有 `search` / `details` / `season_episodes`）：

```python
def find_by_imdb(self, imdb_id: str, media_type: str = "tv") -> int | None
    # GET /find/{imdb_id}?external_source=imdb_id
    # 从 data["tv_results"] / data["movie_results"] 取第一个 id
```

`details()` 已包含 `status`、`seasons[]`、`last_episode_to_air`，`season_episodes()` 已包含 `air_date`，直接复用。

---

## 3. TMDB 匹配策略（**全部重新查询**）

不采用飞牛里已有的匹配结果。按顺序尝试：

```
1. show.imdb_id 非空            → find_by_imdb()  → confidence = "exact"
2. title + year 搜索            → search(title, "tv")，用 score_tmdb_result() 打分
                                  最高分 >= 0.85     → "scored"
                                  0.60 <= 分数 < 0.85 → "weak"（进待确认区）
                                  < 0.60             → 继续
3. 都失败                       → confidence = "none"，归入「TMDB 无此条目」
```

- `score_tmdb_result()` 已存在于 `organizer/tmdb.py`，**直接调用，不要重写**。
- 年份取 `first_air_date` 或 `release_date` 的前 4 位；缺失时不参与打分。
- 若 `imdb_id` 命中但 TMDB 返回的类型不匹配（如电影），回退到标题匹配。
- 允许 `--overrides overrides.json` 手工指定：`{"剧名": 12345}`，优先级最高。

---

## 4. 审计算法

```python
TODAY = date.today()

def audit_show(show, fnos, tmdb) -> Row:
    match = resolve_tmdb(show)                       # §3
    if match.confidence == "none":
        return Row(category="TMDB无此条目", ...)

    detail = tmdb.details(match.tmdb_id, "tv")
    aired: set[tuple[int, int]] = set()
    for season in detail["seasons"]:
        number = season["season_number"]
        if number == 0 and not include_specials:
            continue
        for ep in tmdb.season_episodes(match.tmdb_id, number):
            if is_aired(ep, detail["status"]):
                aired.add((number, ep["episode"]))

    local: set[tuple[int, int]] = set()
    for season in fnos.season_list(show.guid)["items"]:
        if season["season_number"] == 0 and not include_specials:
            continue
        for ep in fnos.episode_list(season["guid"])["items"]:
            local.add((int(ep["season_number"]), int(ep["episode_number"])))

    missing = aired - local          # 缺集
    surplus = local - aired          # 本地多出
    return classify(detail["status"], aired, local, missing, surplus)
```

**`is_aired`：**

```python
if ep["air_date"]:
    return ep["air_date"] <= TODAY
# air_date 为空时的兜底
return detail["status"] == "Ended"     # 已完结 → 视为已播出；连载中 → 视为未播出
```

### 分类规则

| 条件（按顺序判断） | 分类 |
|---|---|
| 匹配失败 | `TMDB无此条目` |
| `missing` 非空且占总集数 ≥ 50% | `缺集·整季缺失` |
| `missing` 非空且全在尾部连续 | `缺集·落后` |
| `missing` 非空（中间有洞） | `缺集·中间缺集` |
| `missing` 为空且 TMDB `status == "Ended"` | `正常完结` |
| `missing` 为空且 TMDB 仍在更新 | `待完结` |

`surplus` 不影响分类，只在报告里追加说明（"本地多 N 集"）。

---

## 5. CLI 规格

```bash
python scripts/audit_library.py [选项]

--library NAME        库名，默认「电视剧」
--library-guid GUID   直接指定库 guid，跳过名称解析
--csv PATH            导出 CSV
--json PATH           导出 JSON
--include-specials    把 season 0（特别篇）计入
--include-surplus     详细列出"本地多出"的集号
--overrides PATH      手工匹配表 {"剧名": tmdb_id}
--cache-dir PATH      TMDB 响应缓存目录，默认 data/tmdb_cache
--cache-ttl HOURS     缓存有效期，默认 24
--concurrency N       飞牛并发，默认 4（TMDB 并发固定 1 + 限速）
--limit N             只扫前 N 部，用于调试
--dry-run             只拉数据不请求 TMDB，打印将审计的条目数
--verbose             打印每个请求的进度
```

**配置来源（按优先级）：**

1. 命令行参数
2. 环境变量 `FNOS_SERVER_URL` / `FNOS_USERNAME` / `FNOS_PASSWORD` / `TMDB_TOKEN`
3. 应用现有配置：`load_config()` + 数据库 `advanced_config` 覆盖项（`persisted_config_from_settings`）

**退出码：** `0` 成功；`1` 参数/配置错误；`2` 至少一项审计失败（部分成功）；`3` 全部失败。

---

## 6. 输出格式

### 终端表格（按缺集数降序，缺集在前）

```
TMDB ID      剧名          分类            已播/总   本地  说明
tt1234567    大宅门        缺集·落后       72/72     40    缺 S01E41-E72
tt7654321    欢乐颂        缺集·整季缺失   201/201   42    缺 S02-S05
tt271016     九门          待完结          30/30     30    -
tt1111111    父母爱情      正常完结        44/44     45    本地多 1 集
-            伟大的长征    TMDB无此条目    -         14    -

合计 125 部 · 正常完结 80 · 待完结 23 · 缺集 12 · 无此条目 3 · 待确认匹配 7
```

### CSV / JSON 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `tmdb_id` | int/null | TMDB 编号 |
| `title` | str | 剧名（TMDB 名优先，回退本地名） |
| `local_title` | str | 飞牛里的原名 |
| `category` | str | 六类之一 |
| `tmdb_status` | str | `Ended` / `Returning Series` |
| `aired` | int | 已播出集数 |
| `total` | int | TMDB 总集数 |
| `local` | int | 本地集数（行数，去重后） |
| `missing` | list | `["S01E13", "S02E05"]` |
| `surplus` | list | 本地多出的集号 |
| `confidence` | str | `exact` / `scored` / `weak` / `none` |
| `guid` | str | 飞牛剧 guid，便于回溯 |

**安全：报告中禁止出现 TMDB token、飞牛密码、Cookie。**

---

## 7. 工程约束

1. **不新增依赖**。只用 `requests`（已有）、`csv`、`json`、`concurrent.futures`。
2. **TMDB 必须限速**：令牌桶 40 次 / 10 秒，超出即等待。
3. **TMDB 必须缓存**：以 `tmdb_id + season` 为 key 存 JSON 到 `--cache-dir`，默认 24h。
   第二次跑同一批剧应几乎不发起 TMDB 请求。
4. **飞牛并发 4**，失败重试 2 次（指数退避 1s / 2s），仍失败则该项标记 `失败` 并继续。
5. **进度输出**：`[12/125] 美人余 ... 缺集·中间缺集`，在 `--verbose` 时打印。
6. **日志**：默认只输出报告；`--verbose` 额外写 `logs/audit_library.log`。
7. 全程只读，**不得调用** `mdb/scan`、`task/stop` 等写操作。

---

## 8. 测试要求

### 8.1 离线夹具（必须，不联网）

把真实响应存成 `tests/fixtures/fnos_*.json`，至少覆盖：

| 夹具 | 来源 | 断言 |
|---|---|---|
| `item_list_meirenyu.json` | 美人余列表行 | 解析出 `number_of_episodes=28`、`local_number_of_episodes=14` |
| `season_list_meirenyu.json` | 季列表 | 1 季，guid 正确 |
| `episode_list_meirenyu.json` | 分集列表 | **13 行，集号 1..12 + 14** |
| `item_list_jiunen.json` | 九门 | 30 集 |
| `episode_list_jiunen.json` | 分集列表 | 30 行，1..30 连续 |

### 8.2 必须通过的用例

| # | 场景 | 期望 |
|---|---|---|
| 1 | 美人余（缺 S01E13） | `缺集·中间缺集`，`missing == ["S01E13"]` |
| 2 | 九门（30/30，连载中） | `待完结`，`missing` 为空 |
| 3 | 虚构：TMDB 44 集、本地 45 集 | `正常完结`，`surplus` 长度 1，**不报缺集** |
| 4 | 虚构：TMDB 仍在更新、本地已跟上 | `待完结` |
| 5 | 虚构：TMDB 未播出集数被排除 | 未播出的集不进 `aired` |
| 6 | 虚构：整季缺失 | `缺集·整季缺失` |
| 7 | 无 `imdb_id`、标题可搜到 | `confidence == "scored"` 并正常审计 |
| 8 | TMDB 搜不到 | `TMDB无此条目`，不抛异常 |

### 8.3 真实环境验收（有 NAS 权限时）

```bash
python scripts/audit_library.py --library 电视剧 --limit 20 --verbose
```

必须满足：

- 不崩溃、无 traceback
- 美人余出现在结果中且被判为缺集（缺 S01E13）
- 九门出现在结果中且为待完结
- 总数的 `scanned` 与 `item/list` 的 `total` 一致

---

## 9. 明确不做

- 不自动补集、不触发下载、不调飞牛扫描接口
- 不写回飞牛媒体库
- 不做成 MCP 工具
- 不做增量对比（第一版）
- 不处理电影库（只处理 `type == "TV"`）

---

## 10. 交付前自检清单

- [ ] `ruff check` 通过
- [ ] 离线夹具测试全部通过
- [ ] 美人余用例输出 `缺 S01E13`
- [ ] 本地多于 TMDB 的样本不被报成缺集
- [ ] 未播出集不被报成缺集
- [ ] TMDB 命中失败时不抛异常
- [ ] 报告与日志中无 token / 密码 / Cookie
- [ ] 第二次运行命中缓存（日志可见 `cache hit`）
- [ ] `--csv` / `--json` 字段与 §6 一致
- [ ] 退出码符合 §5
- [ ] `verification.md` 增加一节记录
