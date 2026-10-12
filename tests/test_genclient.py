import asyncio

import httpx
import pytest

from dndmusic.genclient import GeneratorClient, GeneratorError
from dndmusic.models import Prompt
from fakegen.app import create_app


def client_for(app):
    return GeneratorClient("http://fake", client=httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fake"))


async def test_submit_poll_fetch():
    gc = client_for(create_app(delay_s=0, max_duration_s=1))
    assert (await gc.health())["ok"] is True
    job = await gc.submit(Prompt("epic", "[instrumental]"), 60, seed=5)
    for _ in range(200):
        st = await gc.status(job)
        if st.status == "done":
            break
        await asyncio.sleep(0.01)
    assert st.status == "done" and st.seed == 5 and st.error is None
    assert (await gc.audio(job))[:4] == b"RIFF"
    await gc.aclose()


async def test_http_errors_become_generator_error():
    gc = GeneratorClient("http://down", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)), base_url="http://down"))
    with pytest.raises(GeneratorError):
        await gc.health()


async def test_connection_errors_become_generator_error():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    gc = GeneratorClient("http://down", client=httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://down"))
    with pytest.raises(GeneratorError):
        await gc.submit(Prompt("x", ""), 60)


import json


def mock_client(handler):
    return GeneratorClient("http://g", client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                                base_url="http://g"))


@pytest.mark.parametrize("reply", [
    lambda: httpx.Response(200, text="<html>oops</html>"),
    lambda: httpx.Response(200, json=["not", "an", "object"]),
    lambda: httpx.Response(200, json={}),
], ids=["not_json", "not_object", "missing_keys"])
async def test_malformed_replies_become_generator_error(reply):
    gc = mock_client(lambda r: reply())
    with pytest.raises(GeneratorError):
        await gc.health()
    with pytest.raises(GeneratorError):
        await gc.submit(Prompt("x"), 60)
    with pytest.raises(GeneratorError):
        await gc.status("j1")


async def test_submit_sends_quality_and_status_reads_duration():
    seen = {}

    def handler(request):
        if request.method == "POST":
            seen.update(json.loads(request.content))
            return httpx.Response(202, json={"id": "j1"})
        return httpx.Response(200, json={"id": "j1", "status": "done", "seed": 3, "duration_s": 120.0, "error": None})

    gc = mock_client(handler)
    assert await gc.submit(Prompt("epic"), 200, seed=3, quality=1) == "j1"
    assert seen == {"tags": "epic", "lyrics": "", "duration_s": 200, "seed": 3, "quality": 1}
    assert (await gc.status("j1")).duration_s == 120.0
