import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def lan_ip() -> str:
    """The address this host uses towards the LAN (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@dataclass(frozen=True)
class Settings:
    port: int
    public_url: str
    generator_urls: tuple[str, ...]
    data_dir: Path
    starter_dir: Path | None = None
    host_data_path: str | None = None   # the library folder as the host sees it, for display
    dm_password: bool = False           # from_env turns it on unless DM_PASSWORD=off

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        port = int(env.get("PORT", "8000"))
        public_url = env.get("PUBLIC_URL") or f"http://{lan_ip()}:{port}"
        generators = tuple(
            u.strip().rstrip("/")
            for u in env.get("GENERATOR_URLS", "http://localhost:8001").split(",")
            if u.strip()
        )
        starter = env.get("STARTER_DIR") or None
        return cls(port, public_url.rstrip("/"), generators, Path(env.get("DATA_DIR", "/data")),
                   Path(starter) if starter else None, env.get("HOST_DATA_PATH") or None,
                   env.get("DM_PASSWORD", "on").strip().lower() not in ("off", "0", "no", "false"))
