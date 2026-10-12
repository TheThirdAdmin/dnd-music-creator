"""Brings the generator up (models, then ComfyUI) and restarts ComfyUI when it dies."""
import asyncio
import logging
import time

from .comfy import ComfyClient, ComfyError, ComfyProcess
from .service import GenService

log = logging.getLogger(__name__)


class Supervisor:
    def __init__(self, service: GenService, store, process: ComfyProcess, comfy: ComfyClient, *,
                 ready_timeout_s: float = 600, check_s: float = 2.0, retry_s: float = 30.0):
        self.service = service
        self.store = store
        self.process = process
        self.comfy = comfy
        self.ready_timeout_s = ready_timeout_s
        self.check_s = check_s
        self.retry_s = retry_s

    async def ensure_models(self) -> None:
        while True:
            try:
                await self.store.ensure(self.service.set_status)
                return
            except Exception as e:
                log.error("models: %s", e)
                self.service.set_status(f"error: {e}")
                await asyncio.sleep(self.retry_s)

    async def start_comfy(self, status: str) -> None:
        self.service.set_status(status)
        self.process.start()
        deadline = time.monotonic() + self.ready_timeout_s
        while not await self.comfy.ready():
            if not self.process.alive():
                raise ComfyError(f"ComfyUI exited while starting (code {self.process.returncode})")
            if time.monotonic() > deadline:
                raise ComfyError(f"ComfyUI did not answer within {self.ready_timeout_s:g} s")
            await asyncio.sleep(0.5)
        self.service.set_status("ok", ready=True)
        log.info("ComfyUI is up (pid %s)", self.process.pid)

    async def run(self) -> None:
        await self.ensure_models()
        started = False
        while True:
            if not self.process.alive():
                if started:
                    log.warning("ComfyUI exited (code %s); restarting it", self.process.returncode)
                try:
                    await self.start_comfy("restarting ComfyUI" if started else "loading")
                    started = True
                except (ComfyError, OSError) as e:
                    log.error("ComfyUI start failed: %s", e)
                    await asyncio.to_thread(self.process.stop)
                    self.service.set_status(f"error: {e}")
                    await asyncio.sleep(self.retry_s)
                    continue
            await asyncio.sleep(self.check_s)

    def stop(self) -> None:
        self.process.stop()
