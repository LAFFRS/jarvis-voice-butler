"""Jarvis and its specialist agents.

None of these agents sets its own model: they all use whichever brain the
session was built with (see modes.py), so handoffs work the same in cloud and
local mode.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.beta.tools import EndCallTool
from livekit.agents.llm import ToolError

from browser import BrowserManager
from delegate import CodingJob, CodingRunner, DelegateError, ProgressEvent
from modes import MODES_FILE, ConfigError, JarvisConfig, load_config, save_mode
from prompts import BROWSER_INSTRUCTIONS, CODING_INSTRUCTIONS, JARVIS_INSTRUCTIONS
from tools import BrowserTools


@dataclass
class JarvisServices:
    """Everything the agents share during one session."""

    config: JarvisConfig
    browser: BrowserManager
    runner: CodingRunner
    modes_file: Path = MODES_FILE


def _end_call_tool() -> EndCallTool:
    return EndCallTool(
        extra_description=(
            "Only end the call after the user clearly says they are finished, "
            "says goodbye, or directly asks to end the call."
        ),
        end_instructions=(
            "Give Jarvis's brief, polite British-English farewell, then end the call."
        ),
    )


class ProgressNarrator:
    """Decides when a running coding task is worth a spoken update.

    Speaking after every file edit would be unbearable, so it announces the
    first step, then at most one update per `min_interval` seconds, and always
    the final result.
    """

    def __init__(
        self,
        speak: Callable[[str], Any],
        *,
        min_interval: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._speak = speak
        self._min_interval = min_interval
        self._clock = clock
        self._last_spoken_at: float | None = None
        self._last_step = ""

    def on_progress(self, job: CodingJob, event: ProgressEvent) -> None:
        if event.kind != "tool" or event.text == self._last_step:
            return
        now = self._clock()
        if (
            self._last_spoken_at is not None
            and now - self._last_spoken_at < self._min_interval
        ):
            return
        self._last_spoken_at = now
        self._last_step = event.text
        self._speak(
            "Give the user a very brief progress update on their coding task in "
            f"the {job.workspace} workspace: the coding agent is {event.text}. "
            "One short sentence."
        )

    def on_finish(self, job: CodingJob) -> None:
        summary = job.summary.strip()[:800] or "No summary was given."
        if job.status == "done":
            self._speak(
                f"The coding task in the {job.workspace} workspace has finished. "
                f"Tell the user briefly what was done, based on this summary: {summary}"
            )
        elif job.status == "failed":
            self._speak(
                f"The coding task in the {job.workspace} workspace failed. Tell the "
                f"user briefly why, and suggest a next step. Details: {summary}"
            )
        else:
            self._speak(
                f"Confirm to the user that the coding task in the {job.workspace} "
                "workspace was stopped."
            )


class _JarvisBase(Agent):
    """Shared setup. LiveKit registers each subclass's @function_tool methods
    automatically, so `tools` only lists tools that live on other objects."""

    def __init__(
        self,
        services: JarvisServices,
        *,
        instructions: str,
        tools: list,
        chat_ctx: Any = None,
    ) -> None:
        self.services = services
        self._end_call = _end_call_tool()
        kwargs: dict[str, Any] = {}
        if chat_ctx is not None:
            kwargs["chat_ctx"] = chat_ctx
        super().__init__(
            instructions=instructions,
            tools=[*tools, *self._end_call.tools],
            **kwargs,
        )

    def _handoff_ctx(self) -> Any:
        return self.chat_ctx.copy(exclude_instructions=True)

    def _coding_status(self) -> str:
        job = self.services.runner.current
        if job is None:
            return "No coding task has been started in this session."
        return job.describe()


class JarvisAgent(_JarvisBase):
    """The main butler: conversation, quick answers, routing."""

    def __init__(self, services: JarvisServices, chat_ctx: Any = None) -> None:
        super().__init__(
            services,
            instructions=JARVIS_INSTRUCTIONS,
            tools=[],
            chat_ctx=chat_ctx,
        )

    @function_tool()
    async def transfer_to_coding(self, context: RunContext) -> tuple[Agent, str]:
        """Hand off to the coding specialist, who can make real changes to code in
        the user's projects, run coding tasks, and reverse engineer software."""
        return CodingAgent(self.services, self._handoff_ctx()), (
            "Handing over to the coding specialist."
        )

    @function_tool()
    async def transfer_to_web(self, context: RunContext) -> tuple[Agent, str]:
        """Hand off to the web specialist, who controls a browser to open websites,
        search the internet, research topics, and interact with web pages."""
        return BrowserAgent(self.services, self._handoff_ctx()), (
            "Handing over to the web specialist."
        )

    @function_tool()
    async def coding_task_status(self, context: RunContext) -> str:
        """Check how the current or most recent coding task is going."""
        return self._coding_status()

    @function_tool()
    async def switch_brain_mode(self, context: RunContext, mode: str) -> str:
        """Switch Jarvis between cloud mode (online models) and local mode (models
        running on the user's own PC). Takes effect from the next session.

        Args:
            mode: Either "cloud" or "local".
        """
        mode = mode.strip().lower()
        try:
            save_mode(mode, self.services.modes_file)
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc
        # Re-read to catch an environment override that would mask the change.
        effective = load_config(self.services.modes_file).mode
        if effective != mode:
            return (
                f"Saved {mode} mode, but the launcher is forcing {effective} mode "
                "for now. Start Jarvis without the mode option to use it."
            )
        return f"Switched to {mode} mode. It applies from the next session."


class BrowserAgent(_JarvisBase):
    """Web specialist: the original Playwright browser tools."""

    def __init__(self, services: JarvisServices, chat_ctx: Any = None) -> None:
        self.browser_tools = BrowserTools(services.browser)
        super().__init__(
            services,
            instructions=BROWSER_INSTRUCTIONS,
            tools=self.browser_tools.tools,
            chat_ctx=chat_ctx,
        )

    @function_tool()
    async def back_to_jarvis(self, context: RunContext) -> tuple[Agent, str]:
        """Hand back to the main assistant when the web task is done or the user
        wants something that isn't web work."""
        return JarvisAgent(self.services, self._handoff_ctx()), "Back to Jarvis."


class CodingAgent(_JarvisBase):
    """Coding specialist: delegates work to Claude Code or OpenCode."""

    def __init__(self, services: JarvisServices, chat_ctx: Any = None) -> None:
        names = ", ".join(sorted(services.runner.workspaces)) or "none yet"
        super().__init__(
            services,
            instructions=(
                CODING_INSTRUCTIONS
                + f"\n# Approved workspaces\n\n{names}\n"
                + "If there are none, tell the user to add their project folders "
                "to config/workspaces.toml."
            ),
            tools=[],
            chat_ctx=chat_ctx,
        )

    @function_tool()
    async def start_coding_task(
        self,
        context: RunContext,
        workspace: str,
        task: str,
        allow_shell: bool = False,
    ) -> str:
        """Start a coding agent on a task in one of the approved workspaces. It runs
        in the background and Jarvis reports progress automatically.

        Args:
            workspace: The name of an approved workspace.
            task: A complete, self-contained description of what to do.
            allow_shell: Let the coding agent run commands. Only true after the user
                explicitly agreed to shell access for this task.
        """
        session = context.session
        narrator = ProgressNarrator(
            lambda instructions: session.generate_reply(instructions=instructions)
        )
        try:
            job = await self.services.runner.start(
                workspace,
                task,
                allow_shell=allow_shell,
                on_progress=narrator.on_progress,
                on_finish=narrator.on_finish,
            )
        except DelegateError as exc:
            raise ToolError(str(exc)) from exc
        return (
            f"Started in the {job.workspace} workspace. Tell the user you'll report "
            "back, then hand back to the main assistant."
        )

    @function_tool()
    async def coding_task_status(self, context: RunContext) -> str:
        """Check how the current or most recent coding task is going."""
        return self._coding_status()

    @function_tool()
    async def cancel_coding_task(self, context: RunContext) -> str:
        """Stop the running coding task."""
        if await self.services.runner.cancel():
            return "Stopping the coding task."
        return "No coding task is running."

    @function_tool()
    async def back_to_jarvis(self, context: RunContext) -> tuple[Agent, str]:
        """Hand back to the main assistant once a task is started, or when the user
        wants something that isn't coding work."""
        return JarvisAgent(self.services, self._handoff_ctx()), "Back to Jarvis."
