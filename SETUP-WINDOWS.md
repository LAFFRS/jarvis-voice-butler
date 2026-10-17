# Setting up Jarvis on Windows

This gets the whole system running on your PC: the Jarvis voice agent, the web
UI, coding tasks, and optionally local (offline) mode.

Run all commands in **PowerShell**.

## 1. Install the basics

```powershell
winget install astral-sh.uv          # Python package manager
winget install OpenJS.NodeJS.LTS     # Node.js (web UI and coding agents)
winget install Git.Git
npm install -g pnpm
```

Close and reopen PowerShell afterwards so the new commands are found.

## 2. Install the agent and the web UI

```powershell
cd jarvis-voice-butler\jarvis_new
uv sync
uv run playwright install chromium

cd frontend
pnpm install
cd ..
```

## 3. Add your keys

```powershell
copy .env.example .env.local
copy frontend\.env.example frontend\.env.local
notepad .env.local
```

Fill in `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` from your
project at <https://cloud.livekit.io>, and `GOOGLE_API_KEY` from
<https://aistudio.google.com/apikey>. Put the same three LiveKit values in
`frontend\.env.local` too, and set `AGENT_NAME=my-agent` there. The launcher
sets this for you, but you need it if you ever start the web UI by hand.

## 4. Tell Jarvis which project folders it may code in

Edit `jarvis_new\config\workspaces.toml`:

```toml
[workspaces]
jarvis = "C:/Users/you/code/jarvis-voice-butler"
website = "C:/Users/you/code/my-website"
```

The names on the left are what you say out loud ("Jarvis, in my website
project…"). Jarvis refuses to run coding tasks anywhere else.

## 5. Install a coding agent

**Cloud mode – Claude Code** (needs a Claude Pro/Max plan or an Anthropic API key):

```powershell
npm install -g @anthropic-ai/claude-code
claude          # run once to sign in, then exit
```

**Local mode – OpenCode** (free, uses your GPU):

```powershell
npm install -g opencode-ai
```

## 6. Run it

From the `jarvis-voice-butler` folder:

```powershell
.\jarvis.ps1
```

This opens the agent and the web UI in their own windows and opens
<http://localhost:3000> in your browser. Other options:

| Command | What it does |
|---|---|
| `.\jarvis.ps1 -Mode local` | Use local mode for this run |
| `.\jarvis.ps1 -Console` | Talk to Jarvis in the terminal only |
| `.\jarvis.ps1 -NoFrontend` | Agent only |
| `.\jarvis.ps1 -Phone` | Also let the phone app connect over Wi-Fi (see below) |

If PowerShell refuses to run the script, allow local scripts once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## Things to say

- "Jarvis, in my website project, add a dark mode toggle." – starts a coding task
- "How's that task going?" / "Stop the task." – checks on or cancels it
- "You can run the tests for that one." – gives the coding agent shell access for the task
- "Research the best Python web frameworks this year." – web research; a full
  write-up is saved to `%USERPROFILE%\Jarvis\reports`
- "In my sandbox project, reverse engineer the save file format of game.exe." –
  analysis report saved in that project's `reports` folder
- "What's this error on my screen?" – share your screen in the web UI first (cloud mode)
- "Switch to local mode." – takes effect from the next session

## Optional: plug-in tools for Jarvis

Jarvis can use extra tools from any MCP server (GitHub, a calendar, a database,
smart home, …). List them in `jarvis_new\config\jarvis-mcp.json`, in the same
format most MCP servers document for Claude:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "your-token" }
    }
  }
}
```

Plug-ins load in the main Jarvis agent at the start of each session. Add
`"disabled": true` to a server to switch it off. Every plug-in adds to what
Jarvis has to consider on each reply, so keep only the ones you use.

## Optional: Jarvis on your phone

1. Install the [Flutter SDK](https://docs.flutter.dev/get-started/install/windows).
2. Start Jarvis with phone access:

   ```powershell
   .\jarvis.ps1 -Phone
   ```

   It prints two lines. The first time, it also creates a random
   `JARVIS_PHONE_KEY` in `jarvis_new\.env.local`.
3. Create `agent-starter-flutter\assets\.env` (see `assets\env.example`) with
   those two lines:

   ```
   LIVEKIT_TOKEN_ENDPOINT=http://192.168.1.20:8787/token
   JARVIS_PHONE_KEY=...
   ```

4. Plug in your phone and run:

   ```powershell
   cd agent-starter-flutter
   flutter pub get
   flutter run
   ```

The phone and PC must be on the same Wi-Fi. Allow Python through Windows
Firewall for **private** networks when asked. The key is what stops other
devices on your network from using Jarvis, so don't share it. The web UI only
listens on the PC itself.

## Optional: reverse-engineering tools

The coding agent uses whatever analysis tools it finds, so install the ones you
want. All of these are free:

```powershell
winget install EclipseAdoptium.Temurin.21.JDK   # Java, needed by Ghidra and jadx
winget install radare.radare2
dotnet tool install -g ilspycmd                   # .NET decompiler (needs the .NET SDK)
```

Download [Ghidra](https://github.com/NationalSecurityAgency/ghidra/releases)
and [jadx](https://github.com/skylot/jadx/releases), unzip them, and add
Ghidra's `support` folder (it contains `analyzeHeadless.bat`) and jadx's `bin`
folder to your PATH.

Reverse engineering needs shell access, so Jarvis asks before starting. Put
samples inside an approved workspace (e.g. a `sandbox` workspace), and only
analyse software you're allowed to.

### Plug-in tools for the coding agent (MCP)

Claude Code can use extra tools from MCP servers listed in
`jarvis_new\config\coder-mcp.json`, for example a Ghidra MCP server:

```json
{
  "mcpServers": {
    "ghidra": { "command": "python", "args": ["C:/tools/GhidraMCP/bridge_mcp_ghidra.py"] }
  }
}
```

Then add `"mcp__ghidra"` to `allowed_tools` under `[cloud.coder]` in
`config\modes.toml` so the coding agent may call them. Follow the MCP server's
own instructions for installing it.

## Optional: local mode

Local mode runs the voice and the coding agent on your own GPU. Everything
below fits in an RTX 3090's 24 GB.

### Ollama (the brain)

1. Install from <https://ollama.com>.
2. Give models enough context for coding work (set once, then restart Ollama):

   ```powershell
   setx OLLAMA_CONTEXT_LENGTH 32768
   ```

   If you run out of GPU memory, lower it to `16384`.
3. Download the model:

   ```powershell
   ollama pull qwen3-coder:30b
   ```

### Speech-to-text and text-to-speech

Easiest with [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(enable WSL 2 and GPU support):

```powershell
# Speech-to-text (Whisper) on port 8000
docker run -d --name jarvis-stt --gpus all -p 8000:8000 ghcr.io/speaches-ai/speaches:latest-cuda
# Text-to-speech (Kokoro) on port 8880
docker run -d --name jarvis-tts --gpus all -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-gpu:latest
```

Speaches downloads Whisper models on demand. If it complains about the model,
download it with its CLI or web UI and make sure the name matches `stt_model`
in `config\modes.toml`.

After the first run, start them again with `docker start jarvis-stt jarvis-tts`.

### OpenCode with Ollama

Create `%USERPROFILE%\.config\opencode\opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (local)",
      "options": { "baseURL": "http://localhost:11434/v1" },
      "models": { "qwen3-coder:30b": { "name": "Qwen3 Coder 30B" } }
    }
  }
}
```

### Fully offline audio (optional)

Local mode still sends audio through LiveKit Cloud unless you run a LiveKit
server yourself. Download `livekit-server` for Windows from
<https://github.com/livekit/livekit/releases>, put it on your PATH, and the
launcher starts it automatically in local mode.

## Troubleshooting

- **"not an approved workspace"** – add the folder to `config\workspaces.toml`.
- **"'claude' is not installed or not on PATH"** – reinstall Claude Code and reopen PowerShell.
- **Jarvis never joins the call** – check the agent window for errors, and that both `.env.local` files have the same LiveKit project.
- **Local mode is silent** – the launcher warns if the speech servers on ports 8000/8880 aren't running.
