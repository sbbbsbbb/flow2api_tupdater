"""Token Updater 配置 v3.0"""
import json
import os
from pydantic import BaseModel


PERSIST_KEYS = ("flow2api_url", "connection_token", "refresh_interval")


def _get_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_origins(value: str | None) -> list[str]:
    if value is None:
        return []
    value = value.strip()
    if not value:
        return []
    if value == "*":
        return ["*"]
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_persisted_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
        return {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_persisted_config(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=True, indent=2)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


class Config(BaseModel):
    """配置类"""
    # 管理员密码 (Web UI)
    admin_password: str

    # 外部 API Key
    api_key: str

    # Flow2API 服务器配置
    flow2api_url: str
    connection_token: str

    # 刷新间隔 (分钟)
    refresh_interval: int

    # Profile 目录
    profiles_dir: str = "/app/profiles"

    # Google Labs URL
    labs_url: str = "https://labs.google/fx/tools/flow"

    # Cookie 名称
    session_cookie_name: str = "__Secure-next-auth.session-token"

    # API 端口
    api_port: int

    # 数据库路径
    db_path: str = "/app/data/profiles.db"

    # 会话 TTL (分钟)
    session_ttl_minutes: int

    # CORS 配置
    cors_origins: list[str]
    cors_allow_credentials: bool

    # 配置持久化文件
    config_file: str

    def save(self) -> None:
        """持久化可更新配置"""
        data = {key: getattr(self, key) for key in PERSIST_KEYS}
        _save_persisted_config(self.config_file, data)


def _build_config() -> Config:
    config_file = _get_env("CONFIG_FILE") or "/app/data/config.json"
    persisted = _load_persisted_config(config_file)

    flow2api_url = _get_env("FLOW2API_URL")
    if flow2api_url is None:
        flow2api_url = persisted.get("flow2api_url") or "http://host.docker.internal:8000"

    connection_token = _get_env("CONNECTION_TOKEN")
    if connection_token is None:
        connection_token = persisted.get("connection_token", "")

    refresh_interval = _get_env("REFRESH_INTERVAL")
    if refresh_interval is None:
        refresh_interval = persisted.get("refresh_interval", 60)
    refresh_interval = _parse_int(str(refresh_interval), 60)

    return Config(
        admin_password=_get_env("ADMIN_PASSWORD") or "",
        api_key=_get_env("API_KEY") or "",
        flow2api_url=flow2api_url,
        connection_token=connection_token,
        refresh_interval=refresh_interval,
        api_port=_parse_int(_get_env("API_PORT"), 8002),
        session_ttl_minutes=_parse_int(_get_env("SESSION_TTL_MINUTES"), 1440),
        cors_origins=_parse_origins(_get_env("CORS_ORIGINS")),
        cors_allow_credentials=_parse_bool(_get_env("CORS_ALLOW_CREDENTIALS"), default=False),
        config_file=config_file,
    )


config = _build_config()
