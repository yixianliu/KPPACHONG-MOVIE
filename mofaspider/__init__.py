"""魔法国（mofaguo.cn）影视数据爬虫包。

模块职责：
  config   - 从 .env 加载 DATABASE/SPIDER/MOFAGUO 配置（连接信息绝不硬编码）
  logger   - 控制台 + 滚动文件日志
  db       - PyMySQL 连接与 upsert/insert/update 封装
  http     - 防封 HTTP 客户端（UA 轮换/代理/限速抖动/退避重试）
  parser   - 列表/详情/播放页解析
  images   - 图片下载落盘
  models   - 数据模型与规范化校验
  pipeline - 去重落库与维度维护（单事务）
  state    - 增量状态（幂等/时间窗）
  crawler  - 编排（分类分页 -> 详情 -> 图片 -> 直链 -> 落库）
"""
__version__ = "1.0.0"
