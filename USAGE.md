# 魔法国影视爬虫 · 使用文档

> 项目路径：`KPPACHONG-MOVIE/mofaspider`
> 目标站点：mofaguo.cn（苹果CMS 架构）
> 运行入口：`run.py`

本文档面向**运维与二次开发**，覆盖环境准备、配置、运行、存储规范、反爬策略与故障排查。

---

## 1. 功能简介

- 列表 → 详情 → 分集直链三级采集，自动落库到 `FilmCms`（`ba_film` 等表）。
- **图片按影片名拼音统一命名**，落盘到 Web 静态目录，数据库仅存标准相对路径。
- **去重更新**：以 `source_url` 为外部唯一键，先查后写——存在则全字段覆盖、不存在则新增，事务内 `FOR UPDATE` 保证原子性。
- **反爬体系**：UA 轮换、代理池、自适应限速、指数退避（尊重 `Retry-After`）、反爬挑战识别。
- **增量/全量/限流**多种运行模式，单部失败不中断整体，结束输出统计。

---

## 2. 运行环境

| 项目 | 要求 |
|---|---|
| Python | **≥ 3.10**（本机 3.13.x 已验证） |
| 数据库 | MySQL / MariaDB（与 FilmCms 同库） |
| 网络 | 可访问目标站；如需代理请自备代理服务 |
| 磁盘 | 图片落盘目录需可写（默认 `D:\phpstudy_pro\WWW\FilmCms\public\static\images\film-cover`）|

### 2.1 安装依赖

```bash
# 运行依赖
pip install -r requirements.txt

# 开发/测试依赖（可选，用于跑单测与覆盖率）
pip install -r requirements-dev.txt
```

核心依赖及安全下限：

| 依赖 | 建议下限 | 说明 |
|---|---|---|
| requests | >=2.32.0 | >=2.32 修复 CVE-2024-35195 |
| urllib3 | >=2.2.0 | >=2.2 修复 CVE-2024-37891 |
| beautifulsoup4 | >=4.12.0 | HTML 解析 |
| pymysql | >=1.1.0 | MySQL 驱动 |
| pypinyin | >=0.50.0 | 封面拼音命名 |
| lxml | >=5.0.0 | 更快的 HTML 解析（自动回退 html.parser）|

---

## 3. 配置（`.env`）

配置文件位于项目根目录 `.env`（不存在时出错退出）。结构为三段：`[DATABASE]`、`[SPIDER]`、`[MOFAGUO]`。
所有敏感信息（数据库密码、代理账号）均存放在此文件，**请勿提交到版本库**。

### 3.1 最小可用配置示例

```ini
[DATABASE]
HOSTNAME=127.0.0.1
DATABASE=film_cms
USERNAME=root
PASSWORD=root
HOSTPORT=3306
CHARSET=utf8mb4

[SPIDER]
# 封面图片存储：按影片名拼音命名，落盘到 FilmCms 静态资源目录
IMAGE_DOWNLOAD=true
IMAGE_BASE_DIR=D:\phpstudy_pro\WWW\FilmCms\public\static\images\film-cover
IMAGE_PUBLIC_PREFIX=/static/images/film-cover
IMAGE_MAX_SIZE=10
IMAGE_ALLOWED_TYPES=image/jpeg,image/png,image/webp,image/gif
# IMAGE_FALLBACK_TO_URL=false  # 非规范兜底（调试用，默认关：失败/禁用时封面字段置空，绝不写原图 URL）

[MOFAGUO]
BASE_URL=https://www.mofaguo.cn
# 分类：业务类型枚举 -> 站点 type 编号
CATEGORIES=movie:1,tv:2,variety:3,anime:4
# 分类映射：业务类型枚举 -> ba_film_category.id
CATEGORY_ID_MAP=movie:1,tv:2,anime:3,documentary:4,variety:5
```

### 3.2 配置项完整参考

#### `[DATABASE]`
| 键 | 默认 | 说明 |
|---|---|---|
| HOSTNAME | —（必填）| 数据库主机 |
| DATABASE | —（必填）| 数据库名 |
| USERNAME | —（必填）| 账号 |
| PASSWORD | —（必填）| 密码 |
| HOSTPORT | 3306 | 端口 |
| CHARSET | utf8mb4 | 字符集 |
| DEBUG | false | 是否打印 SQL |
| PREFIX | （空）| 表前缀（如 `ba_`）。代码默认表名已含 `ba_`，此项一般留空 |

#### `[SPIDER]`（请求 / 图片 / 日志）
| 键 | 默认 | 说明 |
|---|---|---|
| SPIDER_PAGE_SIZE | 20 | 每页条目数（参考） |
| SPIDER_MAX_PAGES | 0 | 每分类最大页（0=全部） |
| SPIDER_CONCURRENCY | 4 | 并发线程数 |
| SPIDER_REQUEST_TIMEOUT | 30 | 单请求超时（秒） |
| SPIDER_RETRY_TIMES | 3 | 失败重试次数（429/5xx/网络异常） |
| SPIDER_RETRY_BACKOFF | 2.0 | 退避基数（秒，指数增长） |
| SPIDER_REQUEST_INTERVAL | 1.0 | 基础请求间隔（秒），自适应在此之上乘倍率 |
| SPIDER_USER_AGENT | 内置 Chrome UA | 可自定义固定 UA（实际运行时仍随机轮换内置池） |
| SPIDER_PROXY | （空）| 单代理地址，如 `http://user:pass@1.2.3.4:8080` |
| SPIDER_PROXY_POOL | （空）| 代理池，逗号分隔，逐请求随机轮换（与单代理合并去重） |
| VERIFY_SSL | false | 是否校验 TLS 证书（源站自签/证书链不完整时关） |
| ADAPTIVE_INTERVAL | true | 依据 429/5xx 自适应放宽/收敛限速 |
| IMAGE_DOWNLOAD | true | 是否下载封面 |
| IMAGE_BASE_DIR | `D:\phpstudy_pro\WWW\FilmCms\public\static\images\film-cover` | 图片落盘目录 |
| IMAGE_PUBLIC_PREFIX | `/static/images/film-cover` | 写入数据库的公开相对前缀 |
| IMAGE_TIMEOUT | 30 | 图片下载超时（秒） |
| IMAGE_CONCURRENCY | 4 | 图片并发（预留） |
| IMAGE_MAX_SIZE | 10 | 单图体积上限（MB） |
| IMAGE_ALLOWED_TYPES | jpeg/png/webp/gif | 允许的 Content-Type |
| IMAGE_FALLBACK_TO_URL | false | 非规范兜底：下载失败/禁用时是否写原图 URL（**默认 false，绝不写原图 URL**） |
| LOG_LEVEL | INFO | 日志级别 DEBUG/INFO/WARNING/ERROR |
| LOG_DIR | runtime/spider/log | 日志目录 |
| STATE_DIR | runtime/spider/state | 增量状态文件目录 |

#### `[MOFAGUO]`（站点结构，改版需同步）
| 键 | 默认 | 说明 |
|---|---|---|
| BASE_URL | —（必填）| 站点根地址 |
| PATH_PREFIX | /gangju | 路径前缀 |
| CATEGORIES | movie:1,tv:2,variety:3,anime:4 | 业务类型 -> 站点 type 编号 |
| CATEGORY_ID_MAP | movie:1,tv:2,anime:3,documentary:4,variety:5 | 业务类型 -> `ba_film_category.id` |
| LIST_URL_TPL | /gangju/news/{type}.html | 列表首页模板 |
| LIST_URL_PAGE_TPL | /gangju/news/{type}-{page}.html | 列表分页模板 |
| DETAIL_URL_TPL | /gangju/news/dt/{id}.html | 详情页模板 |
| VIEW_URL_TPL | /gangju/view/{id}-{src}-{ep}.html | 分集直链页模板 |
| EXTRACT_PLAY_URL | true | 是否深入 /view/ 提取直链 |
| MAX_EPISODES_PER_FILM | 0 | 单片最多直链数（0=全部） |
| DETAIL_RETRY_TIMES | 3 | 详情页重试次数 |
| DEDUP_KEY | source_url | 去重键 |
| SINCE_HOURS | 0 | 增量时间窗（小时，0=不限制） |

---

## 4. 快速开始

```bash
# 全量抓取全部分类，下载图片 + 提取直链
python run.py

# 仅抓取电影，前 2 页，单片最多 3 条直链，不下载图片（快速验证）
python run.py --types movie --pages 2 --max-episodes 3 --no-images

# 强制重抓（忽略增量幂等）
python run.py --force

# 仅采集最近 48 小时更新的影片（增量模式）
python run.py --since-hours 48

# 限制每分类最多处理 50 部（生产限流 / 测试）
python run.py --limit 50

# 指定 .env 路径与日志级别
python run.py --env /path/to/.env --log-level DEBUG
```

运行结束会打印统计（pages / listed / processed / updated / inserted / failed 等）。

---

## 5. 命令行参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--env` | 根目录 `.env` | 指定配置文件路径 |
| `--types` | （空=全部）| 仅抓分类，逗号分隔：`movie,tv,variety,anime` |
| `--pages` | 0 | 每分类最大页数（0=全部） |
| `--workers` | 0 | 并发线程数（0=取 `SPIDER_CONCURRENCY`） |
| `--max-episodes` | 0 | 单片最多提取分集直链数（0=全部） |
| `--limit` | 0 | 每分类最多处理影片数（0=不限） |
| `--no-images` | false | 不下载图片 |
| `--no-extract` | false | 不提取直链，仅存播放页地址 |
| `--force` | false | 忽略增量幂等，强制重抓 |
| `--since-hours` | 0 | 仅处理该小时数内更新的影片 |
| `--log-level` | 取配置 | 日志级别覆盖 |

> 命令行参数优先级高于 `.env`（`--no-images` / `--no-extract` / `--since-hours` 会覆盖配置）。

---

## 6. 抓取模式说明

| 模式 | 触发方式 | 行为 |
|---|---|---|
| 全量 | `python run.py` | 遍历全部分类、全部分页 |
| 指定分类 | `--types movie,tv` | 只处理指定类型 |
| 限页 / 限量 | `--pages` / `--limit` | 控制抓取范围，便于灰度 |
| 增量 | `--since-hours N` | 仅处理最近 N 小时更新的影片（配合状态文件幂等跳过） |
| 强制重抓 | `--force` | 忽略“已存在且未变更”的跳过逻辑 |
| 直链开关 | `--no-extract` | 不深入 `/view/`，降低请求量 |

---

## 7. 图片存储规范（重要）

1. **命名规则**：`<影片名拼音>_<唯一标识>.<ext>`
   - 拼音由 `pypinyin` 生成（如 `后来的我们 → houlaidewomen`），英文/数字原样保留，`× # *` 等特殊字符统一剔除。
   - 唯一标识优先取源站 `site_id`，缺失时回退 `source_url` 短哈希，**避免重名冲突**。
   - 拼音转换异常时自动回退为「标题 md5」命名并记日志，绝不中断。
2. **落盘路径**：`IMAGE_BASE_DIR`（默认 `FilmCms/public/static/images/film-cover`）。
3. **数据库字段**：仅写入重命名后的标准相对路径（如 `/static/images/film-cover/houlaidewomen_12345.jpg`）。
4. **失败处理**：下载失败或未启用图片时，封面字段**置空**，绝不写入原图 URL 或本地临时路径（满足存储规范）。
   - 仅在调试时可通过 `IMAGE_FALLBACK_TO_URL=true` 临时回退为原图 URL（不推荐用于生产）。
5. **Windows 兼容**：路径用 `pathlib` 解析，磁盘用原生分隔符、数据库字段统一用 `/`。
6. **校验**：类型白名单 + 体积上限（默认 10MB）+ 完整日志（成功/失败/类型/体积/命名异常）。

---

## 8. 数据库去重与更新逻辑

- **唯一键**：以详情页地址 `source_url` 为外部唯一键（建议对 `ba_film.source_url` 建唯一索引以启用行级锁）。
- **流程**（事务内）：
  1. `SELECT ... WHERE source_url=%s FOR UPDATE` —— 加行锁，保证「查重→写库」原子性，避免并发双插/覆盖错乱。
  2. **已存在** → 以最新爬取数据**全字段覆盖更新**（`create_time` 保留，仅刷新 `update_time`）。
  3. **不存在** → **标准新增**（`create_time = update_time = now`）。
- **关联表**：主演、标签、分类、播放源/分集按 `(film_id, 名称/标识)` 做 upsert，重复运行幂等。
- **并发说明**：单进程内由 `_db_lock` 串行化写库；多进程/多机部署建议对 `source_url` 建唯一索引：
  ```sql
  -- 确认无重复 source_url 后执行（重复需先清洗）
  ALTER TABLE `ba_film` ADD UNIQUE KEY `uk_source_url` (`source_url`);
  ```

---

## 9. 反爬与网络策略

| 策略 | 实现 |
|---|---|
| UA 轮换 | 内置较新 UA 池（Chrome/Edge/Firefox/Safari/移动端），逐请求随机切换 |
| 代理池 | `SPIDER_PROXY_POOL`（逗号分隔）与 `SPIDER_PROXY` 合并去重，逐请求随机选路 |
| 自适应限速 | `ADAPTIVE_INTERVAL=true` 时遇 429/5xx 自动放宽间隔（最高 8×），成功后缓慢收敛 |
| 退避重试 | 对 429/500/502/503/504 与网络异常做指数退避，并**尊重 `Retry-After`**（秒数或 HTTP 日期） |
| 挑战检测 | 识别 403 / 验证码 / 人机校验页面，触发可插拔 `on_challenge` 回调后放弃该请求，避免空转 |
| TLS 校验 | `VERIFY_SSL` 可配置（源站证书异常时关闭） |

---

## 10. 日志与状态

- **日志**：滚动落盘 `runtime/spider/log/mofa_spider.log`（级别见 `LOG_LEVEL`），同时输出控制台。
- **增量状态**：`runtime/spider/state/mofa_state.json` 记录各分类已爬页码/时间，支持中断续跑。
- **结束统计**示例：
  ```
  ===== 运行统计 =====
    pages       : 12
    listed      : 240
    processed   : 240
    inserted    : 31
    updated     : 209
    failed      : 0
  ```

---

## 11. 测试

```bash
pip install -r requirements-dev.txt
python -m pytest test -q                     # 全量
python -m pytest test --cov=mofaspider       # 含覆盖率
```

测试覆盖：`images`（拼音命名/唯一性/特殊字符）、`models`（截断/枚举/SEO）、`pipeline`（查重→新增/覆盖更新，含 `FOR UPDATE` 与 `create_time` 保留）、`http`（Retry-After/代理池/挑战检测/成功失败路径）、`config`（.env 加载）。

---

## 12. 常见问题（FAQ）

**Q1：启动报 `配置加载失败: 缺少段落 [SPIDER]`**
A：根目录缺少 `.env` 或 `[SPIDER]` 段缺失。复制示例配置补齐三段即可。

**Q2：封面字段在数据库里是空的**
A：正常表现。下载失败/未启用图片时按规范置空；若需原图 URL 调试，临时设 `IMAGE_FALLBACK_TO_URL=true`。

**Q3：被目标站 403 / 验证码拦截**
A：日志会标记「疑似触发反爬挑战」。建议：配置 `SPIDER_PROXY_POOL` 分散 IP，或调大 `SPIDER_REQUEST_INTERVAL`、关闭 `ADAPTIVE_INTERVAL`；必要时接入打码平台（扩展 `on_challenge` 回调）。

**Q4：图片落盘目录无写入权限**
A：确认 `IMAGE_BASE_DIR` 指向的路径存在且进程可写；Windows 下注意路径分隔与权限。

**Q5：爬取很慢**
A：受 `SPIDER_REQUEST_INTERVAL` 与并发限制。可在合规前提下提高 `SPIDER_CONCURRENCY`、降低间隔；代理池可提升有效并发。

**Q6：站点改版导致解析为空**
A：爬取逻辑依赖苹果CMS 页面结构（`parse_listing` / `parse_detail` / `parse_view`）。结构变化需同步更新 `mofaspider/parser.py` 与 `.env` 中的 URL 模板。

---

## 13. 维护建议

1. **代理池健康检查**：随机轮换基础上，建议增加可用性探测（定时 HEAD 探活、按失败率加权）。
2. **打码/验证码应对**：通过 `on_challenge` 回调接入打码平台或告警（企业微信/钉钉）。
3. **增量调度**：结合 `crontab` 跑 `--since-hours` 增量，定期全量校准；状态文件建议加版本与体积保护。
4. **可观测性**：关注抓取速率、失败率、429 占比趋势，做阈值预警。
5. **合规与爬虫礼仪**：遵守目标站 `robots.txt`、控制 QPS、设置可识别的 UA 与联系信息，降低法律风险。
