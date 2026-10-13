import json
import logging

import httpx
import pytest

from dndmusic.runpod.balance import Balance, BalanceReader
from dndmusic.runpod.config import RunpodConfig

KEY = "rpa_SECRETKEY123"
CFG = RunpodConfig(endpoint_id="ep", api_key=KEY, rate_per_hr=0.58)


def reader(handler):
    return BalanceReader(CFG, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_ok():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": {"myself": {"clientBalance": 12.34, "currentSpendPerHr": 0}}})
    assert await reader(h).read() == Balance(12.34, 0.0, None)
    assert seen["url"] == f"https://api.runpod.io/graphql?api_key={KEY}"
    assert "clientBalance" in seen["body"]["query"]


@pytest.mark.parametrize("resp", [httpx.Response(401), httpx.Response(200, json={"errors": [{"message": "x"}]})])
async def test_denied(resp):
    b = await reader(lambda req: resp).read()
    assert b == Balance(None, None, "key cannot read the balance")


async def test_unreachable(caplog):
    def h(req):
        raise httpx.ConnectError("boom", request=req)
    caplog.set_level(logging.INFO)
    b = await reader(h).read()
    assert b.error == "Runpod unreachable" and KEY not in b.error
    assert KEY not in caplog.text


async def test_no_key_in_logs(caplog):
    caplog.set_level(logging.INFO)
    await reader(lambda r: httpx.Response(200, json={"data": {"myself": {"clientBalance": 1}}})).read()
    assert KEY not in caplog.text
