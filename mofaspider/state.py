"""增量状态管理：记录已成功落库的影片（source_url -> 更新日期），用于：

  - 幂等：相同 source_url 且更新日期未变 -> 跳过，避免重复写库
  - 增量：配合 SINCE_HOURS，整片更新日期早于时间窗 -> 跳过
  - 进度恢复：异常中断后重跑不会重复抓取已处理影片

状态以 JSON 存于 STATE_DIR/mofa_state.json。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .logger import get_logger

logger = get_logger()


class State:
    def __init__(self, state_dir: str, root: Path, since_hours: int = 0):
        self._dir = (root / state_dir).resolve() if not Path(state_dir).is_absolute() \
            else Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "mofa_state.json"
        self._since_hours = since_hours
        self._lock = threading.Lock()
        self._data = {"last_run": None, "films": {}}
        self._load()

    def _load(self):
        if self._path.is_file():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                self._data.setdefault("films", {})
            except (json.JSONDecodeError, OSError):
                logger.warning("状态文件损坏，已重置: %s", self._path)

    def _save(self):
        try:
            self._path.write_text(json.dumps(self._data, ensure_ascii=False),
                                  encoding="utf-8")
        except OSError as e:
            logger.error("状态文件写入失败: %s", e)

    def cutoff_date(self) -> Optional[str]:
        if self._since_hours <= 0:
            return None
        return (datetime.now() - timedelta(hours=self._since_hours)).strftime("%Y-%m-%d")

    def is_unchanged(self, source_url: str, update_date: str) -> bool:
        """已记录且更新日期一致 -> 视为未变更。"""
        rec = self._data["films"].get(source_url)
        if rec is None:
            return False
        return rec.get("update", "") == (update_date or "")

    def is_too_old(self, update_date: str) -> bool:
        cutoff = self.cutoff_date()
        if not cutoff or not update_date:
            return False
        return update_date < cutoff

    def mark(self, source_url: str, update_date: str):
        with self._lock:
            self._data["films"][source_url] = {
                "update": update_date or "",
                "at": int(time.time()),
            }
            # 周期性落盘（每 50 条）
            if len(self._data["films"]) % 50 == 0:
                self._save()

    def flush(self):
        with self._lock:
            self._data["last_run"] = int(time.time())
            self._save()

    def count(self) -> int:
        return len(self._data.get("films", {}))
