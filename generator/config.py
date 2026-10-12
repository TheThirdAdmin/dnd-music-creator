import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GenSettings:
    device: str = "gpu"
    model_dir: Path = Path("/models")
    work_dir: Path = Path("/tmp/dndgen")
    comfy_dir: Path = Path("/opt/comfyui")
    comfy_port: int = 8188
    port: int = 8001
    steps: int = 8
    idle_free_s: float = 60.0
    keep_s: float = 3600.0
    fetched_keep_s: float = 600.0
    hf_base: str = "https://huggingface.co"

    @property
    def gpu(self) -> bool:
        return self.device == "gpu"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GenSettings":
        env = os.environ if env is None else env
        device = env.get("GENERATOR_DEVICE", "gpu").strip().lower()
        if device not in ("gpu", "cpu"):
            raise ValueError(f"GENERATOR_DEVICE must be gpu or cpu, not {device!r}")
        return cls(device=device, model_dir=Path(env.get("MODEL_DIR", "/models")),
                   work_dir=Path(env.get("WORK_DIR", "/tmp/dndgen")), comfy_dir=Path(env.get("COMFY_DIR", "/opt/comfyui")),
                   port=int(env.get("PORT", "8001")), hf_base=env.get("HF_BASE", "https://huggingface.co").rstrip("/"))
