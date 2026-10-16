import asyncio
import json
import sys
from pathlib import Path

import pytest

from delegate import (
    CodingRunner,
    DelegateError,
    ProgressEvent,
    build_invocation,
    describe_tool,
    load_workspaces,
    parse_claude_line,
    parse_opencode_line,
    resolve_executable,
    resolve_mcp_config,
    resolve_workspace,
)
from modes import CoderSettings

CLAUDE = CoderSettings(
    backend="claude",
    allowed_tools=("Read", "Edit"),
    shell_tools=("Bash",),
    disallowed_tools=("Bash(git push:*)",),
)
OPENCODE = CoderSettings(
    backend="opencode", model="ollama/qwen3-coder:30b", auto_approve=True
)


def fake_which(name: str) -> str:
    return f"/usr/bin/{name}"


# --- workspaces ------------------------------------------------------------ #


def test_load_workspaces_lowercases_names(tmp_path: Path) -> None:
    config = tmp_path / "workspaces.toml"
    config.write_text(
        f'[workspaces]\nWebsite = "{tmp_path.as_posix()}"\n', encoding="utf-8"
    )

    assert load_workspaces(config) == {"website": tmp_path}


def test_unknown_workspace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DelegateError, match="not an approved workspace"):
        resolve_workspace({"website": tmp_path}, "system32")


def test_missing_workspace_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DelegateError, match="does not exist"):
        resolve_workspace({"website": tmp_path / "gone"}, "Website")


# --- parsing --------------------------------------------------------------- #


def test_describe_tool_names_the_file_being_edited() -> None:
    assert describe_tool("Edit", {"file_path": "C:\\code\\app\\auth.py"}) == (
        "editing auth.py"
    )
    assert describe_tool("Grep") == "reading the code"
    assert describe_tool("Bash") == "running a command"


def test_parse_claude_assistant_and_result_lines() -> None:
    assistant = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Adding the form."},
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/p/contact.html"},
                    },
                ]
            },
        }
    )
    result = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "Done."}
    )

    assert parse_claude_line(assistant) == [
        ProgressEvent("text", "Adding the form."),
        ProgressEvent("tool", "editing contact.html"),
    ]
    assert parse_claude_line(result) == [ProgressEvent("result", "Done.")]
    assert parse_claude_line("not json") == []


def test_parse_claude_error_result() -> None:
    line = json.dumps({"type": "result", "subtype": "error_max_turns", "result": ""})

    assert parse_claude_line(line)[0].kind == "error"


def test_parse_opencode_events_by_shape() -> None:
    tool = json.dumps(
        {
            "type": "tool_use",
            "part": {"tool": "edit", "state": {"input": {"filePath": "a/b.ts"}}},
        }
    )
    text = json.dumps({"type": "text", "part": {"text": "All done."}})
    error = json.dumps({"type": "error", "error": {"message": "model not found"}})

    assert parse_opencode_line(tool) == [ProgressEvent("tool", "editing b.ts")]
    assert parse_opencode_line(text) == [ProgressEvent("text", "All done.")]
    assert parse_opencode_line(error) == [ProgressEvent("error", "model not found")]


# --- command building ------------------------------------------------------ #


def test_claude_command_keeps_shell_off_by_default(tmp_path: Path) -> None:
    inv = build_invocation(CLAUDE, "add tests", tmp_path, which=fake_which)

    assert inv.argv[:2] == ["/usr/bin/claude", "-p"]
    assert inv.argv[inv.argv.index("--allowedTools") + 1] == "Read,Edit"
    assert inv.argv[inv.argv.index("--disallowedTools") + 1] == "Bash(git push:*)"
    assert inv.stdin == "add tests"


def test_claude_command_adds_shell_only_when_allowed(tmp_path: Path) -> None:
    inv = build_invocation(
        CLAUDE, "run the tests", tmp_path, allow_shell=True, which=fake_which
    )

    assert inv.argv[inv.argv.index("--allowedTools") + 1] == "Read,Edit,Bash"


def test_opencode_command_attaches_task_file(tmp_path: Path) -> None:
    task_file = tmp_path / "task.md"
    inv = build_invocation(
        OPENCODE, "x", tmp_path, task_file=task_file, which=fake_which
    )

    assert inv.argv[:4] == ["/usr/bin/opencode", "run", "--format", "json"]
    assert inv.argv[inv.argv.index("--file") + 1] == str(task_file)
    assert inv.argv[inv.argv.index("--model") + 1] == "ollama/qwen3-coder:30b"
    assert "--auto" in inv.argv
    assert inv.stdin is None


def test_windows_cmd_shims_run_through_cmd() -> None:
    argv = resolve_executable("claude", lambda _: "C:\\npm\\claude.CMD")

    assert argv == ["cmd.exe", "/c", "C:\\npm\\claude.CMD"]


def test_missing_executable_is_reported() -> None:
    with pytest.raises(DelegateError, match="not installed"):
        resolve_executable("claude", lambda _: None)


# --- running a fake coding agent ------------------------------------------ #

FAKE_CLAUDE = """
import json, sys, time
task = sys.stdin.read()
mode = sys.argv[-1]
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({"type": "system", "subtype": "init"})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Edit", "input": {"file_path": "main.py"}}]}})
if "slow" in task:
    time.sleep(30)
if "fail" in task:
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": "Could not finish."})
    sys.exit(1)
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": "Finished: " + task})
"""


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    return str(script)


def make_runner(tmp_path: Path, fake_claude: str, monkeypatch: pytest.MonkeyPatch):
    import delegate

    # Run the fake agent with this Python interpreter instead of a real claude.
    monkeypatch.setattr(
        delegate,
        "resolve_executable",
        lambda name, which=None: [sys.executable, fake_claude],
    )
    return CodingRunner(CLAUDE, {"project": tmp_path})


async def test_runner_reports_progress_and_result(
    tmp_path: Path, fake_claude: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = make_runner(tmp_path, fake_claude, monkeypatch)
    progress: list[ProgressEvent] = []
    finished = asyncio.Event()

    job = await runner.start(
        "Project",
        "add a contact form",
        on_progress=lambda _job, event: progress.append(event),
        on_finish=lambda _job: finished.set(),
    )
    await asyncio.wait_for(finished.wait(), timeout=20)

    assert job.status == "done"
    assert job.summary == "Finished: add a contact form"
    assert job.steps == ["editing main.py"]
    assert ProgressEvent("tool", "editing main.py") in progress
    assert not runner.busy


async def test_runner_reports_failure(
    tmp_path: Path, fake_claude: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = make_runner(tmp_path, fake_claude, monkeypatch)

    job = await runner.start("project", "this will fail")
    await asyncio.wait_for(runner.wait(), timeout=20)

    assert job.status == "failed"
    assert job.summary == "Could not finish."


async def test_runner_allows_one_task_at_a_time_and_cancels(
    tmp_path: Path, fake_claude: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = make_runner(tmp_path, fake_claude, monkeypatch)
    finished: list[str] = []

    job = await runner.start(
        "project", "slow task", on_finish=lambda j: finished.append(j.status)
    )
    with pytest.raises(DelegateError, match="already running"):
        await runner.start("project", "another")

    assert await runner.cancel()
    await asyncio.wait_for(runner.wait(), timeout=20)

    assert job.status == "cancelled"
    assert finished == ["cancelled"]
    assert "cancelled" in job.describe()


async def test_runner_refuses_unapproved_workspace(
    tmp_path: Path, fake_claude: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = make_runner(tmp_path, fake_claude, monkeypatch)

    with pytest.raises(DelegateError, match="not an approved workspace"):
        await runner.start("windows", "delete everything")
    assert runner.current is None


def test_mcp_config_is_passed_when_the_file_exists(tmp_path: Path) -> None:
    config = tmp_path / "coder-mcp.json"
    config.write_text('{"mcpServers": {}}', encoding="utf-8")
    coder = CoderSettings(backend="claude", mcp_config=str(config))

    inv = build_invocation(coder, "x", tmp_path, which=fake_which)

    assert inv.argv[inv.argv.index("--mcp-config") + 1] == str(config)


def test_missing_mcp_config_is_skipped(tmp_path: Path) -> None:
    coder = CoderSettings(backend="claude", mcp_config="config/nope.json")

    inv = build_invocation(coder, "x", tmp_path, which=fake_which)

    assert "--mcp-config" not in inv.argv


def test_relative_mcp_config_resolves_against_project(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "coder-mcp.json").write_text("{}", encoding="utf-8")

    assert resolve_mcp_config("config/coder-mcp.json", base=tmp_path) == (
        tmp_path / "config" / "coder-mcp.json"
    )
    assert resolve_mcp_config("", base=tmp_path) is None
