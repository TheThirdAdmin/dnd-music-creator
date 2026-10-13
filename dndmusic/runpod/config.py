import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

FILE = "runpod.json"


@dataclass(frozen=True)
class RunpodConfig:
    endpoint_id: str
    api_key: str = field(repr=False)
    rate_per_hr: float
    idle_timeout_s: float = 5.0
    warn_below: float = 5.0
    floor: float = 1.0
    daily_cap: float | None = None
    budget_usd: float | None = None
    api_base: str = "https://api.runpod.ai/v2"


def load_config(data_dir: Path, env: Mapping) -> RunpodConfig | None:
    """<data_dir>/runpod.json, with RUNPOD_ENDPOINT_ID / RUNPOD_API_KEY from env taking precedence."""
    p = Path(data_dir) / FILE
    raw = json.loads(p.read_text()) if p.exists() else {}
    if env.get("RUNPOD_ENDPOINT_ID"):
        raw["endpoint_id"] = env["RUNPOD_ENDPOINT_ID"]
    if env.get("RUNPOD_API_KEY"):
        raw["api_key"] = env["RUNPOD_API_KEY"]
    if not raw.get("endpoint_id") or not raw.get("api_key"):
        return None
    if env.get("RUNPOD_API_BASE"):   # tests and demos only
        raw["api_base"] = env["RUNPOD_API_BASE"]
    raw.setdefault("rate_per_hr", 0.0)
    return RunpodConfig(**{k: v for k, v in raw.items() if k in RunpodConfig.__dataclass_fields__})


def save_config(data_dir: Path, cfg: RunpodConfig) -> None:
    p = Path(data_dir) / FILE
    doc = asdict(cfg)
    try:   # keep the ids runpod_setup.py recorded for --teardown; they are not config fields
        created = json.loads(p.read_text()).get("created")
    except (OSError, ValueError, AttributeError):
        created = None
    if created:
        doc["created"] = created
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, p)
