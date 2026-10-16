import jwt
import pytest
from aiohttp.test_utils import TestClient, TestServer

from phone_server import create_app

KEY = "k" * 32
SECRET = "s" * 32


@pytest.fixture
async def client():
    app = create_app(
        phone_key=KEY,
        livekit_url="wss://example.livekit.cloud",
        api_key="devkey",
        api_secret=SECRET,
    )
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def test_rejects_missing_key(client: TestClient) -> None:
    response = await client.post("/token", json={})
    assert response.status == 403


async def test_rejects_wrong_key(client: TestClient) -> None:
    response = await client.post("/token", json={}, headers={"x-jarvis-key": "nope"})
    assert response.status == 403


async def test_rejects_spoofed_localhost_host_header(client: TestClient) -> None:
    response = await client.post("/token", json={}, headers={"Host": "localhost:3000"})
    assert response.status == 403


async def test_issues_a_token_that_dispatches_jarvis(client: TestClient) -> None:
    response = await client.post(
        "/token",
        json={"room_config": {"agents": [{"agent_name": "someone-else"}]}},
        headers={"x-jarvis-key": KEY},
    )

    assert response.status == 200
    body = await response.json()
    assert body["server_url"] == "wss://example.livekit.cloud"
    claims = jwt.decode(body["participant_token"], SECRET, algorithms=["HS256"])
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["room"].startswith("jarvis_phone_")
    assert claims["roomConfig"]["agents"] == [{"agentName": "my-agent"}]


def test_short_keys_are_refused() -> None:
    with pytest.raises(ValueError, match="at least 16"):
        create_app(phone_key="short", livekit_url="x", api_key="x", api_secret="x")
