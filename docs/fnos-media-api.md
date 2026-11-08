# 飞牛影视（trimemedia）接口记录

本文件只记录**已实测确认**的接口与字段语义，供后续实现「库内条目遍历 / 缺集审计」用。
未确认的部分集中在最后一节，不要按猜测实现。

- 服务地址：`http://<NAS>:5666`（示例 `10.188.188.3:5666`）
- 网关前缀：`/v/api/v1/...`；直连视频服务时去掉 `/v`（本项目 `FnosMediaRefresher._direct_api`）
- 响应封装：`{"code": 0, "msg": "", "data": ...}`，`code == 0` 为成功
- 认证：`_authx` 签名头 + token；本项目 `FnOSMediaRefresher._request()` 已实现（含 token 失效重登）

---

## 1. 已确认端点

### 1.1 `POST /v/api/v1/item/list` — 库内条目分页（2026-11-04 实测）

请求体：

```json
{
  "ancestor_guid": "e6e16f23d8384a428dd81bbb11fc4147",
  "tags": {"type": ["Movie", "TV", "Directory", "Video"]},
  "sort_type": "DESC",
  "sort_column": "create_time",
  "exclude_grouped_video": 1,
  "page": 1,
  "page_size": 50
}
```

响应：

```json
{
  "msg": "",
  "code": 0,
  "data": {
    "mdb_name": "",
    "mdb_category": "",
    "top_dir": "",
    "dir": "",
    "total": 125,
    "list": [ /* 条目数组，本次 50 条 */ ],
    "jump_list": null
  }
}
```

要点：

- `ancestor_guid` 是**库/顶层目录的 guid**（电视剧库为 `e6e16f23d8384a428dd81bbb11fc4147`）。
- 分页字段只有 `total`，**没有** `page` / `pages`；总页数需自行 `ceil(total / page_size)` 计算。
- 返回结果**混类型**：同一次请求里同时出现 `type: "TV"` 和 `type: "Directory"`。
  `Directory` 条目带 `path`（如 `/vol1/1000/media/strm/电视剧`），说明该库是 STRM 目录结构。
- `tags.type` 看起来是**候选集合**而非精确过滤：传了四个类型，返回里仍然只有 TV 与 Directory。

### 1.2 `GET /v/api/v1/search/list?q=<keyword>` — 关键词检索（2026-11-03 实测）

返回 `data` 为条目数组，字段与 `item/list` 的单条一致。
实测 `q=繁花` 返回「繁花（TV, 30 集本地 30）」「繁花 特别篇（Episode, 本地 0）」「繁花剧场版（TV, 本地 5）」。

用途：已知片名时的精确查重；**不能**用于遍历全库。

### 1.3 `GET /v/api/v1/item/{guid}` — 条目详情（2026-11-04 实测）

`guid = 8cf6ab48777d4e33a4ab62dc0129e892`（九门）：

```json
{"code": 0, "data": {
  "guid": "...", "title": "九门", "original_title": "九门", "type": "TV",
  "imdb_id": "tt37118307", "trim_id": "tt271016",
  "season_number": 0, "episode_number": null,
  "number_of_seasons": 1, "number_of_episodes": 30,
  "local_number_of_seasons": 1, "local_number_of_episodes": 30,
  "can_play": 1, "play_error": "", "play_item_guid": "",
  "genres": [30, 7], "production_countries": ["CN"],
  "media_stream": {"resolutions": ["4k"], "audio_type": ["DolbyAtmos","DTS","Stereo"],
                   "color_range_type": ["HDR10","SDR"]}
}}
```

注意：详情**没有** `status`、`first_air_date`、`last_air_date`、`single_child_guid`
（这些只在 `item/list` 里），所以判断是否完结仍需列表数据或 TMDB。

### 1.4 `GET /v/api/v1/episode/list/{guid}` — 分集列表（2026-11-04 实测）

`guid = b890942b4f214e25a5b28344b2fe2960`（九门的 `single_child_guid`，即「第 1 季」）：

```json
{"code": 0, "data": [ {"guid": "...", "type": "Episode", "tv_title": "九门",
  "parent_guid": "b890942b...", "parent_title": "第 1 季",
  "season_number": 1, "episode_number": 1, "title": "百人部队深夜离奇失踪",
  "runtime": 39, "air_date": "2026-07-30", "duration": 0,
  "video_guid": "", ...} ]}
```

实测要点（**很重要，直接影响审计可行性**）：

- 入参是**季的 guid**（来自 `season/list` 或剧条目的 `single_child_guid`），不是剧的 guid。
- 返回**裸数组**，无分页字段。
- **缺集时该集不会有行**。实测「美人余」：TMDB 28 集、本地 13 行，
  `episode_number` 依次是 1..12、**14**——第 13 集直接不存在。
  这说明列表反映的是**本地已有的分集**，不是按 TMDB 铺满的占位行，
  **可以直接用来判缺集**。
- 分集行的 `number_of_episodes` / `local_number_of_episodes` / `status` 全是 `0` / `"1"`，
  **不可用**；只有 `episode_number`、`season_number`、`air_date`、`title` 有意义。
- `video_guid` / `duration > 0` 只出现在部分集（九门 30 集里只有 4 集有），
  **不能**用来判断"文件是否存在"。
- `title` 为空是正常现象（未抓到分集标题），**不代表缺集**。

### 1.5 `GET /v/api/v1/season/list/{guid}` — 季列表（2026-11-04 实测）

`guid = e712c212be0c4353aa28f5c9afd8723b`（美人余）：

```json
{"code": 0, "data": [{
  "guid": "4fca29c0975640dd8ae74a22265917d9",
  "type": "Season", "title": "第 1 季",
  "parent_guid": "e712c212...", "parent_title": "",
  "season_number": 1,
  "episode_number": 28,          // 该季 TMDB 总集数（存在这个字段里）
  "number_of_episodes": 0,       // 不可用
  "local_number_of_episodes": 13 // 与分集行数一致
}]}
```

- **入参是剧的 guid**，返回裸数组（无分页）。
- 这是多季剧遍历的正确入口：剧 → 季 → 集。
- 注意 `episode_number` 在这一行表示"该季总集数"，不是集号。

### 1.6 其余已封装端点

| 端点 | 用途 |
|---|---|
| `POST /v/api/v1/login` / `POST /v/api/v2/user/loginByPassword` | 登录换 token |
| `GET /v/api/v1/mediadb/list` | 媒体库列表（含 guid、海报、计数） |
| `GET /v/api/v1/mediadb/sum` | 媒体库统计 |
| `GET /v/api/v1/mdb/list` | 刷新用库配置（含 `dir_list` 真实挂载路径） |
| `GET /v/api/v1/mdb/scan/{guid}` | 触发库扫描 |
| `GET /v/api/v1/task/running` | 运行中的刷新任务 |

---

## 2. 条目字段（`item/list` 与 `search/list` 共用）

| 字段 | 示例 | 说明 |
|---|---|---|
| `guid` | `1a448a60...` | 条目唯一标识 |
| `title` | `灵魂摆渡` | 标题；Episode 行常为空，用 `tv_title` |
| `type` | `TV` / `Movie` / `Episode` / `Directory` / `Video` | 条目类型 |
| `number_of_seasons` | `3` | TMDB 季数 |
| `number_of_episodes` | `53` | **TMDB 总集数**（缺集审计基准） |
| `local_number_of_seasons` | `3` | 本地季数（**不可信**，见 §3.3） |
| `local_number_of_episodes` | `13` | **本地集数**（缺集审计基准） |
| `status` | `Ended` / `Returning Series` / `1` | 见 §3.1 |
| `release_date` / `first_air_date` / `last_air_date` | `2014-02-28` | 播出日期 |
| `season_number` / `episode_number` | `0` / `0` | 单条季集定位，剧级行通常为 0 |
| `parent_guid` / `parent_title` | | 父级（季或剧），Episode 行有值 |
| `tv_title` | `繁花` | Episode 行对应的剧名 |
| `single_child_guid` | `4fca29c0...` | 疑似子项入口，见 §4 |
| `ancestor_guid` / `ancestor_name` / `ancestor_category` | `...` / `电视剧` / `TV` | 所属库 |
| `path` | `/vol1/1000/media/strm/电视剧` | 仅 `Directory` 条目有 |
| `poster` / `poster_width` / `poster_height` | | 海报 |
| `media_stream.resolutions` | `["4k"]` | 分辨率标记 |
| `imdb_id` / `trim_id` | `tt13173850` / `tt75480` | 外部 id；`trim_id` 有 `tt` / `ittt` / `dt` 多种前缀 |
| `watched` / `is_favorite` / `vote_average` | | 观看状态与评分 |
| `overview` | | 简介（较长，投影时需截断） |

---

## 3. 实测发现的数据质量问题

这些直接决定审计规则，**不处理会大量误报**。

### 3.1 `status: "1"` 表示未匹配到 TMDB

样本中「伟大的长征」「忠诚」「流星蝴蝶剑」「西游记后传」「隋唐英雄传」「星辰预言我爱你」
同时满足：`status == "1"`、`number_of_seasons == 0`、`number_of_episodes == 0`、`local_number_of_episodes > 0`。

结论：这类条目**没有 TMDB 基准**，无法判断缺集，审计时必须跳过并单独计数。

### 3.2 本地集数可能大于 TMDB 集数

| 剧 | TMDB | 本地 |
|---|---|---|
| 重器 | 20 | 48 |
| 百花杀 | 36 | 38 |
| 父母爱情 | 44 | 45 |

原因可能是多版本、花絮、或 TMDB 季数据不全。
**规则：只在 `local < total` 时计入缺集，`local >= total` 一律视为完整。**

### 3.3 `local_number_of_seasons` 不可信

「黑冰」：`number_of_seasons=1`、`local_number_of_seasons=10`、`local_number_of_episodes=20`。
季数明显是脏数据，**审计只用集数，不用季数**。

### 3.4 长剧的缺集可能是整季缺失

「欢乐颂」：TMDB 5 季 201 集，本地 5 季 42 集 → 差 159 集。
这类不是"零散缺几集"，而是"只有一季"，报告里应区分「整季缺失」与「零散缺集」。

### 3.5 剧级 `local_number_of_episodes` 与会话数对不上

「美人余」：

| 层级 | `local_number_of_episodes` | 实际行数 |
|---|---|---|
| 剧（`item/list`） | 14 | — |
| 季（`season/list`） | 13 | — |
| 集（`episode/list`） | — | **13** |

剧级比实际多 1，季级和实际一致。

**结论：集数以 `episode/list` 的行数为准，不要用剧级/季级的计数字段。**
这条也意味着"缺集判定必须真的把分集拉下来数"，不能只靠 `local` 字段省请求。

---

## 4. 已确认 / 待确认

### 已确认（2026-11-04）

| 问题 | 结论 |
|---|---|
| 缺集时分集列表是否补占位行 | **不补**。美人余缺第 13 集，列表里就没有 13 这一行 |
| 多季剧怎么列季 | `GET /season/list/{剧guid}` 返回季数组，再逐季 `episode/list/{季guid}` |
| 层级遍历链 | 库 → `item/list` → 剧 → `season/list` → 季 → `episode/list` → 集 |

### 仍待确认

1. **`tags.type` 精确过滤**：只传 `["TV"]` 时，`total` 是否下降、`Directory` 是否消失。
2. **`page_size` 上限**：试 200，确认是否被截断。
3. **最后一页**：`total=125 / page_size=50` 时第 3 页是否正好 25 条。
4. **`mediadb/list` 的 guid 与 `ancestor_guid` 是否同一个值**（决定工具入参能否复用库 guid）。
5. **POST 的签名模式**：`item/list` 是 POST + JSON body，需确认与登录请求同一套签名可用。
6. **`exclude_grouped_video`** 的语义。
7. **`status` 的完整取值集合**（目前见到 `Ended` / `Returning Series` / `1`）。

---

## 5. 缺集审计规则草案

基于以上数据，计划规则：

1. 只审计 `type == "TV"`，跳过 `Directory` / `Video` / `Episode`。
2. 跳过未匹配 TMDB 的条目：`number_of_episodes <= 0`。
3. 连载中不报缺集：`status == "Returning Series"` 单独归类为「追更中」。
4. 只在 `local_number_of_episodes < number_of_episodes` 时计入缺集。
5. 差集占 TMDB 集数 ≥ 80% 时标记为「整季缺失」，否则「零散缺集」。
6. 同时输出无法审计的条目（未匹配 TMDB），避免用户误以为全库已覆盖。

报告字段草案：

```json
{
  "library": "电视剧",
  "scanned": 125,
  "audited": 96,
  "complete": 80,
  "missing": 16,
  "skipped": {"airing": 9, "unmatched_tmdb": 11},
  "items": [
    {"title": "蜗居", "total": 35, "local": 33, "missing_count": 2, "kind": "sparse"},
    {"title": "欢乐颂", "total": 201, "local": 42, "missing_count": 159, "kind": "season"}
  ]
}
```
