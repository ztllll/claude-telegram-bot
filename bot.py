"""Telegram bridge to local `claude -p` CLI with streaming + tool status.

Each Telegram message is streamed to claude as a non-interactive prompt
with --output-format stream-json --include-partial-messages. Tool calls
appear as live status lines; assistant text appears progressively via
Telegram Bot API 9.5's sendMessageDraft. session_id is persisted per user
for multi-turn context. ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN are
inherited from the parent environment.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SESSIONS_PATH = ROOT / "sessions.json"
DOWNLOADS_DIR = ROOT / "downloads"
LOG_DIR = ROOT / "logs"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

BOT_TOKEN: str = CONFIG["bot_token"]
ALLOWED: set[int] = set(CONFIG["allowed_user_ids"])
WORKING_DIR: str = CONFIG["working_dir"]
CLAUDE_ARGS: list[str] = list(CONFIG.get("claude_args", []))
MAX_CHARS: int = int(CONFIG.get("max_response_chars", 4000))
TIMEOUT: int = int(CONFIG.get("subprocess_timeout_seconds", 600))
RETENTION_DAYS: int = int(CONFIG.get("downloads_retention_days", 7))
DRAFT_THROTTLE_MS: int = int(CONFIG.get("draft_throttle_ms", 300))

# Always include partial messages for token-level streaming
if "--include-partial-messages" not in CLAUDE_ARGS:
    CLAUDE_ARGS.append("--include-partial-messages")

LOG_DIR.mkdir(exist_ok=True)
DOWNLOADS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tgbot")
logging.getLogger("httpx").setLevel(logging.WARNING)

_sessions_lock = asyncio.Lock()


def load_sessions() -> dict[int, str]:
    if not SESSIONS_PATH.exists():
        return {}
    try:
        raw = json.loads(SESSIONS_PATH.read_text())
        return {int(k): v for k, v in raw.items()}
    except Exception as e:
        log.warning("Failed to load sessions.json: %s", e)
        return {}


def save_sessions_sync(sessions: dict[int, str]) -> None:
    tmp = SESSIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({str(k): v for k, v in sessions.items()}, indent=2))
    os.replace(tmp, SESSIONS_PATH)


SESSIONS: dict[int, str] = load_sessions()
USER_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if user is None or user.id not in ALLOWED:
        if user is not None:
            log.warning("REJECTED uid=%s username=%s", user.id, user.username)
        return False
    return True


# ---------- Tool-call status line formatting ----------

def _trim(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def format_tool_status(name: str, inp: dict) -> str:
    """Compact one-line representation of a tool invocation."""
    if not isinstance(inp, dict):
        inp = {}
    n = (name or "?").strip()
    if n == "Bash":
        return f"[bash] {_trim(inp.get('command', ''), 80)}"
    if n == "Read":
        return f"[read] {_trim(inp.get('file_path', ''), 100)}"
    if n == "Edit":
        return f"[edit] {_trim(inp.get('file_path', ''), 100)}"
    if n == "Write":
        return f"[write] {_trim(inp.get('file_path', ''), 100)}"
    if n == "Glob":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        return f"[glob] {_trim(pat, 60)}" + (f" in {_trim(path, 60)}" if path else "")
    if n == "Grep":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        return f"[grep] {_trim(pat, 60)}" + (f" in {_trim(path, 60)}" if path else "")
    if n == "WebFetch":
        return f"[fetch] {_trim(inp.get('url', ''), 100)}"
    if n == "WebSearch":
        return f"[search] {_trim(inp.get('query', ''), 100)}"
    if n in ("Task", "Agent"):
        return f"[agent] {_trim(inp.get('description') or inp.get('prompt', ''), 80)}"
    if n == "TodoWrite":
        todos = inp.get("todos", [])
        return f"[todos] {len(todos)} item(s)"
    if n == "NotebookEdit":
        return f"[notebook] {_trim(inp.get('notebook_path', ''), 80)}"
    # Generic fallback
    summary = ", ".join(f"{k}={_trim(v, 30)}" for k, v in list(inp.items())[:2])
    return f"[{n.lower()}] {_trim(summary, 100)}"


# ---------- Image marker (existing protocol, preserved) ----------

IMG_MARKER_RE = re.compile(r"\[\[img:([^\]\n]+)\]\]")


def extract_images(text: str) -> tuple[list[str], str]:
    paths = [m.group(1).strip() for m in IMG_MARKER_RE.finditer(text)]
    cleaned = IMG_MARKER_RE.sub("", text).strip()
    return paths, cleaned


async def send_images(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, paths: list[str]) -> None:
    for p in paths:
        f = Path(p)
        if not f.is_file():
            await ctx.bot.send_message(chat_id=chat_id, text=f"[图片不存在] {p}")
            continue
        try:
            with open(f, "rb") as fh:
                await ctx.bot.send_photo(chat_id=chat_id, photo=fh)
            log.info("sent photo chat=%d path=%s bytes=%d", chat_id, p, f.stat().st_size)
        except Exception as e:
            log.exception("send_photo failed: %s", e)
            await ctx.bot.send_message(chat_id=chat_id, text=f"[图片发送失败] {p}: {e}")


# ---------- Reply chunking ----------

def chunk(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out


async def send_long(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for part in chunk(text, MAX_CHARS):
        await ctx.bot.send_message(chat_id=chat_id, text=part)


# ---------- Streaming renderer ----------

class StreamRenderer:
    """Accumulates Claude stream events and renders to a Telegram draft.

    Falls back to sendMessage + editMessageText if sendMessageDraft is rejected.
    """

    def __init__(self, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
        self.ctx = ctx
        self.bot = ctx.bot
        self.chat_id = chat_id
        self.user_id = user_id
        # 非零 unique，足够区分同 chat 不同流；时间戳 ns 后取 31 位避免太长
        self.draft_id = int(time.time_ns() & 0x7FFFFFFF) | 1
        self.tool_log: list[str] = []
        self.text_buf = ""
        self.last_payload = ""
        self.last_update = 0.0
        self.mode = "draft"  # "draft" | "edit"
        self.placeholder_id: int | None = None
        self.sealed_count = 0  # 已落地多少条 sendMessage
        self._draft_disabled = False

    def _render(self) -> str:
        parts: list[str] = []
        if self.tool_log:
            parts.append("\n".join(self.tool_log))
        if self.text_buf:
            if parts:
                parts.append("")  # 空行分隔
            parts.append(self.text_buf)
        return "\n".join(parts).rstrip()

    async def start_placeholder(self) -> None:
        """First flush — show a placeholder so user sees activity immediately."""
        self.tool_log.append("[初始化…]")
        await self._flush(force=True)

    async def on_tool_use(self, name: str, inp: dict) -> None:
        # 替换初始占位，第一条工具来时弹掉 [初始化…]
        if self.tool_log == ["[初始化…]"]:
            self.tool_log.clear()
        self.tool_log.append(format_tool_status(name, inp))
        await self._flush(force=True)

    async def on_text_delta(self, text: str) -> None:
        if not text:
            return
        # 第一次出现文字时，弹掉占位（如果工具调用没出现）
        if self.tool_log == ["[初始化…]"]:
            self.tool_log.clear()
        self.text_buf += text
        await self._flush(force=False)

    async def on_complete(self, final_text: str | None) -> None:
        """Finalize: replace draft with permanent sendMessage(s)."""
        # 如果 result.text 与流式累积差不多，用 result 版（更干净）
        if final_text is not None:
            self.text_buf = final_text
        # 拆图与文字
        img_paths, clean_text = extract_images(self.text_buf)
        # 落地最终消息（替代 draft，draft 会自动消失）
        if clean_text.strip():
            for part in chunk(clean_text, MAX_CHARS):
                await self.bot.send_message(chat_id=self.chat_id, text=part)
        elif self.sealed_count == 0 and not img_paths:
            await self.bot.send_message(chat_id=self.chat_id, text="（claude 返回空回复）")
        # 发图
        await send_images(self.ctx, self.chat_id, img_paths)

    async def fail(self, message: str) -> None:
        """Render an error and stop."""
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=f"[错误] {message}")
        except Exception:
            log.exception("fail() send_message error")

    async def _flush(self, force: bool) -> None:
        now = time.monotonic()
        if not force and (now - self.last_update) * 1000 < DRAFT_THROTTLE_MS:
            return

        payload = self._render()
        if not payload:
            return
        if payload == self.last_payload:
            return

        # 4000 字封顶：当前内容用 sendMessage 落地，重置缓冲
        if len(payload) > MAX_CHARS:
            await self._seal_and_reset(payload)
            return

        await self._send(payload)
        self.last_payload = payload
        self.last_update = now

    async def _send(self, payload: str) -> None:
        """Try draft first; on rejection, switch to edit-message mode."""
        if self.mode == "draft" and not self._draft_disabled:
            try:
                await self.bot.send_message_draft(
                    chat_id=self.chat_id,
                    draft_id=self.draft_id,
                    text=payload,
                )
                return
            except BadRequest as e:
                msg = str(e).lower()
                if "draft" in msg or "not supported" in msg or "private" in msg:
                    log.warning("draft unsupported, falling back to edit: %s", e)
                    self._draft_disabled = True
                    self.mode = "edit"
                    # 没成功的 draft 不会留可见消息；用 sendMessage 起占位
                    try:
                        sent = await self.bot.send_message(
                            chat_id=self.chat_id, text=payload
                        )
                        self.placeholder_id = sent.message_id
                    except Exception:
                        log.exception("fallback send_message failed")
                    return
                # 其他 BadRequest 抛
                raise
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.1)
                # 不重试本次，等下个 delta
                return
            except TelegramError as e:
                log.warning("draft TelegramError: %s; will retry next flush", e)
                return

        # edit-message 模式
        if self.placeholder_id is None:
            try:
                sent = await self.bot.send_message(chat_id=self.chat_id, text=payload)
                self.placeholder_id = sent.message_id
            except Exception:
                log.exception("edit-mode placeholder send failed")
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.placeholder_id,
                text=payload,
            )
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.warning("edit_message_text BadRequest: %s", e)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.1)

    async def _seal_and_reset(self, payload: str) -> None:
        """Payload exceeded MAX_CHARS — finalize a chunk and start a new draft."""
        # 切到 MAX_CHARS 边界（按行）
        head, tail = payload[:MAX_CHARS], payload[MAX_CHARS:]
        # 找最近换行避免劈断单行
        cut = head.rfind("\n")
        if cut > MAX_CHARS // 2:
            tail = head[cut:].lstrip("\n") + tail
            head = head[:cut]
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=head)
        except Exception:
            log.exception("seal sendMessage failed")
        self.sealed_count += 1
        # 开新 draft：清空状态行（不重复显示），text_buf = tail
        self.tool_log = []
        self.text_buf = tail
        self.last_payload = ""
        self.draft_id = (self.draft_id + 1) & 0x7FFFFFFF | 1
        self.placeholder_id = None  # edit 模式下也起新条
        # 立刻刷新（如有内容）
        if self.text_buf:
            self.last_update = 0.0
            await self._flush(force=True)


# ---------- Streaming caller ----------

async def stream_claude(prompt: str, user_id: int, renderer: StreamRenderer) -> str | None:
    """Drive claude -p stream-json events into the renderer.

    Returns None on success, or an error string on failure.
    Persists session_id on result event.
    """
    args = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
    sid = SESSIONS.get(user_id)
    if sid:
        args += ["--resume", sid]
    args += CLAUDE_ARGS

    log.info(
        "claude stream uid=%d resume=%s prompt_chars=%d",
        user_id, bool(sid), len(prompt),
    )
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=WORKING_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        return "找不到 claude 命令（检查 PATH）"
    except Exception as e:
        return f"启动 claude 失败：{e}"

    final_result: str | None = None
    new_sid: str | None = None
    is_error = False

    async def consume() -> None:
        nonlocal final_result, new_sid, is_error
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-JSON line: %r", line[:120])
                continue
            t = e.get("type")
            if t == "stream_event":
                ev = e.get("event", {}) or {}
                if ev.get("type") == "content_block_delta":
                    d = ev.get("delta", {}) or {}
                    if d.get("type") == "text_delta":
                        await renderer.on_text_delta(d.get("text", ""))
            elif t == "assistant":
                msg = e.get("message", {}) or {}
                for c in msg.get("content", []) or []:
                    if c.get("type") == "tool_use":
                        await renderer.on_tool_use(
                            c.get("name", "?"), c.get("input", {}) or {}
                        )
            elif t == "result":
                final_result = e.get("result")
                new_sid = e.get("session_id")
                if e.get("is_error"):
                    is_error = True
            # system / user 事件目前忽略（user 事件含 tool_result，可日志但不展示）

    try:
        await asyncio.wait_for(consume(), timeout=TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return f"claude 响应超时（>{TIMEOUT}s）"
    except Exception as e:
        log.exception("stream consume error")
        return f"流式处理异常：{e}"

    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        try:
            err_bytes = await proc.stderr.read() if proc.stderr else b""
        except Exception:
            err_bytes = b""
        err = err_bytes.decode("utf-8", errors="replace")[:500].strip()
        log.error("claude exit=%d stderr=%s", proc.returncode, err)
        return f"claude 退出码 {proc.returncode}\n{err}"

    if is_error:
        return f"claude 报错：{final_result or '(no detail)'}"

    if new_sid:
        async with _sessions_lock:
            SESSIONS[user_id] = new_sid
            save_sessions_sync(SESSIONS)

    log.info(
        "claude stream done uid=%d chars=%d elapsed=%.1fs",
        user_id, len(final_result or ""), elapsed,
    )

    # 让 renderer 用 result.text 做最后一次干净落地
    await renderer.on_complete(final_result)
    return None


# ---------- Telegram handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Claude Code 桥接已就绪（流式模式）。\n\n"
        "直接发文字、图片或文件即可对话。\n"
        "状态行会实时显示 Claude 调用了哪些工具。\n\n"
        "/new 开新会话\n"
        "/status 查看会话状态"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    uid = update.effective_user.id
    async with _sessions_lock:
        if uid in SESSIONS:
            del SESSIONS[uid]
            save_sessions_sync(SESSIONS)
    await update.message.reply_text("已开新会话。")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    uid = update.effective_user.id
    sid = SESSIONS.get(uid)
    msg = f"session: {sid[:8] + '…' if sid else '（无，下条消息会开新会话）'}"
    await update.message.reply_text(msg)


async def download_attachments(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> list[str]:
    msg = update.message
    uid = update.effective_user.id
    user_dir = DOWNLOADS_DIR / str(uid)
    user_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []

    targets: list[tuple[str, object]] = []
    if msg.photo:
        targets.append(("photo", msg.photo[-1]))
    if msg.document:
        targets.append(("document", msg.document))
    if msg.audio:
        targets.append(("audio", msg.audio))
    if msg.voice:
        targets.append(("voice", msg.voice))
    if msg.video:
        targets.append(("video", msg.video))

    for kind, obj in targets:
        try:
            file = await ctx.bot.get_file(obj.file_id)
            file_name = getattr(obj, "file_name", None)
            if file_name and "." in file_name:
                ext = "." + file_name.rsplit(".", 1)[-1]
            elif file.file_path and "." in file.file_path:
                ext = "." + file.file_path.rsplit(".", 1)[-1]
            elif kind == "photo":
                ext = ".jpg"
            else:
                ext = ""
            target = user_dir / f"{obj.file_id}{ext}"
            await file.download_to_drive(custom_path=str(target))
            paths.append(str(target.resolve()))
            log.info("downloaded %s uid=%d -> %s", kind, uid, target)
        except Exception as e:
            log.exception("download failed: %s", e)

    return paths


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    msg = update.message
    if msg is None:
        return
    uid = update.effective_user.id

    file_paths = await download_attachments(update, ctx)
    text = (msg.text or msg.caption or "").strip()

    parts: list[str] = []
    if text:
        parts.append(text)
    for p in file_paths:
        parts.append(f"@{p}")
    prompt = "\n\n".join(parts).strip()
    if not prompt:
        return

    async with USER_LOCKS[uid]:
        renderer = StreamRenderer(ctx, msg.chat_id, uid)
        try:
            await renderer.start_placeholder()
        except Exception:
            log.exception("start_placeholder failed (continuing without)")

        err = await stream_claude(prompt, uid, renderer)
        if err:
            await renderer.fail(err)


async def _cleanup_loop() -> None:
    while True:
        try:
            cutoff = time.time() - RETENTION_DAYS * 86400
            for f in DOWNLOADS_DIR.rglob("*"):
                if f.is_file() and f.stat().st_mtime < cutoff:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except Exception as e:
            log.warning("cleanup error: %s", e)
        await asyncio.sleep(86400)


async def _post_init(app: Application) -> None:
    asyncio.create_task(_cleanup_loop())
    log.info("Bot ready; allowed users: %s; claude_args=%s", sorted(ALLOWED), CLAUDE_ARGS)


def main() -> None:
    if BOT_TOKEN.startswith("REPLACE") or not BOT_TOKEN:
        log.error("config.json bot_token 未填写")
        sys.exit(1)
    if not ALLOWED:
        log.error("config.json allowed_user_ids 不能为空")
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
