# Copilot CLI Telegram Bridge

A portable Telegram bot that bridges your phone to GitHub Copilot CLI.
Send messages from Telegram, get Copilot responses back. Reuses a single
persistent session to maintain context across messages.

## Features

- **Copilot CLI integration** — messages routed to `copilot -p` with full tool permissions
- **Session persistence** — reuses one session (no spam), survives bot restarts
- **Session management** — `/new`, `/resume` (with picker), `/continue`, `/name`, `/sessions`
- **Bash commands** — `/cmd <command>` runs shell commands
- **Dev tools** — `/git`, `/diff`, `/test`, `/tree`, `/logs`
- **Security** — locked to your Telegram user ID

## Quick Start

### 1. Create a Telegram Bot
- Message `@BotFather` on Telegram -> `/newbot`
- Save the bot token

### 2. Get Your User ID
- Message `@userinfobot` -> save your numeric ID

### 3. Configure
```bash
cp .env.example .env
# Edit .env with your token and user ID
```

### 4. Install & Run
```bash
pip install -r requirements.txt
python bot.py
```

## Configuration

All config is via environment variables (or `.env` file):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TELEGRAM_USER_ID` | Yes | — | Your numeric Telegram user ID |
| `REPO_DIR` | No | Current directory | Working directory for copilot/bash |
| `EXTRA_PATH` | No | — | Additional PATH entries (colon-separated) |

## Adapting to Another Repo

1. Copy this directory into your repo
2. Edit `.env` with a new bot token (create another bot via @BotFather)
3. Set `REPO_DIR` to your repo path
4. Set `EXTRA_PATH` if you have tools in non-standard locations
5. Add your own custom commands following the pattern

## Commands

| Command | Description |
|---------|-------------|
| _(any message)_ | Send to Copilot CLI (same session) |
| `/new` | Start a fresh Copilot session |
| `/name <label>` | Name the active session |
| `/continue <msg>` | Resume the most recent session |
| `/resume` | Pick a session to resume (interactive) |
| `/sessions` | List recent sessions |
| `/cmd <bash>` | Run a shell command |
| `/status` | Health check |
| `/git` | Branch, commits, dirty files |
| `/diff` | Git diff summary |
| `/test [path]` | Run pytest |
| `/tree` | Project file listing |
| `/logs [n]` | Tail logs |

## Adding Custom Commands

```python
async def cmd_mycommand(update, context):
    if not is_authorized(update.effective_user.id):
        return
    # Your logic here
    await update.message.reply_text("Done!")

# Register in main():
#   ("mycommand", cmd_mycommand),
# Add to BOT_COMMANDS:
#   BotCommand("mycommand", "Description"),
```

## Architecture

```
Phone -> Telegram Bot API -> bot.py -> copilot -p (subprocess) -> response
                                    -> bash command (subprocess) -> response
```

- No tmux, no polling, no timeout guessing
- Waits for `copilot -p` process to exit (however long it takes)
- Session ID stored in `.session_id` file
- Usage stats stripped from output automatically
