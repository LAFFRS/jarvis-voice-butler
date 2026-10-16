from pathlib import Path

import pytest

import modes
from modes import ConfigError, build_session_models, load_config, save_mode


@pytest.fixture
def modes_file(tmp_path: Path) -> Path:
    path = tmp_path / "modes.toml"
    path.write_text(modes.MODES_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_shipped_config_defaults_to_cloud(modes_file: Path) -> None:
    config = load_config(modes_file, env={})

    assert config.mode == "cloud"
    assert config.coder.backend == "claude"
    assert "Bash(git push:*)" in config.coder.disallowed_tools
    assert "Bash" not in config.coder.allowed_tools


def test_env_overrides_mode_and_coder_follows(modes_file: Path) -> None:
    config = load_config(modes_file, env={"JARVIS_MODE": "local"})

    assert config.mode == "local"
    assert config.coder.backend == "opencode"
    assert config.coder.model == "ollama/qwen3-coder:30b"


def test_coder_can_be_mixed_with_voice_mode(modes_file: Path) -> None:
    config = load_config(modes_file, env={"JARVIS_CODER": "local"})

    assert config.mode == "cloud"
    assert config.coder.backend == "opencode"


def test_invalid_mode_is_rejected(modes_file: Path) -> None:
    with pytest.raises(ConfigError, match="'cloud' or 'local'"):
        load_config(modes_file, env={"JARVIS_MODE": "turbo"})


def test_missing_section_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "modes.toml"
    path.write_text('mode = "cloud"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"\[cloud\]"):
        load_config(path, env={})


def test_save_mode_rewrites_only_mode_line(modes_file: Path) -> None:
    before = modes_file.read_text(encoding="utf-8")

    save_mode("local", modes_file)

    after = modes_file.read_text(encoding="utf-8")
    assert load_config(modes_file, env={}).mode == "local"
    changed = [
        (old, new)
        for old, new in zip(before.splitlines(), after.splitlines(), strict=True)
        if old != new
    ]
    assert changed == [('mode = "cloud"', 'mode = "local"')]


def test_save_mode_rejects_unknown_mode(modes_file: Path) -> None:
    with pytest.raises(ConfigError):
        save_mode("turbo", modes_file)


def test_local_mode_builds_local_pipeline(modes_file: Path) -> None:
    config = load_config(modes_file, env={"JARVIS_MODE": "local"})

    models = build_session_models(config)

    assert set(models) == {"stt", "llm", "tts"}
    assert models["llm"].model == "qwen3-coder:30b"


def test_cloud_mode_builds_realtime_model(
    modes_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    config = load_config(modes_file, env={})

    models = build_session_models(config)

    assert list(models) == ["llm"]
    assert models["llm"].model == "gemini-3.1-flash-live-preview"
