from pathlib import Path
from types import SimpleNamespace

import pytest
from livekit.agents.llm import ToolError

import modes
from agents import (
    BrowserAgent,
    CodingAgent,
    JarvisAgent,
    JarvisServices,
    ProgressNarrator,
)
from browser import BrowserManager
from delegate import CodingJob, CodingRunner, DelegateError, ProgressEvent


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JarvisServices:
    monkeypatch.delenv("JARVIS_MODE", raising=False)
    modes_file = tmp_path / "modes.toml"
    modes_file.write_text(
        modes.MODES_FILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = modes.load_config(modes_file, env={})
    return JarvisServices(
        config=config,
        browser=BrowserManager(headless=True),
        runner=CodingRunner(config.coder, {"website": tmp_path}),
        modes_file=modes_file,
    )


def tool_ids(agent) -> set[str]:
    return {tool.id for tool in agent.tools}


def test_each_agent_only_has_its_own_tools(services: JarvisServices) -> None:
    assert tool_ids(JarvisAgent(services)) == {
        "transfer_to_coding",
        "transfer_to_web",
        "coding_task_status",
        "switch_brain_mode",
        "end_call",
    }
    assert tool_ids(CodingAgent(services)) == {
        "start_coding_task",
        "coding_task_status",
        "cancel_coding_task",
        "back_to_jarvis",
        "end_call",
    }
    browser_tools = tool_ids(BrowserAgent(services))
    assert {
        "open_url",
        "search_the_web",
        "save_research_report",
        "back_to_jarvis",
    } <= browser_tools
    assert "start_coding_task" not in browser_tools


def test_coding_agent_is_told_the_approved_workspaces(services: JarvisServices) -> None:
    assert "website" in CodingAgent(services).instructions


async def test_handoffs_return_the_right_specialist(services: JarvisServices) -> None:
    jarvis = JarvisAgent(services)

    coding, _ = await jarvis.transfer_to_coding(None)
    web, _ = await jarvis.transfer_to_web(None)
    back, _ = await coding.back_to_jarvis(None)

    assert isinstance(coding, CodingAgent)
    assert isinstance(web, BrowserAgent)
    assert isinstance(back, JarvisAgent)
    assert coding.services is services


async def test_switch_brain_mode_saves_the_mode(services: JarvisServices) -> None:
    reply = await JarvisAgent(services).switch_brain_mode(None, "Local")

    assert "local mode" in reply
    assert modes.load_config(services.modes_file, env={}).mode == "local"


async def test_switch_brain_mode_rejects_nonsense(services: JarvisServices) -> None:
    with pytest.raises(ToolError):
        await JarvisAgent(services).switch_brain_mode(None, "turbo")


async def test_status_without_a_task(services: JarvisServices) -> None:
    assert "No coding task" in await JarvisAgent(services).coding_task_status(None)


class FakeRunner:
    def __init__(self, workspaces: dict[str, Path], fail: bool = False) -> None:
        self.workspaces = workspaces
        self.current = None
        self.fail = fail
        self.calls: list[dict] = []

    async def start(self, workspace, task, **kwargs):
        self.calls.append({"workspace": workspace, "task": task, **kwargs})
        if self.fail:
            raise DelegateError("'nope' is not an approved workspace.")
        self.current = CodingJob(workspace=workspace, folder=Path("."), task=task)
        return self.current

    async def cancel(self) -> bool:
        return False


async def test_start_coding_task_passes_shell_consent_through(
    services: JarvisServices,
) -> None:
    services.runner = FakeRunner(services.runner.workspaces)
    spoken: list[str] = []
    context = SimpleNamespace(
        session=SimpleNamespace(
            generate_reply=lambda instructions: spoken.append(instructions)
        )
    )

    reply = await CodingAgent(services).start_coding_task(
        context, "website", "add a contact form"
    )

    call = services.runner.calls[0]
    assert call["allow_shell"] is False
    assert call["task"] == "add a contact form"
    assert "Started" in reply
    # Progress is spoken through the session.
    call["on_progress"](
        services.runner.current, ProgressEvent("tool", "editing contact.html")
    )
    assert "editing contact.html" in spoken[0]


async def test_start_coding_task_surfaces_errors(services: JarvisServices) -> None:
    services.runner = FakeRunner({}, fail=True)
    context = SimpleNamespace(session=SimpleNamespace(generate_reply=lambda **_: None))

    with pytest.raises(ToolError, match="not an approved workspace"):
        await CodingAgent(services).start_coding_task(context, "nope", "x")


def test_narrator_throttles_progress_but_always_reports_the_end() -> None:
    spoken: list[str] = []
    now = [0.0]
    narrator = ProgressNarrator(spoken.append, min_interval=45, clock=lambda: now[0])
    job = CodingJob(workspace="website", folder=Path("."), task="x")

    narrator.on_progress(job, ProgressEvent("tool", "reading the code"))
    now[0] = 10
    narrator.on_progress(job, ProgressEvent("tool", "editing a.py"))
    narrator.on_progress(job, ProgressEvent("text", "thinking out loud"))
    now[0] = 60
    narrator.on_progress(job, ProgressEvent("tool", "editing b.py"))
    job.status, job.summary = "done", "Added the form."
    narrator.on_finish(job)

    assert len(spoken) == 3
    assert "reading the code" in spoken[0]
    assert "editing b.py" in spoken[1]
    assert "Added the form." in spoken[2]


async def test_reverse_engineering_tasks_get_the_analysis_brief(
    services: JarvisServices,
) -> None:
    services.runner = FakeRunner(services.runner.workspaces)
    context = SimpleNamespace(session=SimpleNamespace(generate_reply=lambda **_: None))

    await CodingAgent(services).start_coding_task(
        context,
        "website",
        "Work out the save file format of game.exe",
        allow_shell=True,
        reverse_engineering=True,
    )

    task = services.runner.calls[0]["task"]
    assert task.startswith("# Reverse engineering brief")
    assert "Never run an unknown binary" in task
    assert task.endswith("Work out the save file format of game.exe")


async def test_web_agent_saves_research_reports(
    services: JarvisServices, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_REPORTS_DIR", str(tmp_path / "reports"))

    reply = await BrowserAgent(services).save_research_report(
        None, "Rust vs Go", "## Speed\n\nBoth are fast.", ["https://a.example"]
    )

    saved = list((tmp_path / "reports").glob("*-rust-vs-go.md"))
    assert len(saved) == 1
    assert saved[0].name in reply
    assert "https://a.example" in saved[0].read_text(encoding="utf-8")
