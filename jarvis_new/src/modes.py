"""The brain switch: loads config/modes.toml and builds the session's models.

Cloud mode uses Gemini Live (one realtime model handles listening, thinking and
speaking). Local mode chains three local services instead: an OpenAI-compatible
speech-to-text server, Ollama, and an OpenAI-compatible text-to-speech server.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
MODES_FILE = CONFIG_DIR / "modes.toml"

Mode = Literal["cloud", "local"]
MODES: tuple[Mode, ...] = ("cloud", "local")


class ConfigError(ValueError):
    """Raised when config/modes.toml is missing values or has invalid ones."""


@dataclass(frozen=True)
class CoderSettings:
    backend: Literal["claude", "opencode"]
    model: str = ""
    permission_mode: str = "acceptEdits"
    allowed_tools: tuple[str, ...] = ()
    shell_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    auto_approve: bool = False


@dataclass(frozen=True)
class CloudSettings:
    realtime_model: str
    voice: str
    language: str
    noise_cancellation: bool
    coder: CoderSettings


@dataclass(frozen=True)
class LocalSettings:
    ollama_url: str
    llm_model: str
    stt_url: str
    stt_model: str
    tts_url: str
    tts_model: str
    tts_voice: str
    language: str
    coder: CoderSettings


@dataclass(frozen=True)
class JarvisConfig:
    mode: Mode
    coder_mode: Mode
    cloud: CloudSettings
    local: LocalSettings
    source: Path | None = field(default=None, compare=False)

    @property
    def coder(self) -> CoderSettings:
        return self.cloud.coder if self.coder_mode == "cloud" else self.local.coder


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise ConfigError(f"modes.toml is missing the [{name}] section.")
    return value


def _require(data: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"modes.toml [{where}] is missing '{key}'.")
    return data[key]


def _check_mode(value: Any, what: str) -> Mode:
    if value not in MODES:
        raise ConfigError(f"{what} must be 'cloud' or 'local', got {value!r}.")
    return value


def _coder(data: Mapping[str, Any], where: str) -> CoderSettings:
    backend = _require(data, "backend", where)
    if backend not in ("claude", "opencode"):
        raise ConfigError(
            f"modes.toml [{where}] backend must be 'claude' or 'opencode'."
        )
    return CoderSettings(
        backend=backend,
        model=str(data.get("model", "")),
        permission_mode=str(data.get("permission_mode", "acceptEdits")),
        allowed_tools=tuple(data.get("allowed_tools", ())),
        shell_tools=tuple(data.get("shell_tools", ())),
        disallowed_tools=tuple(data.get("disallowed_tools", ())),
        auto_approve=bool(data.get("auto_approve", False)),
    )


def parse_config(
    data: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    source: Path | None = None,
) -> JarvisConfig:
    env = os.environ if env is None else env
    mode = _check_mode(env.get("JARVIS_MODE") or data.get("mode", "cloud"), "mode")

    coder_value = env.get("JARVIS_CODER") or data.get("coder", "auto")
    coder_mode = mode if coder_value == "auto" else _check_mode(coder_value, "coder")

    cloud = _section(data, "cloud")
    local = _section(data, "local")

    def req(section: Mapping[str, Any], key: str, where: str) -> str:
        return str(_require(section, key, where))

    return JarvisConfig(
        mode=mode,
        coder_mode=coder_mode,
        cloud=CloudSettings(
            realtime_model=req(cloud, "realtime_model", "cloud"),
            voice=req(cloud, "voice", "cloud"),
            language=req(cloud, "language", "cloud"),
            noise_cancellation=bool(cloud.get("noise_cancellation", True)),
            coder=_coder(_section(cloud, "coder"), "cloud.coder"),
        ),
        local=LocalSettings(
            ollama_url=req(local, "ollama_url", "local"),
            llm_model=req(local, "llm_model", "local"),
            stt_url=req(local, "stt_url", "local"),
            stt_model=req(local, "stt_model", "local"),
            tts_url=req(local, "tts_url", "local"),
            tts_model=req(local, "tts_model", "local"),
            tts_voice=req(local, "tts_voice", "local"),
            language=req(local, "language", "local"),
            coder=_coder(_section(local, "coder"), "local.coder"),
        ),
        source=source,
    )


def load_config(
    path: Path = MODES_FILE, env: Mapping[str, str] | None = None
) -> JarvisConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML: {exc}") from exc
    return parse_config(data, env=env, source=path)


_MODE_LINE = re.compile(r'^mode\s*=\s*"[^"]*"', re.MULTILINE)


def save_mode(mode: str, path: Path = MODES_FILE) -> None:
    """Rewrite the top-level `mode = "..."` line, keeping comments intact."""
    _check_mode(mode, "mode")
    text = path.read_text(encoding="utf-8")
    new_text, count = _MODE_LINE.subn(f'mode = "{mode}"', text, count=1)
    if count == 0:
        new_text = f'mode = "{mode}"\n' + text
    path.write_text(new_text, encoding="utf-8")


def build_session_models(config: JarvisConfig) -> dict[str, Any]:
    """Keyword arguments for AgentSession for the active mode.

    Agents don't set their own models, so every agent (and every handoff)
    automatically uses whichever brain is active for the session.
    """
    if config.mode == "cloud":
        from google.genai import types as genai_types
        from livekit.plugins import google

        c = config.cloud
        return {
            "llm": google.beta.realtime.RealtimeModel(
                model=c.realtime_model,
                voice=c.voice,
                language=c.language,
                tool_response_scheduling=genai_types.FunctionResponseScheduling.WHEN_IDLE,
            ),
        }

    from livekit.plugins import openai

    lc = config.local
    # Local OpenAI-compatible servers ignore the key, but the client requires one.
    return {
        "stt": openai.STT(
            model=lc.stt_model,
            language=lc.language,
            base_url=lc.stt_url,
            api_key="local",
        ),
        "llm": openai.LLM.with_ollama(model=lc.llm_model, base_url=lc.ollama_url),
        "tts": openai.TTS(
            model=lc.tts_model,
            voice=lc.tts_voice,
            base_url=lc.tts_url,
            api_key="local",
        ),
    }


if __name__ == "__main__":
    # Used by jarvis.ps1 to find out which mode is active.
    print(load_config().mode)
