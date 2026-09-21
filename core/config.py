# -*- coding: utf-8 -*-
"""配置加载：.env + 默认值。

约定：
    - 所有配置项都在 .env 里，代码里只保留默认值；
    - 读 .env 用极简解析器（避免依赖 python-dotenv）；
    - 任何模块通过 `cfg` 单例访问配置。
"""
from __future__ import annotations

import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# 默认值（.env 缺失时的兜底）
DEFAULTS: dict[str, str] = {
    "DEEPSEEK_API_KEY": "",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "MODEL": "deepseek-chat",
    "TEMPERATURE": "0.3",
    "MAX_TOKENS": "2048",
    "LLM_TIMEOUT": "180",
    "HISTORY_TURNS": "6",
    "KB_DIR": "./kb",
    "TOP_K": "5",
    "KB_SCORE_THRESHOLD": "0.5",
    "MAX_CONTEXT_CHARS": "9000",
    "WEB_SEARCH_ENABLED": "true",
    "WEB_ENGINES": "bing,baidu",
    "WEB_MAX_PAGES": "3",
    "WEB_TIMEOUT": "15",
    "WEB_MAX_CHARS": "6000",
    "AUTO_INGEST": "inbox",
    "HOST": "127.0.0.1",
    "PORT": "8765",
    "OPEN_BROWSER": "true",
}


def _parse_env(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 开头为注释，支持引号包裹。"""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            data[key] = val
    return data


class Config:
    """线程安全的配置容器；支持运行时热更新并落盘。"""

    def __init__(self, path: Path = ENV_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._raw: dict[str, str] = {}
        self._mtime: float = 0.0
        self.reload()

    # ---------- 读写 ----------
    def _mtime_now(self) -> float:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return 0.0

    def reload(self) -> None:
        with self._lock:
            self._raw = dict(DEFAULTS)
            self._raw.update(_parse_env(self._path))
            self._mtime = self._mtime_now()

    def maybe_reload(self) -> bool:
        """外部改动过 .env 就自动重载，返回是否真的重载了。

        好处：手工编辑 .env（比如换 API Key）后不必重启服务，
        下一个请求就会带上新配置，省掉"改了没生效"这类排查。
        """
        m = self._mtime_now()
        with self._lock:
            if not m or m == self._mtime:
                return False
            self._raw = dict(DEFAULTS)
            self._raw.update(_parse_env(self._path))
            self._mtime = m
        return True

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            return self._raw.get(key, DEFAULTS.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(key) or default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key) or default)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        return str(self.get(key, "")).strip().lower() in ("1", "true", "yes", "on", "是")

    def get_list(self, key: str, default: str = "") -> list[str]:
        raw = self.get(key) or default
        return [x.strip() for x in str(raw).split(",") if x.strip()]

    def set_many(self, updates: dict[str, str]) -> None:
        """更新若干项并写回 .env（保留注释与顺序，未知键追加到末尾）。"""
        with self._lock:
            lines = []
            if self._path.exists():
                lines = self._path.read_text(encoding="utf-8").splitlines()
            seen: set[str] = set()
            out: list[str] = []
            for line in lines:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k = s.split("=", 1)[0].strip()
                    if k in updates:
                        out.append(f"{k}={updates[k]}")
                        seen.add(k)
                        continue
                out.append(line)
            for k, v in updates.items():
                if k not in seen:
                    out.append(f"{k}={v}")
            self._path.write_text("\n".join(out) + "\n", encoding="utf-8")
            self._raw.update(updates)
            self._mtime = self._mtime_now()

    # ---------- 派生属性 ----------
    @property
    def api_key(self) -> str:
        return self.get("DEEPSEEK_API_KEY")

    @property
    def base_url(self) -> str:
        return self.get("DEEPSEEK_BASE_URL").rstrip("/")

    @property
    def kb_dir(self) -> Path:
        """知识库目录。相对路径一律相对于本项目根目录解析，便于跨机器迁移。"""
        p = Path(self.get("KB_DIR").strip() or "./kb")
        return p if p.is_absolute() else (ROOT / p)

    @property
    def inbox_dir(self) -> Path:
        return self.kb_dir / "_inbox"

    @property
    def data_dir(self) -> Path:
        return ROOT / "data"

    @property
    def log_dir(self) -> Path:
        return ROOT / "logs"

    @property
    def static_dir(self) -> Path:
        return ROOT / "static"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chat.db"

    @property
    def index_path(self) -> Path:
        return self.data_dir / "kb_index.db"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.log_dir, self.inbox_dir):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def public(self) -> dict:
        """给前端的安全视图：绝不返回 API Key 明文。"""
        key = self.api_key
        masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已配置" if key else "未配置")
        return {
            "model": self.get("MODEL"),
            "base_url": self.base_url,
            "api_key_masked": masked,
            "api_key_set": bool(key),
            "temperature": self.get_float("TEMPERATURE", 0.3),
            "max_tokens": self.get_int("MAX_TOKENS", 2048),
            "history_turns": self.get_int("HISTORY_TURNS", 6),
            "kb_dir": str(self.kb_dir),
            "top_k": self.get_int("TOP_K", 5),
            "kb_score_threshold": self.get_float("KB_SCORE_THRESHOLD", 0.5),
            "max_context_chars": self.get_int("MAX_CONTEXT_CHARS", 9000),
            "web_search_enabled": self.get_bool("WEB_SEARCH_ENABLED", True),
            "web_engines": self.get_list("WEB_ENGINES", "bing,baidu"),
            "web_max_pages": self.get_int("WEB_MAX_PAGES", 3),
            "web_timeout": self.get_int("WEB_TIMEOUT", 15),
            "auto_ingest": self.get("AUTO_INGEST"),
            "host": self.get("HOST"),
            "port": self.get_int("PORT", 8765),
        }


cfg = Config()
