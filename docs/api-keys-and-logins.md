# API keys and logins

papr-thinkr can use five kinds of model access. **You only need one.** Local
models need nothing at all. The command-line tools reuse a login you already
have. The `*-api` backends need an API key.

| Backend | What it needs | How to check |
|---|---|---|
| `ollama` (local) | Ollama running and a model pulled | `python papr.py backends` |
| `claude-code` | Claude Code installed and signed in once (`claude`) | same |
| `codex` | Codex CLI (`npm i -g @openai/codex`) signed in once. Signing in to the **ChatGPT/Codex VS Code extension counts**, because both use `~/.codex/auth.json`. | same |
| `gemini` | Gemini CLI (`npm i -g @google/gemini-cli`) **plus a Gemini API key** (see below) | same |
| `anthropic-api` | `ANTHROPIC_API_KEY` | same |
| `openai-api` | `OPENAI_API_KEY`, or `OPENAI_BASE_URL` pointing at a local server (LM Studio etc.) | same |
| `gemini-api` | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | same |

> **Gemini and Google sign-in.** Google has stopped letting the free
> *Gemini Code Assist for individuals* sign-in drive the Gemini CLI. The CLI
> now replies "This client is no longer supported … migrate to Antigravity".
> A cached Google sign-in is therefore not enough. Use a free API key from
> Google AI Studio instead: it works for both the `gemini` CLI and the
> `gemini-api` backend.

## Where to get a key

| Key | Where |
|---|---|
| `GEMINI_API_KEY` | <https://aistudio.google.com/apikey>. Sign in with a Google account and choose **Create API key**. There is a free tier. |
| `ANTHROPIC_API_KEY` | <https://console.anthropic.com/settings/keys>. Billed per token, separately from a Claude.ai subscription. |
| `OPENAI_API_KEY` | <https://platform.openai.com/api-keys>. Billed per token, separately from a ChatGPT subscription. |

A Claude Pro/Max or ChatGPT Plus/Pro **subscription does not include API
access**. The way to use those subscriptions is the `claude-code` and `codex`
backends.

## Three ways to add a key

### 1. In the app (easiest)

`python papr.py serve` → click the backend pill (top right) → **API keys** →
paste the key → **Save**. The backends list updates immediately. The key is
never shown again in full; only something like `AIza…3xQk` is displayed.

### 2. From the command line

```bash
python papr.py keys                          # what's set, masked
python papr.py keys set GEMINI_API_KEY       # prompts without echoing what you type
python papr.py keys remove GEMINI_API_KEY
```

`--value KEY` also works, but it leaves the key in your shell history.

### 3. As an environment variable

papr-thinkr also reads ordinary environment variables, and a variable you set
yourself takes precedence over a stored key.

```powershell
# PowerShell, permanently for your user
[Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "AIza...", "User")
```

```bash
# bash/zsh
export GEMINI_API_KEY=AIza...
```

## Where stored keys live

Keys are stored in a JSON file in your user profile, **outside the
repository**, so they can't be committed by accident:

| OS | File |
|---|---|
| Windows | `%APPDATA%\papr-thinkr\keys.json` |
| macOS | `~/Library/Application Support/papr-thinkr/keys.json` |
| Linux | `~/.config/papr-thinkr/keys.json` (file mode 600) |

Set `PAPR_CONFIG_DIR` to use another folder. Anyone who can read your user
profile can read this file, the same as the logins that Claude Code, Codex
and the Gemini CLI keep in your home folder.

## What gets sent where

- **Local backends** (Ollama, or a local OpenAI-compatible server) send
  nothing off your machine.
- **Cloud backends** send the manuscript text, the reviewer comments,
  summaries of your data files (column statistics, not raw rows), the
  plotting code, and, for figure checks, the rendered figure images.
- The CLI agents are run in an empty scratch folder with their tools switched
  off. The only exception is a read-only "Read" tool when there is a figure
  to look at, and even then only the figure files are placed in that folder.

## Claude connectors (Gmail, Calendar, Scholar Gateway)

Those connectors belong to your claude.ai account, not to papr-thinkr.
Authorise them at **claude.ai → Settings → Connectors → (connector) →
Connect**. They are not needed for anything papr-thinkr does.
