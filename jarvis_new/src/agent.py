import logging
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    JobContext,
    TurnHandlingOptions,
    cli,
    inference,
    room_io,
)
from livekit.plugins import ai_coustics

from agents import JarvisAgent, JarvisServices
from browser import BrowserManager
from delegate import CodingRunner, load_workspaces
from modes import JarvisConfig, build_session_models, load_config
from plugins import PluginConfigError, load_plugin_specs

logger = logging.getLogger("jarvis")

# Resolve relative to this file so the launcher can start Jarvis from anywhere.
load_dotenv(Path(__file__).resolve().parents[1] / ".env.local")


def build_services(config: JarvisConfig, *, headless: bool = True) -> JarvisServices:
    try:
        plugins = load_plugin_specs()
    except PluginConfigError:
        # A typo in the plug-in file shouldn't stop Jarvis from starting.
        logger.exception("ignoring config/jarvis-mcp.json")
        plugins = []
    return JarvisServices(
        config=config,
        browser=BrowserManager(headless=headless),
        runner=CodingRunner(config.coder, load_workspaces()),
        plugins=plugins,
    )


def Assistant(browser: BrowserManager | None = None) -> JarvisAgent:  # noqa: N802
    """The main Jarvis agent with default services (used by tests and console)."""
    services = build_services(load_config())
    if browser is not None:
        services.browser = browser
    return JarvisAgent(services)


def build_room_options(config: JarvisConfig) -> room_io.RoomOptions:
    cloud = config.mode == "cloud"
    audio = (
        room_io.AudioInputOptions(
            noise_cancellation=ai_coustics.audio_enhancement(
                model=ai_coustics.EnhancerModel.QUAIL_VF_S
            ),
        )
        if cloud and config.cloud.noise_cancellation
        else room_io.AudioInputOptions()
    )
    # Only Gemini Live can see video; the local text model can't use it.
    return room_io.RoomOptions(video_input=cloud, audio_input=audio)


server = AgentServer()


@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: JobContext):
    config = load_config()
    ctx.log_context_fields = {"room": ctx.room.name, "mode": config.mode}

    services = build_services(config, headless=False)
    ctx.add_shutdown_callback(services.browser.close)
    # A coding task is tied to the call that started it.
    ctx.add_shutdown_callback(services.runner.close)

    session = AgentSession(
        **build_session_models(config),
        turn_handling=TurnHandlingOptions(
            # Cloud mode uses LiveKit's hosted turn detector; local mode uses the
            # small on-device model so nothing leaves the PC.
            turn_detection=(
                inference.TurnDetector()
                if config.mode == "cloud"
                else inference.TurnDetector(version="v1-mini")
            ),
            # Adaptive interruptions tell a real interruption from a "mhm".
            interruption={"mode": "adaptive"},
            # Let the LLM start replying while waiting for the end of turn.
            preemptive_generation={"enabled": True},
        ),
    )

    await session.start(
        agent=JarvisAgent(services),
        room=ctx.room,
        room_options=build_room_options(config),
    )

    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
