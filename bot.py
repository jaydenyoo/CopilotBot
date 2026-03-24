#!/usr/bin/env python3
"""
Copilot CLI Telegram Bridge.

Portable bot that bridges Telegram to GitHub Copilot CLI.
Reuses a single persistent session. Fully configurable via env vars.

Usage:
    export TELEGRAM_BOT_TOKEN="your-token"
    export TELEGRAM_USER_ID="your-user-id"
    python bot.py
"""

import os

# Configurable extra PATH entries for tools like gh, az, copilot
_extra = os.environ.get("EXTRA_PATH", "")
os.environ["PATH"] = (
    f"{_extra}:" if _extra else ""
) + "/home/vscode/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/sbin:/bin:" + os.environ.get("PATH", "")

import re
import sys
import asyncio
import subprocess
import logging

try:
    import yaml
except ImportError:
    yaml = None

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "0"))
REPO_DIR = os.environ.get("REPO_DIR", os.getcwd())

_lock = asyncio.Lock()
_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".session_id")


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

def _load_session_id():
    try:
        with open(_SESSION_FILE) as f:
            sid = f.read().strip()
            return sid if sid else None
    except FileNotFoundError:
        return None


def _save_session_id(sid):
    if sid:
        with open(_SESSION_FILE, "w") as f:
            f.write(sid)
    else:
        try:
            os.remove(_SESSION_FILE)
        except FileNotFoundError:
            pass


_bot_session_id = _load_session_id()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_ansi(t):
    return re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").sub("", t)


def strip_usage_stats(t):
    return re.sub(r"\n?Total usage est:.*", "", t, flags=re.DOTALL).strip()


def is_authorized(uid):
    return ALLOWED_USER_ID == 0 or uid == ALLOWED_USER_ID


def list_sessions(limit=10):
    d = os.path.expanduser("~/.copilot/session-state")
    ss = []
    if not os.path.isdir(d) or not yaml:
        return ss
    for sid in sorted(
        os.listdir(d),
        key=lambda s: os.path.getmtime(os.path.join(d, s)),
        reverse=True,
    )[:limit]:
        ws = os.path.join(d, sid, "workspace.yaml")
        if not os.path.exists(ws):
            continue
        try:
            with open(ws) as f:
                data = yaml.safe_load(f) or {}
            ss.append({
                "id": sid,
                "summary": data.get("summary", "") or "",
                "branch": data.get("branch", "") or "",
                "updated": str(data.get("updated_at", "")),
            })
        except Exception:
            ss.append({"id": sid, "summary": "", "branch": "", "updated": ""})
    return ss


def rename_session(sid, name):
    if not yaml:
        return False
    ws = os.path.expanduser(f"~/.copilot/session-state/{sid}/workspace.yaml")
    if not os.path.exists(ws):
        return False
    try:
        with open(ws) as f:
            data = yaml.safe_load(f) or {}
        data["summary"] = name
        with open(ws, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

async def run_copilot(prompt, extra_flags=""):
    """Run copilot -p as subprocess. No timeout -- waits for process exit."""
    escaped = prompt.replace("'", "'\\''")
    cmd = f"copilot --allow-all {extra_flags} -p '{escaped}'".strip()
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=REPO_DIR,
    )
    stdout, _ = await proc.communicate()
    output = strip_ansi(stdout.decode(errors="replace")).strip()
    return strip_usage_stats(output) if output else "(empty response)"


async def run_copilot_session(prompt):
    """Run copilot reusing the bot's persistent session."""
    global _bot_session_id
    if _bot_session_id:
        r = await run_copilot(prompt, extra_flags=f"--resume={_bot_session_id}")
    else:
        r = await run_copilot(prompt)
    ss = list_sessions(1)
    if ss:
        _bot_session_id = ss[0]["id"]
        _save_session_id(_bot_session_id)
        logger.info(f"Bot session: {_bot_session_id}")
    return r


async def run_bash(command):
    """Run a bash command and return output."""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=REPO_DIR,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return "Timed out (60s)."
    return strip_ansi(stdout.decode(errors="replace")).strip() or "(no output)"


def split_message(text, max_len=4000):
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def _send_chunks(update, text):
    chunks = split_message(text)
    for i, c in enumerate(chunks):
        prefix = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
        await update.message.reply_text(f"{prefix}{c}")


# ---------------------------------------------------------------------------
# Core handlers
# ---------------------------------------------------------------------------

async def handle_message(update, context):
    """Regular text -> Copilot CLI, reusing persistent session."""
    if not update.message or not update.message.text:
        return
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    text = update.message.text
    resume_id = context.user_data.pop("resume_session", None)
    if resume_id:
        global _bot_session_id
        _bot_session_id = resume_id
        _save_session_id(_bot_session_id)
        logger.info(f"Switched to session {resume_id[:12]}, prompt: {text[:80]}...")
        await update.message.reply_text("Resuming session...")
        async with _lock:
            response = await run_copilot(text, extra_flags=f"--resume={resume_id}")
    else:
        logger.info(f"Copilot prompt: {text[:80]}...")
        await update.message.reply_text("Asking Copilot...")
        async with _lock:
            response = await run_copilot_session(text)
    await _send_chunks(update, response)


async def cmd_start(update, context):
    await update.message.reply_text(
        f"Copilot CLI Bridge\n"
        f"User ID: {update.effective_user.id}\n"
        f"Repo: {REPO_DIR}\n\n"
        f"/help for commands"
    )


async def cmd_help(update, context):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "Commands\n\n"
        "Copilot:\n"
        "  (any message) -> same session\n"
        "  /new - fresh session\n"
        "  /name <label> - name session\n"
        "  /continue <msg> - resume last\n"
        "  /resume - pick session\n"
        "  /sessions - list sessions\n\n"
        "General:\n"
        "  /cmd <bash>\n"
        "  /status\n\n"
        "Dev:\n"
        "  /git\n  /diff\n  /test [path]\n  /tree"
    )


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

async def cmd_new(update, context):
    if not is_authorized(update.effective_user.id):
        return
    global _bot_session_id
    _bot_session_id = None
    _save_session_id(None)
    await update.message.reply_text("Session reset. Next message starts fresh.")


async def cmd_name(update, context):
    if not is_authorized(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /name <label>")
        return
    if len(parts) == 3 and len(parts[1]) > 20 and "-" in parts[1]:
        target_id, label = parts[1], parts[2]
    else:
        target_id = _bot_session_id
        label = update.message.text.split(maxsplit=1)[1]
    if not target_id:
        await update.message.reply_text("No active session.")
        return
    if rename_session(target_id, label):
        await update.message.reply_text(f"Session renamed to: {label}")
    else:
        await update.message.reply_text(f"Session {target_id[:12]}... not found.")


async def cmd_continue(update, context):
    if not is_authorized(update.effective_user.id):
        return
    prompt = update.message.text.replace("/continue", "", 1).strip()
    if not prompt:
        await update.message.reply_text("Usage: /continue <message>")
        return
    await update.message.reply_text("Resuming last session...")
    async with _lock:
        response = await run_copilot(prompt, extra_flags="--continue")
    await _send_chunks(update, response)


async def cmd_resume(update, context):
    global _bot_session_id
    if not is_authorized(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) == 1:
        sessions = list_sessions(10)
        if not sessions:
            await update.message.reply_text("No sessions found.")
            return
        buttons = []
        for s in sessions:
            label = s["summary"][:40] or "(no summary)"
            ts = s["updated"][11:16] if len(s["updated"]) > 16 else ""
            active = " <" if s["id"] == _bot_session_id else ""
            buttons.append([InlineKeyboardButton(
                f"{ts}  {label}{active}", callback_data=f"pick_{s['id']}"
            )])
        await update.message.reply_text(
            "Pick a session:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    if len(parts) == 2:
        context.user_data["resume_session"] = parts[1]
        await update.message.reply_text(
            f"Selected: {parts[1][:12]}...\nSend your message."
        )
        return
    _bot_session_id = parts[1]
    _save_session_id(_bot_session_id)
    await update.message.reply_text(f"Resuming {parts[1][:12]}...")
    async with _lock:
        response = await run_copilot(parts[2], extra_flags=f"--resume={parts[1]}")
    await _send_chunks(update, response)


async def callback_session_pick(update, context):
    q = update.callback_query
    await q.answer()
    if not q.data.startswith("pick_"):
        return
    sid = q.data[5:]
    context.user_data["resume_session"] = sid
    ss = list_sessions(10)
    summary = next((s["summary"] for s in ss if s["id"] == sid), "")
    await q.edit_message_text(
        f"Selected: {summary[:50] or sid[:12]}\n\nSend your message."
    )


async def cmd_sessions(update, context):
    if not is_authorized(update.effective_user.id):
        return
    ss = list_sessions(10)
    if not ss:
        await update.message.reply_text("No sessions found.")
        return
    lines = []
    for i, s in enumerate(ss):
        ts = s["updated"][5:16].replace("T", " ") if len(s["updated"]) > 16 else "?"
        summary = s["summary"][:50] or "(no summary)"
        active = " <-" if s["id"] == _bot_session_id else ""
        lines.append(f"{i+1}. [{ts}] {summary}{active}\n   {s['id']}")
    await update.message.reply_text("Recent sessions:\n\n" + "\n\n".join(lines))


# ---------------------------------------------------------------------------
# General commands
# ---------------------------------------------------------------------------

async def cmd_bash(update, context):
    if not is_authorized(update.effective_user.id):
        return
    command = update.message.text.replace("/cmd", "", 1).strip()
    if not command:
        await update.message.reply_text("Usage: /cmd <command>")
        return
    logger.info(f"Bash: {command[:80]}")
    await update.message.reply_text(f"Running: {command[:60]}...")
    async with _lock:
        response = await run_bash(command)
    await _send_chunks(update, response)


async def cmd_status(update, context):
    if not is_authorized(update.effective_user.id):
        return
    cok = subprocess.run(
        ["which", "copilot"], capture_output=True, timeout=5
    ).returncode == 0
    sl = _bot_session_id[:12] + "..." if _bot_session_id else "(none)"
    await update.message.reply_text(
        f"Copilot: {'OK' if cok else 'missing'}\n"
        f"Session: {sl}\n"
        f"Repo: {REPO_DIR}"
    )


# ---------------------------------------------------------------------------
# Dev commands
# ---------------------------------------------------------------------------

async def cmd_git(update, context):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(await run_bash(
        f"cd {REPO_DIR} && echo 'Branch:' && git --no-pager branch --show-current && "
        f"echo '' && echo 'Last 5:' && git --no-pager log --oneline -5 && "
        f"echo '' && echo 'Uncommitted:' && git --no-pager status --short | head -15"
    ) or "(clean)")


async def cmd_diff(update, context):
    if not is_authorized(update.effective_user.id):
        return
    r = await run_bash(
        f"cd {REPO_DIR} && git --no-pager diff --stat && "
        f"echo '---' && git --no-pager diff --staged --stat"
    )
    await update.message.reply_text(
        f"Diff:\n\n{r}" if r.strip() else "No changes."
    )


async def cmd_test(update, context):
    if not is_authorized(update.effective_user.id):
        return
    parts = update.message.text.split(maxsplit=1)
    target = parts[1].strip() if len(parts) > 1 else "."
    await update.message.reply_text(f"Running tests: {target}...")
    await _send_chunks(update, await run_bash(
        f"cd {REPO_DIR} && python -m pytest {target} -q --tb=short 2>&1 | tail -30"
    ))


async def cmd_tree(update, context):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text("Files:\n\n" + await run_bash(
        f"find {REPO_DIR} -type f -name '*.py' "
        f"-not -path '*/venv/*' -not -path '*/__pycache__/*' "
        f"| sed 's|{REPO_DIR}/||' | sort | head -50"
    ))


async def cmd_logs(update, context):
    if not is_authorized(update.effective_user.id):
        return
    parts = update.message.text.split()
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
    await _send_chunks(update, await run_bash(
        f"tail -n {n} {REPO_DIR}/outputs/*.log 2>/dev/null || "
        f"tail -n {n} /tmp/*.log 2>/dev/null || "
        f"echo 'No logs found'"
    ))


# ---------------------------------------------------------------------------
# Bot menu & main
# ---------------------------------------------------------------------------

BOT_COMMANDS = [
    BotCommand("help", "Commands"),
    BotCommand("new", "Fresh session"),
    BotCommand("name", "Name session"),
    BotCommand("continue", "Resume last"),
    BotCommand("resume", "Pick session"),
    BotCommand("sessions", "List sessions"),
    BotCommand("status", "Health check"),
    BotCommand("git", "Git info"),
    BotCommand("diff", "Diff"),
    BotCommand("test", "Pytest"),
    BotCommand("logs", "Tail logs"),
    BotCommand("cmd", "Bash command"),
    BotCommand("tree", "File listing"),
]


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set.")
        print()
        print("Setup:")
        print("  1. Message @BotFather on Telegram -> /newbot")
        print("  2. Copy the bot token")
        print("  3. cp .env.example .env && edit .env")
        print("  4. pip install -r requirements.txt")
        print("  5. python bot.py")
        return

    print(f"Copilot Telegram Bridge starting...")
    print(f"   Repo: {REPO_DIR}")
    print(f"   User: {ALLOWED_USER_ID or 'ANY (insecure!)'}")
    print(f"   Session: {_bot_session_id or '(new)'}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    for name, fn in [
        ("start", cmd_start), ("help", cmd_help), ("new", cmd_new),
        ("name", cmd_name), ("cmd", cmd_bash), ("continue", cmd_continue),
        ("resume", cmd_resume), ("sessions", cmd_sessions),
        ("status", cmd_status), ("git", cmd_git), ("diff", cmd_diff),
        ("logs", cmd_logs), ("test", cmd_test), ("tree", cmd_tree),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callback_session_pick, pattern="^pick_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def post_init(a):
        await a.bot.set_my_commands(BOT_COMMANDS)

    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
