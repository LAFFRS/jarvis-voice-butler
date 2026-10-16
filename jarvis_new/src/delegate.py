"""Runs a real coding agent (Claude Code or OpenCode) on a task in a project folder.

Jarvis stays responsive while the coding agent works: `CodingRunner.start`
returns immediately, and progress and the final result arrive through
callbacks that the voice agent turns into short spoken updates.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Literal

from modes import CONFIG_DIR, CoderSettings

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

logger = logging.getLogger("jarvis.delegate")

WORKSPACES_FILE = CONFIG_DIR / "workspaces.toml"


class DelegateError(RuntimeError):
    """A coding task could not be started or controlled."""


# --------------------------------------------------------------------------- #
# Workspaces: the only folders a coding agent may work in.
# --------------------------------------------------------------------------- #


def load_workspaces(path: Path = WORKSPACES_FILE) -> dict[str, Path]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("workspaces", {})
    return {
        str(name).lower(): Path(str(folder)).expanduser()
        for name, folder in entries.items()
    }


def resolve_workspace(workspaces: dict[str, Path], name: str) -> Path:
    key = name.strip().lower()
    if key not in workspaces:
        known = ", ".join(sorted(workspaces)) or "none configured"
        raise DelegateError(
            f"'{name}' is not an approved workspace. Approved workspaces: {known}."
        )
    folder = workspaces[key]
    if not folder.is_dir():
        raise DelegateError(f"The '{key}' workspace folder does not exist: {folder}")
    return folder


# --------------------------------------------------------------------------- #
# Output parsing: turn each agent's JSON event stream into progress events.
# --------------------------------------------------------------------------- #

EventKind = Literal["tool", "text", "result", "error"]


@dataclass(frozen=True)
class ProgressEvent:
    kind: EventKind
    text: str


def describe_tool(name: str, tool_input: dict[str, Any] | None = None) -> str:
    """A short, speakable description of what the coding agent is doing."""
    tool_input = tool_input or {}
    lowered = name.lower()
    target = tool_input.get("file_path") or tool_input.get("filePath") or ""
    filename = PurePath(str(target).replace("\\", "/")).name if target else ""
    if lowered in ("edit", "write", "multiedit", "notebookedit", "patch"):
        return f"editing {filename}" if filename else "editing files"
    if lowered in ("read", "glob", "grep", "list", "ls"):
        return "reading the code"
    if lowered in ("bash", "shell"):
        return "running a command"
    if lowered in ("websearch", "webfetch"):
        return "looking something up online"
    if lowered in ("todowrite", "task", "todo"):
        return "planning the work"
    return f"using {name}"


def parse_claude_line(line: str) -> list[ProgressEvent]:
    """Parse one line of `claude -p --output-format stream-json` output."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []

    if event.get("type") == "assistant":
        content = (event.get("message") or {}).get("content") or []
        events: list[ProgressEvent] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                events.append(
                    ProgressEvent(
                        "tool", describe_tool(block.get("name", ""), block.get("input"))
                    )
                )
            elif block.get("type") == "text" and block.get("text", "").strip():
                events.append(ProgressEvent("text", block["text"].strip()))
        return events

    if event.get("type") == "result":
        text = str(event.get("result") or "").strip()
        if event.get("is_error") or event.get("subtype", "success") != "success":
            return [
                ProgressEvent("error", text or "The coding agent reported an error.")
            ]
        return [ProgressEvent("result", text)]

    return []


def parse_opencode_line(line: str) -> list[ProgressEvent]:
    """Parse one line of `opencode run --format json` output.

    OpenCode doesn't document its event schema, so this reads events by shape:
    a `part` (or the event itself) with a tool name is a tool step, and one
    with text is a message. The last text message becomes the result.
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []

    if event.get("type") == "error" or "error" in event:
        err = event.get("error")
        message = err.get("message") if isinstance(err, dict) else err
        return [ProgressEvent("error", str(message or "OpenCode reported an error."))]

    part = event.get("part") if isinstance(event.get("part"), dict) else event
    tool = part.get("tool")
    if isinstance(tool, str) and tool:
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
        return [ProgressEvent("tool", describe_tool(tool, tool_input))]

    text = part.get("text")
    if isinstance(text, str) and text.strip():
        return [ProgressEvent("text", text.strip())]
    return []


# --------------------------------------------------------------------------- #
# Command building.
# --------------------------------------------------------------------------- #


def resolve_executable(
    name: str, which: Callable[[str], str | None] = shutil.which
) -> list[str]:
    path = which(name)
    if path is None:
        raise DelegateError(
            f"'{name}' is not installed or not on PATH. See SETUP-WINDOWS.md."
        )
    # npm installs Windows command shims as .cmd files, which need cmd.exe.
    if path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", path]
    return [path]


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    stdin: str | None
    parse_line: Callable[[str], list[ProgressEvent]]


def build_invocation(
    coder: CoderSettings,
    task: str,
    workdir: Path,
    *,
    allow_shell: bool = False,
    task_file: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Invocation:
    if coder.backend == "claude":
        allowed = list(coder.allowed_tools)
        if allow_shell:
            allowed += list(coder.shell_tools)
        argv = [
            *resolve_executable("claude", which),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            coder.permission_mode,
        ]
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        if coder.disallowed_tools:
            argv += ["--disallowedTools", ",".join(coder.disallowed_tools)]
        if coder.model:
            argv += ["--model", coder.model]
        # The task goes through stdin so quoting can't break on Windows.
        return Invocation(argv=argv, stdin=task, parse_line=parse_claude_line)

    if task_file is None:
        raise DelegateError("OpenCode tasks need a task file.")
    argv = [
        *resolve_executable("opencode", which),
        "run",
        "--format",
        "json",
        "--dir",
        str(workdir),
        "--file",
        str(task_file),
    ]
    if coder.model:
        argv += ["--model", coder.model]
    if coder.auto_approve:
        argv.append("--auto")
    argv.append("Carry out the task described in the attached file.")
    return Invocation(argv=argv, stdin=None, parse_line=parse_opencode_line)


# --------------------------------------------------------------------------- #
# The runner.
# --------------------------------------------------------------------------- #

JobStatus = Literal["running", "done", "failed", "cancelled"]
ProgressCallback = Callable[["CodingJob", ProgressEvent], Awaitable[None] | None]
FinishCallback = Callable[["CodingJob"], Awaitable[None] | None]


@dataclass
class CodingJob:
    workspace: str
    folder: Path
    task: str
    status: JobStatus = "running"
    steps: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    summary: str = ""
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    def describe(self) -> str:
        last = self.steps[-1] if self.steps else "getting started"
        if self.status == "running":
            return (
                f"Working in {self.workspace} for {self.elapsed_seconds} seconds, "
                f"{len(self.steps)} steps so far. Currently {last}."
            )
        return f"Task in {self.workspace} {self.status}. {self.summary}".strip()


async def _call(callback: Callable[..., Any] | None, *args: Any) -> None:
    if callback is None:
        return
    try:
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception("coding task callback failed")


class CodingRunner:
    """Runs one coding task at a time in an approved workspace."""

    def __init__(
        self,
        coder: CoderSettings,
        workspaces: dict[str, Path],
        *,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.coder = coder
        self.workspaces = workspaces
        self._which = which
        self.current: CodingJob | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.status == "running"

    async def start(
        self,
        workspace: str,
        task: str,
        *,
        allow_shell: bool = False,
        on_progress: ProgressCallback | None = None,
        on_finish: FinishCallback | None = None,
    ) -> CodingJob:
        if self.busy:
            raise DelegateError("A coding task is already running. Cancel it first.")
        if not task.strip():
            raise DelegateError("The coding task description is empty.")
        folder = resolve_workspace(self.workspaces, workspace)

        task_file: Path | None = None
        if self.coder.backend == "opencode":
            fd, name = tempfile.mkstemp(prefix="jarvis-task-", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(task)
            task_file = Path(name)

        invocation = build_invocation(
            self.coder,
            task,
            folder,
            allow_shell=allow_shell,
            task_file=task_file,
            which=self._which,
        )
        logger.info("starting coding task in %s: %s", folder, invocation.argv[:4])
        self._process = await asyncio.create_subprocess_exec(
            *invocation.argv,
            cwd=str(folder),
            stdin=asyncio.subprocess.PIPE if invocation.stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        job = CodingJob(workspace=workspace.strip().lower(), folder=folder, task=task)
        self.current = job
        self._task = asyncio.create_task(
            self._watch(job, invocation, task_file, on_progress, on_finish)
        )
        return job

    async def _watch(
        self,
        job: CodingJob,
        invocation: Invocation,
        task_file: Path | None,
        on_progress: ProgressCallback | None,
        on_finish: FinishCallback | None,
    ) -> None:
        proc = self._process
        assert proc is not None and proc.stdout is not None and proc.stderr is not None
        error: str | None = None
        result: str | None = None
        shutting_down = False
        try:
            if invocation.stdin is not None and proc.stdin is not None:
                proc.stdin.write(invocation.stdin.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()

            stderr_task = asyncio.create_task(proc.stderr.read())
            async for raw in proc.stdout:
                for event in invocation.parse_line(raw.decode("utf-8", "replace")):
                    if event.kind == "tool":
                        job.steps.append(event.text)
                    elif event.kind == "text":
                        job.messages.append(event.text)
                    elif event.kind == "result":
                        result = event.text
                    elif event.kind == "error":
                        error = event.text
                    await _call(on_progress, job, event)
            code = await proc.wait()
            stderr = (await stderr_task).decode("utf-8", "replace").strip()

            if job.status == "cancelled":
                job.summary = "Cancelled at your request."
            elif error or code != 0:
                job.status = "failed"
                job.summary = error or stderr[-500:] or f"Exited with code {code}."
            else:
                job.status = "done"
                job.summary = result or (job.messages[-1] if job.messages else "")
        except asyncio.CancelledError:
            # The watcher itself was cancelled (Jarvis is shutting down).
            shutting_down = True
            job.status = "cancelled"
            job.summary = "Cancelled."
            raise
        except Exception as exc:
            logger.exception("coding task crashed")
            job.status = "failed"
            job.summary = str(exc)
        finally:
            if task_file is not None:
                with contextlib.suppress(OSError):
                    task_file.unlink()
            self._process = None
            if not shutting_down:
                await _call(on_finish, job)

    async def cancel(self) -> bool:
        proc = self._process
        if not self.busy or proc is None:
            return False
        assert self.current is not None
        self.current.status = "cancelled"
        await _kill_tree(proc)
        return True

    async def wait(self) -> None:
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def close(self) -> None:
        await self.cancel()
        await self.wait()


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Stop the coding agent and everything it started."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        # terminate() would only stop the cmd.exe shim, not node underneath.
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
