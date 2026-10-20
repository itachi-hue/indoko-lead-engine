"""Configuration: non-secret settings from config.toml, secrets from .env or the environment."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GROUPS = ("engineer", "founder", "researcher", "creator")
ALL_SEGMENTS = GROUPS + ("warm",)

SECRET_KEYS = (
    "EXA_API_KEY",
    "GEMINI_API_KEY",
    "INSTANTLY_API_KEY",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "HUNTER_API_KEY",
    "PROSPEO_API_KEY",
    "ICYPEAS_API_KEY",
    "VERIFIER_API_KEY",
    "OPENALEX_MAILTO",
    "ALERT_SMTP_URL",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "DATABASE_URL",
    "CRON_SECRET",
)

DEFAULTS: dict = {
    "pilot": {
        "us_only": True,
        "groups": list(GROUPS),
        "per_group_limit": 100,
    },
    "paths": {
        "data_dir": "data",
        "lock_file": "~/.lead-engine/run.lock",
    },
    "gemini": {
        "model": "",
        "prompt_version": "v1",
        "max_regenerations": 1,
    },
    "emails": {
        "verifier": "",
        "budget_usd_per_run": 10.0,
        "engineer_policy": "published_personal_only",
        "providers": ["published_page", "exa_websets", "hunter", "prospeo", "icypeas"],
    },
    "instantly": {
        "mode": "auto",
        "campaigns": {},
        "contact_limit": 1000,
        "monthly_email_limit": 5000,
    },
    "sending": {
        "daily_new_per_inbox": [15, 25, 35, 40],
        "inboxes": 0,
        "bounce_pause_pct": 2.0,
        "bounce_min_sends": 50,
        "events_max_age_hours": 26,
        "pilot_start": "",
    },
    "sender": {
        "name": "",
        "company": "Indoko",
        "postal_address": "",
        "booking_links": {},
    },
    "sheet": {
        "spreadsheet_id": "",
        "review_tab": "Review",
        "crm_tab": "CRM",
        "suppression_tab": "Suppression",
        "metrics_tab": "Metrics",
        "linkedin_tab": "LinkedIn",
    },
    "drive": {
        "warm_files": [],          # [{id, label, consent_confirmed, assume_us}]
        "backup_folder_id": "",
    },
    "retention": {
        "uncontacted_days": 90,
        "backup_days": 14,
    },
    "storage": {
        "backend": "sqlite",
        "database_url": "",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Quotes around values are stripped. No shell expansion."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


@dataclass
class Settings:
    root: Path
    data: dict
    secrets: dict[str, str] = field(default_factory=dict)

    def section(self, name: str) -> dict:
        return self.data.get(name, {})

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def secret(self, key: str) -> str:
        return self.secrets.get(key, "")

    @property
    def data_dir(self) -> Path:
        p = Path(os.path.expanduser(self.get("paths.data_dir", "data")))
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lead_engine.sqlite"

    @property
    def lock_path(self) -> Path:
        return Path(os.path.expanduser(self.get("paths.lock_file", "~/.lead-engine/run.lock")))


def load_settings(root: Path | str | None = None, config_file: str = "config.toml",
                  env: dict[str, str] | None = None) -> Settings:
    """Load settings. `env` overrides the process environment (used in tests)."""
    root = Path(root or os.environ.get("LEAD_ENGINE_ROOT") or Path.cwd()).resolve()
    data = DEFAULTS
    source = dict(os.environ) if env is None else dict(env)
    cfg = root / config_file
    if source.get("LEAD_ENGINE_CONFIG_TOML"):      # hosted: the whole config.toml in one env var
        data = _merge(DEFAULTS, tomllib.loads(source["LEAD_ENGINE_CONFIG_TOML"]))
    elif cfg.exists():
        with cfg.open("rb") as fh:
            data = _merge(DEFAULTS, tomllib.load(fh))
    file_env = parse_env_file(root / ".env")
    secrets = {k: source.get(k) or file_env.get(k, "") for k in SECRET_KEYS}
    return Settings(root=root, data=data, secrets=secrets)
