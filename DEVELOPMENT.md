# claude-telegram-bot — 开发文档

> 目标读者：维护者、贡献者、想从零复刻这个 bot 的开发者 / AI 模型
> README 给陌生人看的快速上手见 [README.md](README.md)，本文是详细架构 + 实现 + 运维的全集

---

## 0. TL;DR

Python bot，本机 long-polling Telegram 接收消息 → 调 `claude -p --output-format stream-json --verbose --include-partial-messages` 子进程 → 把工具调用变成实时状态行、把 token 流变成打字机效果，通过 Telegram Bot API 9.5 的 `sendMessageDraft` 渲染 → 完成时用 `sendMessage` 落地。每用户独立 session_id 持久化。launchd 开机自启。

设计前提：本机已有 `claude` CLI 且配置好 API（官方或第三方端点）。bot **不**直接调 Anthropic API，复用 CLI 完整工具能力。

---

## 1. 架构

### 数据流

```
Telegram 客户端
        ↓ HTTPS
api.telegram.org
        ↓ long polling
bot.py 进程（launchd 守护）
        ├── 白名单守卫
        ├── 每用户 asyncio.Lock 串行化
        ├── 附件下载（图片/文档落 downloads/）
        └── StreamRenderer ───────────────┐
                                          ↓
                  asyncio.create_subprocess_exec
                                          ↓
            claude -p --output-format stream-json
                  --verbose --include-partial-messages
                  --resume <session_id>
                  --permission-mode bypassPermissions
                                          ↓ HTTPS
                  Anthropic API / 第三方端点
                                          ↓
                            Claude (model 由 ~/.claude/settings.json 决定)
                                          ↑ JSONL 事件流
                  StreamRenderer 解析事件
                                          │
        ┌─────────────────────────────────┤
        ↓                                 ↓
工具调用 → 状态行              text_delta → 累计文字
   `[bash] echo hi`                  渐进展示
   `[read] /path/to/foo`
        │                                 │
        └────────► sendMessageDraft ◄─────┘
                  （同一 draft_id 累积渲染）
                              ↓
              result 事件 → sendMessage 落地永久消息
                              ↓
                       图片协议 [[img:/path]]
                              → sendPhoto
```

### 组件清单

| 组件 | 文件位置 | 职责 |
|---|---|---|
| 配置加载 | `bot.py:35-58` | 读 `config.json`，自动追加 `--include-partial-messages` |
| 会话持久化 | `bot.py:60-77` | `sessions.json`，原子写（tmp + replace） |
| 白名单 | `bot.py:81-87` | `is_allowed(update)` |
| 工具行格式化 | `bot.py:90-130` | `format_tool_status(name, input)` 把 tool_use 变成 `[bash] cmd` 等 |
| 图片回传协议 | `bot.py:135-160` | `[[img:/path]]` marker → `sendPhoto` |
| 流式渲染器 | `bot.py:185-300` | `StreamRenderer` 类：状态行 + 文字 buffer + draft 节流 + 4000 封顶 |
| 流式调用 | `bot.py:305-380` | `stream_claude(prompt, uid, renderer)` 解析 JSONL |
| 命令处理 | `bot.py:385-415` | `/start` `/help` `/new` `/status` |
| 附件下载 | `bot.py:418-456` | photo/document/audio/voice/video → 本地文件 |
| 主消息处理 | `bot.py:459-485` | `handle_message` 装配 prompt + 启动 stream |
| 清理任务 | `bot.py:488-498` | 每 24h 删 N 天前的下载 |

### 关键设计决策

| 决策 | 为什么 |
|---|---|
| 调 `claude -p` 子进程而不是直接调 API | 复用 Claude Code 的所有工具能力（Read/Bash/Edit/Grep/...）；自动继承 `~/.claude/settings.json` 和 `ANTHROPIC_*` 环境变量 |
| Long polling 而不是 webhook | 不需要公网可达 + HTTPS 证书；不需要内网穿透 |
| launchd 而不是 nohup/tmux（macOS） | 开机自启；崩溃 10s 节流自动重启；macOS 原生；标准 stdout/stderr 日志 |
| 每用户串行锁 | 同一用户连发两条，两个 subprocess 同时 `--resume` 同 session_id 会状态错乱；不同用户不阻塞 |
| 原子写 sessions.json | tmp 文件 + `os.replace`，避免半写入 |
| `sendMessageDraft` 而不是 `editMessageText` | Bot API 9.5（2026-03-01）原生流式，客户端动画化，不受 1/s 编辑速率限制 |
| `--include-partial-messages` | 拿到 token 级 `text_delta`，不只是消息级整段返回 |
| 状态行 + 文字流叠加 | 用户最大痛点是"长任务时不知道还在不在跑"，状态行直接解决；token 流是锦上添花 |

---

## 2. 技术依赖（精确版本）

| 软件 | 版本 | 用途 |
|---|---|---|
| Python | 3.12+ | 主语言 |
| `python-telegram-bot[rate-limiter]` | **22.7** | Telegram Bot API 9.5 完整支持，含 `send_message_draft` 绑定 |
| Claude Code CLI | 2.1+ | 子进程 `claude -p` 主体 |
| Telegram Bot API | 9.5+（2026-03-01 起） | 客户端需支持 sendMessageDraft 才有动画效果（旧客户端只看到最终消息） |
| macOS | 13+（用 launchd 守护） | 也可改 systemd 跑在 Linux 上 |

⚠️ **PTB 必须 22.7+**。21.x 没有 `send_message_draft` 方法。

---

## 3. 关键 API 详解

### 3.1 `claude -p --output-format stream-json` 输出格式

启动命令：
```bash
claude -p "<prompt>" --output-format stream-json --verbose \
       --include-partial-messages --permission-mode bypassPermissions \
       --resume <session_id>
```

stdout 是 JSONL（每行一个 JSON 对象），事件类型：

#### `system/init`（启动时一次）
```json
{
  "type": "system",
  "subtype": "init",
  "session_id": "xxxxxxxx-xxxx-...",
  "tools": ["Bash","Read","Edit",...],
  "model": "claude-opus-4-7[1m]",
  "permissionMode": "bypassPermissions"
}
```

#### `stream_event/content_block_delta`（token 级流式 — 最关键）
```json
{
  "type": "stream_event",
  "event": {
    "type": "content_block_delta",
    "index": 0,
    "delta": { "type": "text_delta", "text": " hello" }
  }
}
```
其他 `delta.type`：`input_json_delta`（工具参数流，可忽略）、`thinking_delta`（思考过程，可忽略）。

实测：1406 字符分 18 个 delta，平均 ~0.5s/delta。

#### `assistant`（每个完整 content block 出现）
```json
{
  "type": "assistant",
  "message": {
    "content": [
      { "type": "text", "text": "完整文本块" },
      { "type": "tool_use", "name": "Bash", "input": {"command":"echo hi"} }
    ]
  }
}
```
监听 `tool_use` 提取工具名 + input → 状态行。`text` 块这里也有但已经被前面 `text_delta` 增量给完了，不要重复处理。

#### `user`（工具结果，可忽略）
```json
{
  "type": "user",
  "message": {
    "content": [
      { "type": "tool_result", "tool_use_id": "...", "is_error": false, "content": "hi\n" }
    ]
  }
}
```
不展示给用户（工具输出可能很长 / 无意义），只用于日志。

#### `result`（终结事件）
```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "duration_ms": 4170,
  "result": "<完整最终文本>",
  "session_id": "xxxxxxxx-...",
  "usage": {"input_tokens":..., "output_tokens":...},
  "total_cost_usd": 0.22
}
```
- 如果 `is_error=true` → 报错给用户
- 提取 `session_id` 持久化（覆盖旧的，因为 claude 可能新建 session）
- 提取 `result` 作为"最终干净版本"替换流式累积（避免边界格式 bug）

### 3.2 Telegram `sendMessageDraft`（Bot API 9.5）

PTB 22.7 绑定：

```python
async def send_message_draft(
    chat_id: int,
    draft_id: int,           # 必须非零，唯一标识本次流式会话
    text: str,
    message_thread_id: int | None = None,
    parse_mode: str | None = None,
    entities: Sequence[MessageEntity] | None = None,
    *, read_timeout, write_timeout, connect_timeout, pool_timeout, api_kwargs
) -> bool                    # 注意返回 bool，不是 Message 对象
```

**用法模式**：用同一个 `draft_id` 反复调用，`text` 是**累积全文**（不是增量）。Telegram 客户端会动画化展示 text 的变化。

**限制**：仅私聊。群组/频道不支持。

**关键陷阱**：
- 返回 `bool`，**没有 message_id**。所以 fallback 到 `editMessageText` 必须重新 `sendMessage` 一条占位拿到 message_id
- 同一秒内多次调用同一 `draft_id` 不会被节流（draft 不算 message edit），但还是建议 ~300ms 一次以免压垮 Telegram
- 一旦发送了真正的 `sendMessage`（finalize），draft 自动消失，不需要 `deleteMessageDraft`

### 3.3 Telegram 速率限制

| 限制 | 阈值 | 处理 |
|---|---|---|
| `editMessageText` 同消息 | ~1/s | 节流；HTTP 429 + `retry_after` |
| 单条消息字符 | 4096 | 用 4000 留余量 |
| 全 chat send/edit | ~30/s | 与 broadcast 共享池 |
| 编辑相同内容 | `BadRequest: not modified` | catch + 跳过 |

---

## 4. 从零搭建步骤

### 4.1 环境准备

```bash
# 检查 Python
python3 --version       # 应 ≥ 3.12

# 装 Claude Code CLI（如果还没装）
# 参考 https://docs.claude.com/en/docs/claude-code

# 配置 API（官方）
# 在 ~/.claude/settings.json 或环境变量中设置 ANTHROPIC_API_KEY

# 配置 API（第三方端点）
export ANTHROPIC_BASE_URL="https://your-endpoint.example.com"
export ANTHROPIC_AUTH_TOKEN="sk-..."
# 也可以写到 ~/.claude/settings.json:
# {"env": {"ANTHROPIC_BASE_URL": "...", "ANTHROPIC_AUTH_TOKEN": "..."}}

# 验证 claude 能跑
claude -p "say hi" --output-format json
```

### 4.2 创建 Telegram Bot

1. Telegram 找 [@BotFather](https://t.me/BotFather) → `/newbot` → 给 bot 起名 → 拿到 token（形如 `1234567890:AAH...`）
2. 保存 token，等下填进 `config.json`

### 4.3 拿白名单 user_id

Telegram bot 不能通过 username 主动查 user_id。流程：

1. 你给刚创建的 bot 发任意一条消息（如 `/start`）
2. 浏览器/curl 访问：
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[].message.from'
   ```
3. 提取数字 `id` 字段（整数，例如 `123456789`）

### 4.4 项目脚手架

```bash
git clone https://github.com/ztllll/claude-telegram-bot.git ~/code/claude-telegram-bot
cd ~/code/claude-telegram-bot
mkdir -p downloads logs
```

### 4.5 配置文件

`config.json`（chmod 600，用 `cp config.example.json config.json` 起手）：
```json
{
  "bot_token": "<BotFather token>",
  "allowed_user_ids": [<your_user_id>],
  "working_dir": "/Users/<you>/",
  "claude_args": ["--permission-mode", "bypassPermissions"],
  "max_response_chars": 4000,
  "subprocess_timeout_seconds": 600,
  "downloads_retention_days": 7,
  "draft_throttle_ms": 300
}
```

装依赖：
```bash
pip3 install --user -r requirements.txt
```

### 4.6 launchd 部署（macOS）

复制 `com.example.claude-telegram-bot.plist` → `~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist`，替换所有 `YOUR_*` 占位符（含 ANTHROPIC token），然后：

```bash
chmod 600 ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist
launchctl list | grep claude-telegram-bot   # PID 非空 = 启动成功
```

⚠️ **PATH 必须显式塞**——launchd 默认 PATH 不含 `~/.local/bin`，`claude` 命令会找不到。

---

## 5. 关键代码模块详解

### 5.1 `StreamRenderer` 类（核心）

职责：把 Claude 的事件流累积渲染到 Telegram draft，超 4000 字符自动封顶分多条。

```python
class StreamRenderer:
    def __init__(self, ctx, chat_id, user_id):
        self.bot = ctx.bot
        self.chat_id = chat_id
        # 必须非零 unique；时间戳 ns 取低 31 位 + |1 保证非零
        self.draft_id = int(time.time_ns() & 0x7FFFFFFF) | 1
        self.tool_log: list[str] = []      # 状态行历史
        self.text_buf = ""                  # 累积文字
        self.last_payload = ""              # 防"内容未变"错误
        self.last_update = 0.0              # 节流时间戳
        self.mode = "draft"                 # "draft" | "edit"（fallback）
        self.placeholder_id = None          # edit 模式下占位消息 id
        self.sealed_count = 0               # 封顶累计

    def _render(self) -> str:
        """状态行历史 + 空行 + 文字"""
        parts = []
        if self.tool_log:
            parts.append("\n".join(self.tool_log))
        if self.text_buf:
            if parts:
                parts.append("")
            parts.append(self.text_buf)
        return "\n".join(parts).rstrip()

    async def on_tool_use(self, name, inp):
        if self.tool_log == ["[初始化…]"]:
            self.tool_log.clear()       # 第一条 tool 来了，弹掉占位
        self.tool_log.append(format_tool_status(name, inp))
        await self._flush(force=True)   # 工具调用立即刷新

    async def on_text_delta(self, text):
        if not text: return
        if self.tool_log == ["[初始化…]"]:
            self.tool_log.clear()
        self.text_buf += text
        await self._flush(force=False)  # 文字流节流

    async def on_complete(self, final_text):
        # 用 result 字段替换 buf（最干净版）
        if final_text is not None:
            self.text_buf = final_text
        # 拆图与文字
        img_paths, clean = extract_images(self.text_buf)
        # 落地（替代 draft，draft 自动消失）
        if clean.strip():
            for part in chunk(clean, MAX_CHARS):
                await self.bot.send_message(chat_id=self.chat_id, text=part)
        await send_images(self.ctx, self.chat_id, img_paths)
```

完整实现见 `bot.py`。fallback 路径（draft 不可用时降级到 editMessageText）、4000 字符封顶、错误处理等细节在源码中。

### 5.2 工具状态格式化

```python
def format_tool_status(name, inp):
    inp = inp or {}
    if name == "Bash":     return f"[bash] {_trim(inp.get('command',''), 80)}"
    if name == "Read":     return f"[read] {_trim(inp.get('file_path',''), 100)}"
    if name == "Edit":     return f"[edit] {_trim(inp.get('file_path',''), 100)}"
    if name == "Write":    return f"[write] {_trim(inp.get('file_path',''), 100)}"
    if name == "Glob":     return f"[glob] {_trim(inp.get('pattern',''), 60)}"
    if name == "Grep":     return f"[grep] {_trim(inp.get('pattern',''), 60)}"
    if name == "WebFetch": return f"[fetch] {_trim(inp.get('url',''), 100)}"
    if name == "WebSearch":return f"[search] {_trim(inp.get('query',''), 100)}"
    # 加新工具的格式：在这里加分支
    summary = ", ".join(f"{k}={_trim(v,30)}" for k,v in list(inp.items())[:2])
    return f"[{name.lower()}] {_trim(summary, 100)}"
```

### 5.3 流式调用 `stream_claude`

骨架（完整版见 `bot.py`）：

```python
async def stream_claude(prompt, user_id, renderer):
    args = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if (sid := SESSIONS.get(user_id)):
        args += ["--resume", sid]
    args += CLAUDE_ARGS

    proc = await asyncio.create_subprocess_exec(
        *args, cwd=WORKING_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )

    async def consume():
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line: continue
            try: e = json.loads(line)
            except json.JSONDecodeError: continue

            t = e.get("type")
            if t == "stream_event":
                ev = e.get("event", {})
                if ev.get("type") == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        await renderer.on_text_delta(d.get("text", ""))
            elif t == "assistant":
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        await renderer.on_tool_use(c.get("name","?"), c.get("input",{}) or {})
            elif t == "result":
                # 持久化 session_id，调用 renderer.on_complete()
                ...

    await asyncio.wait_for(consume(), timeout=TIMEOUT)
```

### 5.4 图片回传协议

让 Claude 在回复中用 `[[img:/绝对路径]]` 标记表示要发图：

```python
IMG_MARKER_RE = re.compile(r"\[\[img:([^\]\n]+)\]\]")

def extract_images(text):
    paths = [m.group(1).strip() for m in IMG_MARKER_RE.finditer(text)]
    cleaned = IMG_MARKER_RE.sub("", text).strip()
    return paths, cleaned

async def send_images(ctx, chat_id, paths):
    for p in paths:
        f = Path(p)
        if not f.is_file():
            await ctx.bot.send_message(chat_id, f"[图片不存在] {p}")
            continue
        with open(f, "rb") as fh:
            await ctx.bot.send_photo(chat_id, fh)
```

让 Claude 知道这个协议：在 `~/.claude/projects/<your-project>/memory/` 添加一条 project memory：

```markdown
---
name: 图片回传协议
description: 通过 Telegram 给用户发图片时使用 [[img:/path]] 标记
type: project
---

当用户通过 Telegram 跟我对话、需要给他展示截图/图片时，直接在回复正文里写 `[[img:/绝对路径]]` 即可。bot 会扫描标记，对每条调 sendPhoto，并把标记从文本里剥掉再发剩余文字。多张图就写多个标记。文件不存在或发送失败 bot 会单独报错。

不要再用 curl 调 Telegram API 来发图。
```

### 5.5 多用户隔离

```python
USER_LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

async def handle_message(update, ctx):
    uid = update.effective_user.id
    async with USER_LOCKS[uid]:    # 同用户串行
        renderer = StreamRenderer(ctx, msg.chat_id, uid)
        await renderer.start_placeholder()
        if (err := await stream_claude(prompt, uid, renderer)):
            await renderer.fail(err)
```

不同用户互不阻塞（不同 lock）。`defaultdict` 自动按需创建。

---

## 6. Configuration

### `config.json` 字段

```json
{
  "bot_token": "BotFather token",
  "allowed_user_ids": [整数数组],
  "working_dir": "claude 子进程的 cwd（影响相对路径解析）",
  "claude_args": ["传给 claude 的额外参数（除 -p/--resume/--output-format 之外）"],
  "max_response_chars": 4000,
  "subprocess_timeout_seconds": 600,
  "downloads_retention_days": 7,
  "draft_throttle_ms": 300
}
```

⚠️ 改完 `config.json` 必须重启 bot 才生效：
```bash
launchctl kickstart -k gui/$(id -u)/com.<your-id>.claude-telegram-bot
```

### `claude_args` 调整示例

**默认（无限制）**：
```json
"claude_args": ["--permission-mode", "bypassPermissions"]
```

**严格模式**（只读 + 限定 bash 命令）：
```json
"claude_args": [
  "--permission-mode", "dontAsk",
  "--allowed-tools", "Read,Bash(git *)"
]
```

bot.py 启动时会自动追加 `--include-partial-messages`（如果不在的话），不需要手动加。

### plist 的 `EnvironmentVariables`

| 变量 | 用途 | 必填 |
|---|---|---|
| `PATH` | 否则找不到 `claude` 命令 | ✅ |
| `HOME` | claude 读 `~/.claude/settings.json` 的根 | ✅ |
| `ANTHROPIC_BASE_URL` | 第三方 API 端点 | 第三方 API 时 ✅ |
| `ANTHROPIC_AUTH_TOKEN` | API token（第三方） | 第三方 API 时 ✅ |
| `ANTHROPIC_API_KEY` | API token（官方） | 用官方 API 时 ✅ |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | 关掉非必要遥测 | 否 |

---

## 7. 服务管理

```bash
# 状态（PID 非 - = 在跑）
launchctl list | grep claude-telegram-bot

# 重启（最常用）
launchctl kickstart -k gui/$(id -u)/com.<your-id>.claude-telegram-bot

# 停止
launchctl bootout gui/$(id -u)/com.<your-id>.claude-telegram-bot

# 启动
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist

# 实时日志
tail -f ~/code/claude-telegram-bot/logs/stdout.log

# 错误日志（应基本为空）
tail -f ~/code/claude-telegram-bot/logs/stderr.log
```

---

## 8. 测试矩阵

| 用例 | 操作 | 期望 |
|---|---|---|
| Smoke | 发 `你好` | 5-15s 收到回复，1-2 条 message |
| 状态行 | 发 `用 bash 列下 /tmp 文件` | 立刻看到 `[bash] ls /tmp` 状态行，然后渐进文字 |
| 多工具 | 发 `读一下 ~/.claude/settings.json 然后总结` | 多条 `[read]/[bash]` 状态行累积 |
| Token 流 | 发 `写一首 200 字关于咖啡的散文` | 文字逐字/逐句出现 |
| 长输出 | 发 `写一篇 3000 字的 Python asyncio 教程` | 自动分多条消息，连贯 |
| 多轮 | 任意问题 → `刚才我说了什么？` | 第二条用 `--resume`，正确记得第一条 |
| 新会话 | `/new` → 任意问题 | session 重置 |
| 图片输入 | 发图片 + caption `这是什么？` | claude 看到图，回答 |
| 文档输入 | 发文本/代码文件 | claude 读到内容，回答 |
| 图片回传 | `用 screencapture 截屏并发我` | claude 用 `[[img:/tmp/x.png]]` 标记，bot 调 sendPhoto |
| 白名单 | 用非白名单账号发消息 | 无回复，logs 中 `REJECTED uid=...` |
| 重启 | `launchctl kickstart -k ...` | 进程重启，原 session 仍可恢复（sessions.json 持久化） |
| 崩溃恢复 | `kill -9 <pid>` | 10s 内 launchd 自动重启 |

---

## 9. 故障排查

```
症状：bot 不回应
├── launchctl list | grep claude-telegram-bot
│   ├── 没输出 → 服务未加载 → bootstrap
│   ├── PID = -      → 进程死了 → 看 stderr.log
│   └── PID 数字 → 在跑，往下查
├── tail logs/stdout.log | grep REJECTED
│   └── 有 → 你不在白名单 → 改 config.json
├── ps aux | grep bot.py | grep -v grep
│   └── 多个进程 → polling 冲突（409） → kill 多余的
├── curl -m 5 $ANTHROPIC_BASE_URL/ -o /dev/null -w "%{http_code}\n"
│   └── 连不上 → API 端点问题
└── 否则查 stderr.log

症状：bot 回 "claude 退出码 X"
└── 终端复现：claude -p "hi" --output-format json --permission-mode bypassPermissions
    └── 看具体错误（token 过期、CLI 版本问题、API 端点 down）

症状：bot 回 "claude 响应超时"
├── 是长任务？→ 调高 config.json 的 subprocess_timeout_seconds
└── 简单对话也超时 → API 端点慢或挂

症状：状态行不出现 / 文字不流式
├── tail logs/stdout.log | grep "claude_args"
│   └── 确认含 --include-partial-messages
├── 第三方 API 端点是否支持 SSE？（partial messages 依赖 SSE）
│   └── 不支持 → 状态行仍能工作，文字不流式但仍能回
└── PTB 版本？
    └── pip3 show python-telegram-bot | grep Version  → 应 22.7+

症状："Task was destroyed but it is pending!"（warning）
└── _post_init 里 asyncio.create_task(_cleanup_loop()) 没持有引用，
    bot 关闭时被强销。无功能影响。修：保留引用 + post_shutdown 取消。

症状：BadRequest "draft" / "not supported"
├── Telegram 客户端版本太旧（不支持 sendMessageDraft 渲染）
│   └── bot 已自动 fallback 到 editMessageText，仍能用，只是没原生动画
└── 群组/频道场景（sendMessageDraft 仅私聊）→ 同上
```

### 常用诊断命令

```bash
# 看完整启动日志
head -50 ~/code/claude-telegram-bot/logs/stdout.log

# 查最近一次 claude 调用的耗时
grep "claude stream done" ~/code/claude-telegram-bot/logs/stdout.log | tail -5

# 看 sessions 持久化是否正常
cat ~/code/claude-telegram-bot/sessions.json | python3 -m json.tool

# 检查 PTB 版本
pip3 show python-telegram-bot | grep Version

# 检查 send_message_draft 绑定
python3 -c "from telegram import Bot; print('send_message_draft' in dir(Bot))"

# 验证 stream-json 输出
cd /tmp && claude -p "test" --output-format stream-json --verbose --include-partial-messages --permission-mode bypassPermissions
```

---

## 10. Hardening（加固）

| 风险 | 影响 | 缓解 |
|---|---|---|
| `bot_token` 泄漏 | 任何人能向 bot 发消息 | 仍需在白名单内才被处理 |
| 白名单内账号被盗 | **攻击者获得 home 全权限**（claude 默认在 home，无 tool 限制） | Telegram 开二步验证；考虑 `claude_args` 加 `--allowed-tools` 限制；考虑加 bot 内二级密码 |
| token 写在 plist | 文件可读则泄漏 | chmod 600 |
| 第三方 API HTTP 明文 | token 在网络明文 | 优先用 HTTPS 端点 |
| 群组场景 | 整个群成员都能用 bot | 当前 `is_allowed` 只验 `effective_user.id`。需要的话加 `update.effective_chat.type == "private"` |
| 项目目录被云同步 | token 落到云端 | iCloud/Dropbox/git 一律排除该目录 |

### 加 bot 二级密码（可选改造）

```python
# config.json 加 "second_password": "xxx"
AUTHED: set[int] = set()  # 内存中的"已认证"用户

async def handle_message(update, ctx):
    uid = update.effective_user.id
    if uid not in AUTHED:
        text = (update.message.text or "").strip()
        if text == f"/auth {SECOND_PASSWORD}":
            AUTHED.add(uid)
            await update.message.reply_text("已认证")
            return
        await update.message.reply_text("先 /auth <密码>")
        return
    # ... 原有逻辑
```

`AUTHED` 是内存集合，bot 重启会清空（重新认证），更安全。

---

## 11. 常见改造

### 加白名单用户

```bash
# 1. 让对方先给 bot 发消息
# 2. 拉 user_id
curl -s "https://api.telegram.org/bot$(jq -r .bot_token ~/code/claude-telegram-bot/config.json)/getUpdates" \
    | jq '.result[].message.from | {id, username}'
# 3. 把 id 加入 config.json 的 allowed_user_ids
# 4. 重启
launchctl kickstart -k gui/$(id -u)/com.<your-id>.claude-telegram-bot
```

### 切换工作目录（缩减权限）

```bash
mkdir -p ~/code/claude-telegram-bot/workspace
# 改 config.json 的 working_dir 指到这里
# 重启
```

### 换模型

`~/.claude/settings.json`：
```json
{ "model": "claude-haiku-4-5-20251001" }
```
bot 自动继承，无需改代码。

### 关闭流式（回到一次性返回）

把 bot.py 中以下行注释掉：
```python
if "--include-partial-messages" not in CLAUDE_ARGS:
    CLAUDE_ARGS.append("--include-partial-messages")
```
状态行仍工作（不依赖 partial），文字会变成一次性出现。

### 跑在 Linux（systemd）

把 launchd 部署部分换成 systemd 单元：

```ini
# /etc/systemd/system/claude-telegram-bot.service
[Unit]
Description=Claude Telegram Bot
After=network-online.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/code/claude-telegram-bot
ExecStart=/home/<your-user>/code/claude-telegram-bot/run.sh
Restart=always
RestartSec=10
Environment="PATH=/home/<your-user>/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="HOME=/home/<your-user>"
Environment="ANTHROPIC_BASE_URL=https://your-endpoint.example.com"
Environment="ANTHROPIC_AUTH_TOKEN=sk-..."
StandardOutput=append:/home/<your-user>/code/claude-telegram-bot/logs/stdout.log
StandardError=append:/home/<your-user>/code/claude-telegram-bot/logs/stderr.log

[Install]
WantedBy=default.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-telegram-bot
sudo systemctl status claude-telegram-bot
```

`config.json` 等其他东西无需改动。

---

## 12. 已知小遗留

- `_post_init` 里 `asyncio.create_task(_cleanup_loop())` 没保留引用，bot 关闭时日志会出现 `Task was destroyed but it is pending! (Task-6 _cleanup_loop)` warning。无功能影响。修复：把 task 存到 `app.bot_data["_cleanup_task"]`，在 `post_shutdown` 钩子里 `cancel()`。
- `sendChatAction` typing 提示已废弃（draft 本身就是反馈），相关代码 `_keep_typing` 已移除（之前版本有）。

---

## 13. 附录：快速参考卡

```
项目:   ~/code/claude-telegram-bot/
入口:   bot.py (~510 行, Python 3.12)
进程:   launchd (macOS), label = com.<your-id>.claude-telegram-bot
日志:   ~/code/claude-telegram-bot/logs/{stdout,stderr}.log
配置:   ~/code/claude-telegram-bot/config.json (chmod 600)
plist:  ~/Library/LaunchAgents/com.<your-id>.claude-telegram-bot.plist (chmod 600)
依赖:   python-telegram-bot[rate-limiter]==22.7

关键命令:
  重启:  launchctl kickstart -k gui/$(id -u)/com.<your-id>.claude-telegram-bot
  日志:  tail -f ~/code/claude-telegram-bot/logs/stdout.log
  状态:  launchctl list | grep claude-telegram-bot
  白名单: jq '.allowed_user_ids' ~/code/claude-telegram-bot/config.json

claude 调用形态:
  claude -p "<prompt>" --output-format stream-json --verbose
         --include-partial-messages --permission-mode bypassPermissions
         [--resume <session_id>]

Telegram 流式 API:
  await bot.send_message_draft(chat_id, draft_id, text)
  # draft_id 非零，同流多次调用累积渲染
  # 完成时用 send_message 落地

Bot 命令:
  /start  /help  欢迎
  /new   开新会话（清当前 session_id）
  /status 查看 session_id 前 8 位

图片回传协议（claude 在回复中使用）:
  [[img:/绝对路径]]   bot 自动调 sendPhoto，标记从文本剥除
```
