import logging

import httpx
import uvicorn

from .app import create_app
from .comfy import ComfyClient, ComfyProcess, ComfyRenderer, comfy_command, write_model_paths
from .config import GenSettings
from .modelfiles import ModelStore, needed
from .service import GenService
from .supervisor import Supervisor


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = GenSettings.from_env()
    s.work_dir.mkdir(parents=True, exist_ok=True)
    comfy = ComfyClient(f"http://127.0.0.1:{s.comfy_port}")
    service = GenService(ComfyRenderer(comfy, s.steps, s.work_dir / "comfy" / "output"), gpu=s.gpu,
                         work_dir=s.work_dir, idle_free_s=s.idle_free_s, keep_s=s.keep_s,
                         fetched_keep_s=s.fetched_keep_s)
    store = ModelStore(s.model_dir, needed(s.gpu), httpx.AsyncClient(timeout=httpx.Timeout(60.0)), s.hf_base)
    process = ComfyProcess(comfy_command(s, write_model_paths(s)), cwd=s.comfy_dir)
    logging.getLogger(__name__).info("generator: %s mode, models in %s, port %d", s.device, s.model_dir, s.port)
    uvicorn.run(create_app(service, Supervisor(service, store, process, comfy)), host="0.0.0.0", port=s.port,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
