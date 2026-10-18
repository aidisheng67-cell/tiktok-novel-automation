"""凭证加载与脱敏。

优先级：环境变量 > 本地 JSON > 默认值。
仓库只提交 `.env.example`，不提交任何真实凭证；本地仍可继续用
`deepseek.json` / `feishu.json` / `feixing.json`，这些文件已被 .gitignore 排除。

CI、Docker、服务器建议只注入环境变量：
    DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
    FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN / FEISHU_BASE_URL
    FEIXING_API_KEY / FEIXING_TEAM_ID
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENV_MAP = {
    "deepseek": {
        "api_key": "DEEPSEEK_API_KEY",
        "base_url": "DEEPSEEK_BASE_URL",
        "model": "DEEPSEEK_MODEL",
    },
    "feishu": {
        "app_id": "FEISHU_APP_ID",
        "app_secret": "FEISHU_APP_SECRET",
        "app_token": "FEISHU_APP_TOKEN",
        "base_url": "FEISHU_BASE_URL",
    },
    "feixing": {
        "api_key": "FEIXING_API_KEY",
        "team_id": "FEIXING_TEAM_ID",
    },
}

DEFAULTS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "feishu": {"base_url": "https://open.feishu.cn"},
    "feixing": {},
}

SECRET_KEYS = {
    "api_key", "app_secret", "app_token", "access_token",
    "tenant_access_token", "token", "secret", "password",
}

_SECRET_QUERY = re.compile(
    r"(?i)\b(api_key|app_secret|app_token|access_token|tenant_access_token|token|secret|password)"
    r"=([^&\s\"']+)"
)


def root_dir(root: str | Path | None = None) -> Path:
    return Path(root) if root else Path(os.environ.get("NOVEL_ROOT") or ROOT)


def load(name: str, root: str | Path | None = None) -> dict:
    """读取一份凭证；环境变量覆盖 JSON，JSON 覆盖默认值。"""
    base = root_dir(root)
    data = dict(DEFAULTS.get(name, {}))
    path = base / f"{name}.json"
    if path.exists():
        with path.open(encoding="utf-8") as f:
            data.update(json.load(f) or {})
    for key, env in ENV_MAP.get(name, {}).items():
        value = os.environ.get(env)
        if value not in (None, ""):
            data[key] = value
    return data


def missing(name: str, cfg: dict, required: tuple[str, ...]) -> list[str]:
    """返回缺失的必填项，用于给出可读的启动错误。"""
    return [k for k in required if not str(cfg.get(k, "")).strip()]


def redact_text(text: str, secrets=()) -> str:
    """替换真实密钥和 URL 里的 key=... 片段。"""
    out = str(text)
    for s in secrets:
        if s and len(str(s)) >= 6:
            out = out.replace(str(s), "***")
    return _SECRET_QUERY.sub(r"\1=***", out)


def redact(data: dict, secrets=()) -> dict:
    """按字段名脱敏，供控制台/日志展示。"""
    out = dict(data)
    for key in list(out):
        if key.lower() in SECRET_KEYS and out[key]:
            out[key] = "***"
        elif isinstance(out[key], str):
            out[key] = redact_text(out[key], secrets)
    return out


def save(name: str, data: dict, root: str | Path | None = None) -> Path:
    """写回本地 JSON（控制台用）。调用方负责不把占位符 *** 写回。"""
    base = root_dir(root)
    path = base / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
