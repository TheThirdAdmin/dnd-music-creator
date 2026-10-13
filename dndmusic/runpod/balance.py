import logging
from dataclasses import dataclass

import httpx

from .config import RunpodConfig

# GraphQL takes the key as a query parameter and httpx logs URLs at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

URL = "https://api.runpod.io/graphql"
QUERY = "query { myself { clientBalance currentSpendPerHr } }"


@dataclass(frozen=True)
class Balance:
    usd: float | None = None
    spend_per_hr: float | None = None
    error: str | None = None


class BalanceReader:
    def __init__(self, cfg: RunpodConfig, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(timeout=15)

    async def read(self) -> Balance:
        denied = Balance(error="key cannot read the balance")
        try:
            r = await self.client.post(URL, params={"api_key": self.cfg.api_key}, json={"query": QUERY})
        except httpx.HTTPError:
            return Balance(error="Runpod unreachable")
        if r.status_code != 200:
            return denied
        try:
            d = r.json()
            if d.get("errors"):
                return denied
            me = d["data"]["myself"]
            return Balance(float(me["clientBalance"]), float(me.get("currentSpendPerHr") or 0.0))
        except (ValueError, KeyError, TypeError, AttributeError):
            return denied

    async def aclose(self) -> None:
        await self.client.aclose()
