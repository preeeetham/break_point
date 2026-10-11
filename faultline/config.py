from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("\"'")
    return out


@dataclass
class Settings:
    compose_project: str = "faultline"
    compose_file: str = "deploy/docker-compose.yml"
    nginx_conf: str = "deploy/nginx/nginx.conf"
    ditto_url: str = "http://127.0.0.1:8080"
    ditto_user: str = "ditto"
    ditto_password: str = "ditto"
    prometheus_url: str = "http://127.0.0.1:9090"
    llm_provider: str = "ollama"  # ollama | openai | anthropic
    llm_model: str = ""           # empty = provider default (phi4-mini / gpt-4o-mini / claude-sonnet-5)
    llm_base_url: str = ""        # empty = provider default
    llm_api_key: str = ""
    llm_enabled: bool = True
    llm_timeout_s: float = 120.0
    reports_dir: str = "reports"
    thresholds_file: str = "config/thresholds.yaml"
    knowledge_file: str = "config/ditto_knowledge.yaml"
    probe_rps: float = 4.0
    probe_namespace: str = "faultline.probe"
    health_interval_s: float = 3.0
    pumba_image: str = "gaiaadm/pumba:latest"
    tc_image: str = "ghcr.io/alexei-led/pumba-alpine-nettools:latest"
    network_name: str = ""
    dashboard_port: int = 8765
    # Tolerance when correlating timestamps from different clocks (host vs containers) and samplers
    # (health probe every 3 s). Two events closer than this are "same window": order unknown, never
    # reported as ordered. See faultline/topology/quality.py same_window().
    time_lenience_s: float = 1.5
    # ---- approval gate. No defaults that could silently work: an unset ADMIN_EMAIL or an unset
    # credential must fail loudly at the agent's startup check, never quietly approve anything.
    admin_email: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""            # empty = smtp_user
    smtp_starttls: bool = True
    imap_host: str = ""            # empty = smtp_host
    imap_port: int = 993
    imap_user: str = ""            # empty = smtp_user
    imap_password: str = ""        # empty = smtp_password
    imap_mailbox: str = "INBOX"
    imap_ssl: bool = True
    approval_timeout_s: float = 120.0
    approval_poll_s: float = 5.0

    @property
    def reports(self) -> Path:
        return Path(self.reports_dir)

    @property
    def compose_network(self) -> str:
        return self.network_name or f"{self.compose_project}_default"


ENV_MAP = {
    "TIME_LENIENCE_SECONDS": "time_lenience_s",
    "FAULTLINE_COMPOSE_PROJECT": "compose_project",
    "FAULTLINE_COMPOSE_FILE": "compose_file",
    "FAULTLINE_KNOWLEDGE_FILE": "knowledge_file",
    "FAULTLINE_REPORTS_DIR": "reports_dir",
    "FAULTLINE_LLM": "llm_enabled",
    "FAULTLINE_NETWORK": "network_name",
    "DITTO_URL": "ditto_url",
    "DITTO_USER": "ditto_user",
    "DITTO_PASSWORD": "ditto_password",
    "PROMETHEUS_URL": "prometheus_url",
    # OLLAMA_* are legacy aliases; LLM_* come later in the map so they win when both are set
    "OLLAMA_URL": "llm_base_url",
    "OLLAMA_MODEL": "llm_model",
    "LLM_PROVIDER": "llm_provider",
    "LLM_MODEL": "llm_model",
    "LLM_BASE_URL": "llm_base_url",
    "LLM_API_KEY": "llm_api_key",
    "ADMIN_EMAIL": "admin_email",
    "SMTP_HOST": "smtp_host",
    "SMTP_PORT": "smtp_port",
    "SMTP_USER": "smtp_user",
    "SMTP_PASSWORD": "smtp_password",
    "SMTP_FROM": "smtp_from",
    "SMTP_STARTTLS": "smtp_starttls",
    "IMAP_HOST": "imap_host",
    "IMAP_PORT": "imap_port",
    "IMAP_USER": "imap_user",
    "IMAP_PASSWORD": "imap_password",
    "IMAP_MAILBOX": "imap_mailbox",
    "IMAP_SSL": "imap_ssl",
    "APPROVAL_TIMEOUT_SECONDS": "approval_timeout_s",
    "APPROVAL_POLL_SECONDS": "approval_poll_s",
}


def load_settings(path: str | None = None) -> Settings:
    s = Settings()
    cfg = Path(path) if path else ROOT / "config" / "settings.yaml"
    if cfg.is_file():
        for k, v in (yaml.safe_load(cfg.read_text()) or {}).items():
            if hasattr(s, k) and v is not None:
                setattr(s, k, v)
    env = {**_dotenv(ROOT / ".env"), **os.environ}
    for ek, attr in ENV_MAP.items():
        raw = env.get(ek)
        if raw is None or raw == "":
            continue
        cur = getattr(s, attr)
        if isinstance(cur, bool):
            val: object = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            val = int(raw)
        elif isinstance(cur, float):
            val = float(raw)
        else:
            val = raw
        setattr(s, attr, val)
    s.smtp_from = s.smtp_from or s.smtp_user
    s.imap_host = s.imap_host or s.smtp_host
    s.imap_user = s.imap_user or s.smtp_user
    s.imap_password = s.imap_password or s.smtp_password
    for attr in ("compose_file", "nginx_conf", "reports_dir", "thresholds_file", "knowledge_file"):
        p = Path(getattr(s, attr))
        if not p.is_absolute():
            setattr(s, attr, str(ROOT / p))
    return s
