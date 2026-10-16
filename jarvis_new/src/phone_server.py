"""Token server for the Jarvis phone app.

The phone can't use the web UI's token route: that only listens on this PC.
This server listens on the local network and hands out LiveKit tokens, but
only to callers that send the shared JARVIS_PHONE_KEY, because a Jarvis
session can edit code on this PC.

Run:  uv run src/phone_server.py   (the launcher does this with -Phone)
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

logger = logging.getLogger("jarvis.phone")

AGENT_NAME = "my-agent"
DEFAULT_PORT = 8787


def create_app(
    *,
    phone_key: str,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    agent_name: str = AGENT_NAME,
) -> web.Application:
    if len(phone_key) < 16:
        raise ValueError("JARVIS_PHONE_KEY must be at least 16 characters.")

    async def token(request: web.Request) -> web.Response:
        supplied = request.headers.get("x-jarvis-key", "")
        if not hmac.compare_digest(supplied.encode(), phone_key.encode()):
            logger.warning("rejected token request from %s", request.remote)
            return web.Response(status=403, text="Forbidden")

        room_name = f"jarvis_phone_{secrets.token_hex(4)}"
        jwt = (
            api.AccessToken(api_key, api_secret)
            .with_identity(f"phone_user_{secrets.token_hex(4)}")
            .with_name("user")
            .with_ttl(timedelta(minutes=15))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_publish_data=True,
                    can_subscribe=True,
                )
            )
            # Always dispatch Jarvis, whatever agent the caller asked for.
            .with_room_config(
                api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name=agent_name)]
                )
            )
            .to_jwt()
        )
        return web.json_response(
            {"server_url": livekit_url, "participant_token": jwt},
            headers={"Cache-Control": "no-store"},
        )

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/token", token)
    return app


def lan_address() -> str:
    """Best guess at this PC's address on the local network."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            # No packets are sent; this just picks the outgoing interface.
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env.local")
    logging.basicConfig(level=logging.INFO)

    missing = [
        name
        for name in (
            "JARVIS_PHONE_KEY",
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"Missing in .env.local: {', '.join(missing)}")

    port = int(os.environ.get("JARVIS_PHONE_PORT", DEFAULT_PORT))
    app = create_app(
        phone_key=os.environ["JARVIS_PHONE_KEY"],
        # A local LiveKit server is "localhost" to the PC but not to the phone;
        # the launcher sets its network address here.
        livekit_url=os.environ.get("JARVIS_PHONE_LIVEKIT_URL")
        or os.environ["LIVEKIT_URL"],
        api_key=os.environ["LIVEKIT_API_KEY"],
        api_secret=os.environ["LIVEKIT_API_SECRET"],
    )
    print("Phone app settings for agent-starter-flutter/assets/.env:")
    print(f"  LIVEKIT_TOKEN_ENDPOINT=http://{lan_address()}:{port}/token")
    print("  JARVIS_PHONE_KEY=<the same key as in jarvis_new/.env.local>")
    web.run_app(app, host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
