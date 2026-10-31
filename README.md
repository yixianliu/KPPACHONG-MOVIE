# 魔法国（mofaguo.cn）影视数据爬虫

针对 `https://www.mofaguo.cn/gangju/`（魔法国 / 欧克TV，苹果CMS-STUI 模板）的全量影视数据抓取模块。
抓取三类核心数据：**影视基础元信息**、**图片资源**、**播放源地址与线路信息**，经清洗校验后按
既有 `film_cms` 库表结构写入 MySQL。

## 1. 代码结构

```
mofaspider/
  config.py    # 从 .env 加载 [DATABASE]/[SPIDER]/[MOFAGUO]，连接信息绝不硬编码
  logger.py    # 控制台 + 滚动文件日志（级别走配置）
  db.py        # PyMySQL 连接 + upsert/insert/update 封装（表名为代码常量）
  http.py      # 防封 HTTP 客户端：UA 轮换 / 代理 / 限速抖动 / 指数退避重试
  parser.py    # 列表 / 详情 / 播放页解析（围绕已探明的真实 DOM）
  images.py    # 图片下载落盘（类型 + 体积校验，返回相对公开路径）
  models.py    # Film / PlaySource / Episode 数据类 + 规范化校验（按表约束截断）
  pipeline.py  # 去重 upsert 影片，并维护播放源/分集/属性/主演维度（单事务）
  state.py     # 增量状态（按 source_url + 更新日期幂等，支持时间窗）
  crawler.py   # 编排：分类 → 列表分页 → 详情 → 图片 → 直链 → 落库
run.py         # 命令行入口
requirements.txt
.env           # 配置（数据库/网络/站点），连接信息仅在此处
```

## 2. 配置（.env）

所有数据库连接信息来自 `[DATABASE]`，站点与抓取策略来自 `[SPIDER]` 与 `[MOFAGUO]`：

```ini
[DATABASE]
HOSTNAME=127.0.0.1
DATABASE=film_cms
USERNAME=root
PASSWORD=root
HOSTPORT=3306
CHARSET=utf8mb4

[MOFAGUO]
BASE_URL=https://www.mofaguo.cn
CATEGORIES=movie:1,tv:2,variety:3,anime:4      # 本地类型枚举 -> 站点 news 列表编号
EXTRACT_PLAY_URL=true                          # 深入 /view/ 提取真实 m3u8/mp4 直链
MAX_EPISODES_PER_FILM=0                        # 单片最多提取分集直链(0=全部)
SINCE_HOURS=0                                  # 增量：仅采集该小时数内的更新(0=全量)
CATEGORY_ID_MAP=movie:1,tv:2,anime:3,documentary:4,variety:5
# 以及 [SPIDER] 中的并发/超时/重试/限速/代理/图片下载等参数
```

> 说明：代码中**不出现任何数据库账号密码**；表名（`ba_film` 等）为代码常量，非连接机密。

## 3. 数据库表映射

| 目标表 | 写入内容 |
| --- | --- |
| `ba_film` | 影片主记录（片名/导演/主演/类型/地区/年份/简介/评分/封面/来源地址等），按 `source_url` 去重 upsert |
| `ba_film_play_source` | 播放线路（线路名、`play_from` 线路标识、`play_type`） |
| `ba_film_episode` | 分集（标题、序号、直链 `play_url`、备用地址、是否免费） |
| `ba_film_attribute` / `ba_film_attribute_relation` | 地区/语言/年份/标签/主演 维度字典与关联 |
| `ba_film_star` | 主演维度行（确保存在） |

分类映射：`movie→1, tv→2, anime→3, documentary→4, variety→5`（取自 `ba_film_category` 顶层）。

## 4. 运行

```bash
pip install -r requirements.txt

# 全量抓取全部分类（图片 + 直链提取）
python run.py

# 仅电影分类，前 2 页，每片最多 3 条分集直链，不下载图片（调试/快速验证）
python run.py --types movie --pages 2 --max-episodes 3 --no-images

# 仅采集最近 48 小时更新的影片（增量）
python run.py --since-hours 48

# 强制重抓（忽略幂等跳过）；限制每类最多 5 部（生产限流/冒烟）
python run.py --force --limit 5

# 不深入提取直链，仅保留播放页地址
python run.py --no-extract
```

常用参数：`--types`（movie,tv,variety,anime）、`--pages`、`--workers`、`--max-episodes`、
`--limit`、`--no-images`、`--no-extract`、`--force`、`--since-hours`、`--log-level`、`--env`。

运行统计与错误均输出到控制台与 `runtime/spider/log/mofa_spider.log`；进度（幂等/增量）记录于
`runtime/spider/state/mofa_state.json`。

## 5. 生产级特性

- **请求防封**：随机 UA 轮换、可选 HTTP/HTTPS 代理、全局限速 + 随机抖动（降低规律性）。
- **异常重试**：对网络异常与 429/5xx 状态码做指数退避重试。
- **数据去重**：以详情页地址 `source_url` 为外部唯一键做 upsert；增量模式按更新日期幂等跳过。
- **增量更新**：`--since-hours` 时间窗 + 状态文件，中断可续跑，避免重复抓取。
- **清洗校验**：按各表 `varchar` 长度截断、`enum` 枚举校正、`score/year` 范围校正、主演/标签去重，
  并自动生成 SEO 字段。
- **完整日志与异常捕获**：单影片失败仅记日志并计数，不中断整体；结束输出统计。

## 6. 已知约束 / 说明

- 播放直链来自 `/view/{id}-{src}-{ep}.html` 内 `var player_aaaa` 的 `url` 字段（m3u8/mp4），
  站点改版需同步更新 `parser.parse_view`。
- 中文片名首字母按库规范归为 `#`（苹果CMS 既有行为）。
- `ba_film_star.film_count` 为冗余统计，本爬虫仅保证主演维度行存在，不实时重算计数。
- 列表分页规律为 `/gangju/news/{type}-{page}.html`，已在 `.env` 模板中参数化。
