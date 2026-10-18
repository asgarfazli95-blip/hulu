#!/usr/bin/env python3
"""
𝐏𝐘-𝐇𝐎𝐒𝐓𝐄𝐑 (High-Level OP Edition - FINAL)
- Strictly runs .py files.
- Secret Spy: Notifies admin on every Upload/Start/Stop.
- Bulletproof Auto-Heal: Parses custom pip messages, installs all missing deps with versions.
- Full Admin Panel: Approve, Ban, Unban, Nuke, Broadcast.
"""

import os, sys, asyncio, json, logging, time, hashlib, re, shlex
from pathlib import Path
from datetime import datetime

# ==================== AUTO-INSTALL CORE ====================
def _boot():
    pkgs = [
        ("telegram", "python-telegram-bot>=21"),
        ("dotenv",   "python-dotenv"),
        ("psutil",   "psutil"),
    ]
    need = []
    for mod, pkg in pkgs:
        try: __import__(mod)
        except ImportError: need.append(pkg)
    if need:
        import subprocess
        print(f"[PYHOST] Installing missing core packages: {', '.join(need)}")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", *need])
        except Exception as e:
            print(f"[PYHOST] Core pip install failed: {e}", file=sys.stderr)

try: _boot()
except Exception as e: print(f"[PYHOST] Boot deps failed: {e}", file=sys.stderr)

import psutil
from dotenv import load_dotenv
from telegram import (ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update, BotCommand)
from telegram.constants import ParseMode
from telegram.error import (BadRequest, Forbidden, RetryAfter, TimedOut,
                            NetworkError, TelegramError, Conflict)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ConversationHandler, MessageHandler, filters, ContextTypes)

# ==================== LOGGING ====================
log = logging.getLogger("pyhost")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

# ==================== RESILIENCE LAYER ====================
RETRY_MAX = 3
RETRY_BASE = 1.5
RETRYABLE = (TimedOut, NetworkError, RetryAfter)

async def _retry(coro_factory, max_tries=RETRY_MAX):
    last = None
    for attempt in range(max_tries):
        try: return await coro_factory()
        except RetryAfter as e: await asyncio.sleep(float(e.retry_after) + 0.5); last = e
        except RETRYABLE as e: await asyncio.sleep(RETRY_BASE ** attempt); last = e
    if last: raise last

async def safe_reply(message, text, **kwargs):
    if message is None: return None
    async def _do():
        try: return await message.reply_text(text, **kwargs)
        except BadRequest as e:
            s = str(e).lower()
            if "not found" in s or "reply" in s or "message to be" in s:
                return await message.get_bot().send_message(chat_id=message.chat_id, text=text, **kwargs)
            if "parse" in s or "entities" in s:
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await message.get_bot().send_message(chat_id=message.chat_id, text=text, **k)
            raise
        except Forbidden: return None
    try: return await _retry(_do)
    except TelegramError as e: log.warning("safe_reply: %s", e)

async def safe_edit(query, text, **kwargs):
    if query is None: return None
    async def _do():
        try: return await query.edit_message_text(text, **kwargs)
        except BadRequest as e:
            s = str(e).lower()
            if "not modified" in s: return None
            if "not found" in s or "message to edit" in s:
                return await query.get_bot().send_message(chat_id=query.from_user.id, text=text, **kwargs)
            if "parse" in s or "entities" in s:
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await query.get_bot().send_message(chat_id=query.from_user.id, text=text, **k)
            raise
    try: return await _retry(_do)
    except TelegramError as e: log.warning("safe_edit: %s", e)

async def safe_send(bot, chat_id, text, **kwargs):
    async def _do():
        try: return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except BadRequest as e:
            if "parse" in str(e).lower() or "entities" in str(e).lower():
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await bot.send_message(chat_id=chat_id, text=text, **k)
            raise
        except Forbidden: return None
    try: return await _retry(_do)
    except TelegramError as e: log.warning("safe_send: %s", e)

async def _nudge(update, context):
    try:
        if update is not None and getattr(update, "callback_query", None):
            try: await update.callback_query.answer()
            except Exception: pass
        chat_id = update.effective_chat.id if getattr(update, "effective_chat", None) else None
        if not chat_id and getattr(update, "callback_query", None):
            chat_id = update.callback_query.from_user.id
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ action hiccuped — try again")
    except Exception: pass

def shield(fn):
    async def _wrap(update, context, *a, **kw):
        try: return await fn(update, context, *a, **kw)
        except (BadRequest, Forbidden) as e:
            log.warning("shield[%s]: %s", fn.__name__, e); await _nudge(update, context)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try: return await fn(update, context, *a, **kw)
            except Exception: pass
        except Conflict as e: log.error("TOKEN CONFLICT: %s", e)
        except Exception as e:
            log.exception("shield[%s]: %s", fn.__name__, e); await _nudge(update, context)
    return _wrap

async def on_error(update, context):
    err = context.error
    if isinstance(err, Conflict): log.error("POLLING CONFLICT: %s", err); return
    if isinstance(err, RetryAfter): await asyncio.sleep(float(err.retry_after) + 0.5); return
    if isinstance(err, (BadRequest, Forbidden)): log.warning("error soft: %s", err); return
    log.exception("error handler:", exc_info=err); await _nudge(update, context)

# ==================== CONFIG ====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN: raise SystemExit("BOT_TOKEN missing in .env")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
if not ADMIN_IDS: raise SystemExit("ADMIN_IDS missing in .env")

WORKSPACE = Path(os.getenv("WORKSPACE_BASE", "./workspaces")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
DATA_FILE = WORKSPACE / "shadow_data.json"
LOG_CAP = 10 * 1024 * 1024
WATCHDOG = 5

# ==================== DATA ====================
def _load():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {"users": {}, "scripts": {}}

D = _load()
USERS = D["users"]; SCRIPTS = D["scripts"]

def persist():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(D, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e: log.warning("persist: %s", e)

def banner(title: str = "") -> str:
    head = f"🐍 · 𝐏𝐘-𝐇𝐎𝐒𝐓𝐄𝐑 · 🐍"
    return f"{head}\n{'━'*22}\n{title}" if title else f"{head}\n{'━'*22}"

# ==================== SPY FEATURE ====================
async def notify_admins(bot, text):
    if not bot: return
    for aid in ADMIN_IDS:
        try: await bot.send_message(chat_id=aid, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

# ==================== SUPERVISOR ====================
SUPERVISORS = {}

class ScriptSupervisor:
    def __init__(self, sid, uid, path, auto=True, env=None):
        self.sid = sid; self.uid = uid
        self.path = Path(path).resolve()
        self.auto = auto; self.env = env or {}
        self.log = self.path.parent / "logs" / f"{sid}.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.proc = None; self._task = None; self._stop = False
        self.restarts = 0; self.backoff = 1; self._healing = False
        self.started = None

    def running(self): return self.proc is not None and self.proc.returncode is None

    def _w(self, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.log.exists() and self.log.stat().st_size > LOG_CAP: self.log.write_text("", encoding="utf-8")
            with open(self.log, "a", encoding="utf-8") as f: f.write(f"[{ts}] [PYHOST] {msg}\n")
        except Exception: pass

    def stats(self):
        if not self.running(): return None
        try:
            p = psutil.Process(self.proc.pid)
            with p.oneshot():
                return {"cpu": round(p.cpu_percent(interval=0), 1), "mem": round(p.memory_info().rss / 1024 / 1024, 1)}
        except Exception: return None

    async def start(self, bot=None):
        if self.running(): return
        self._stop = False
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"; env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUTF8"] = "1"
        env.update(self.env)

        try:
            self._w(f"launching {self.path}")
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(self.path),
                cwd=str(self.path.parent), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
            )
            self.started = datetime.now()
            self._w(f"PID {self.proc.pid}")
            
            u = USERS.get(str(self.uid), {})
            uname = u.get('username', 'Unknown')
            await notify_admins(bot, f"🕵️‍♂️ **Spy Alert**\n\n👤 User: @{uname} (`{self.uid}`)\n🚀 Started: `{self.path.name}`")

            self._task = asyncio.create_task(self._pump(bot))
        except Exception as e:
            self._w(f"launch fail: {e}")

    async def _pump(self, bot=None):
        if not self.proc or not self.proc.stdout: return
        tail = []
        try:
            with open(self.log, "a", encoding="utf-8") as lf:
                while True:
                    ln = await self.proc.stdout.readline()
                    if not ln: break
                    t = ln.decode(errors="replace")
                    lf.write(t); lf.flush(); tail.append(t)
                    if len(tail) > 150: tail = tail[-150:]
        except Exception as e: self._w(f"pump: {e}")
        finally:
            if self.proc:
                await self.proc.wait()
                self._w(f"exited {self.proc.returncode}")
            tail_text = "".join(tail[-80:])
            if self.auto and not self._stop and bot:
                await self._heal(bot, tail_text)

    async def _heal(self, bot, tail):
        if self._healing: return
        self._healing = True
        try:
            note = ""
            pip_logs = []
            
            # 1. Try to run the exact `pip install ...` line the script printed
            pip_matches = re.findall(r"pip install (.+)", tail, re.MULTILINE)
            if pip_matches:
                cmd_str = pip_matches[0].strip()
                try:
                    args = shlex.split(cmd_str)
                    rc, out = await run_cmd([sys.executable, "-m", "pip", "install"] + args)
                    if rc == 0:
                        note = f"📦 `pip install {cmd_str}` executed"
                    else:
                        pip_logs.append(f"Full command failed: {out[-200:]}")
                except Exception as e:
                    pip_logs.append(f"Parse error: {e}")
            
            # 2. If full command failed, try extracting individual missing modules
            if not note:
                pkgs_to_install = set()
                mod_matches = re.findall(r"No module named ['\"]([^'\"]+)['\"]", tail)
                for mod in mod_matches:
                    mod = mod.split(".")[0]
                    pkg_map = {
                        "telegram": "python-telegram-bot", "dotenv": "python-dotenv",
                        "gtts": "gTTS", "edge_tts": "edge-tts", "yt_dlp": "yt-dlp",
                        "PIL": "Pillow", "bs4": "beautifulsoup4", "cv2": "opencv-python"
                    }
                    pkgs_to_install.add(pkg_map.get(mod, mod))
                
                # Also extract from the pip install line just in case
                if pip_matches:
                    for match in pip_matches:
                        for part in match.split():
                            part = part.strip("'\"")
                            if part and not part.startswith("-") and part not in ("pip", "install", "-U", "-r"):
                                pkgs_to_install.add(part)

                success_pkgs = []
                failed_pkgs = []
                for pkg in pkgs_to_install:
                    if not pkg or pkg.startswith("-"): continue
                    
                    rc, out = await run_cmd([sys.executable, "-m", "pip", "install", pkg])
                    if rc == 0:
                        success_pkgs.append(pkg)
                        continue
                        
                    rc, out = await run_cmd([sys.executable, "-m", "pip", "install", "--user", pkg])
                    if rc == 0:
                        success_pkgs.append(pkg)
                    else:
                        failed_pkgs.append(pkg)
                        pip_logs.append(f"[{pkg}]\n{out[-200:]}")

                if success_pkgs:
                    note += f"📦 Installed: `{'`, `'.join(success_pkgs)}`\n"
                if failed_pkgs:
                    note += f"❌ Failed: `{'`, `'.join(failed_pkgs)}`\n"

            if note:
                await safe_send(bot, self.uid, f"🩹 *auto-heal* · `{self.path.name}`\n{note}\n♻️ restarting…", parse_mode=ParseMode.MARKDOWN)
            else:
                log_str = "\n".join(pip_logs[-3:]) if pip_logs else ""
                await safe_send(bot, self.uid, f"💥 *crash* · `{self.path.name}`\n```\n{tail[-400:]}\n{log_str}\n```\n♻️ restarting…", parse_mode=ParseMode.MARKDOWN)
            
            self.restarts += 1
            await asyncio.sleep(min(self.backoff, 60))
            self.backoff = min(self.backoff * 2, 60)
            await self.start(bot)
        finally: self._healing = False

    async def stop(self, bot=None):
        self._stop = True
        if self._task and not self._task.done(): self._task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                try: await asyncio.wait_for(self.proc.wait(), timeout=5)
                except asyncio.TimeoutError: self.proc.kill(); await self.proc.wait()
            except Exception: pass
        self._w("stopped")
        self.proc = None
        
        if bot:
            u = USERS.get(str(self.uid), {})
            uname = u.get('username', 'Unknown')
            await notify_admins(bot, f"🕵️‍♂️ **Spy Alert**\n\n👤 User: @{uname} (`{self.uid}`)\n🛑 Stopped: `{self.path.name}`")

async def run_cmd(cmd, timeout=600):
    env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUTF8"] = "1"
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace")
    except Exception as e: return -1, str(e)

# ==================== HELPERS ====================
def get_user(uid): return USERS.get(str(uid))
def add_user(uid, name, uname):
    USERS[str(uid)] = {"status": "pending", "name": name, "username": uname}
    persist()
def approve(uid):
    u = USERS.get(str(uid))
    if u: u["status"] = "approved"; persist(); return True
def ban(uid):
    u = USERS.get(str(uid))
    if u: u["status"] = "banned"; persist(); return True
def unban(uid): return approve(uid)
def is_ok(uid): return bool(get_user(uid) and get_user(uid)["status"] == "approved")
def is_pend(uid): return bool(get_user(uid) and get_user(uid)["status"] == "pending")
def is_ban(uid): return bool(get_user(uid) and get_user(uid)["status"] == "banned")
def user_scripts(uid): return [s for s in SCRIPTS.values() if s["user_id"] == uid]
def ws_of(uid):
    w = WORKSPACE / str(uid); w.mkdir(parents=True, exist_ok=True); return w

# ==================== UI ====================
BTN_DEPLOY = "🚀 Deploy"
BTN_UPLOAD = "📁 Upload"
BTN_LOGS   = "📜 Logs"
BTN_STATS  = "📊 Stats"
BTN_STOP   = "🛑 Stop All"
BTN_ADMIN  = "👑 Admin Panel"
BTN_TEXTS  = {BTN_DEPLOY, BTN_UPLOAD, BTN_LOGS, BTN_STATS, BTN_STOP, BTN_ADMIN}

def reply_kb(uid):
    rows = [[BTN_DEPLOY, BTN_UPLOAD], [BTN_LOGS, BTN_STATS], [BTN_STOP]]
    if uid in ADMIN_IDS: rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup([[KeyboardButton(b) for b in r] for r in rows], resize_keyboard=True, is_persistent=True)

# ==================== HANDLERS ====================
@shield
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    uid, name, uname = u.id, u.full_name, u.username or "no_username"
    
    if not get_user(uid):
        add_user(uid, name, uname)
        for aid in ADMIN_IDS:
            await safe_send(ctx.bot, aid, f"🔔 *New Request*\n👤 {name}\n🆔 `{uid}`\n📛 @{uname}", parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"), InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{uid}")]]))
        return await safe_reply(update.message, banner("✅ request sent to admin"))
        
    if is_ban(uid): return await safe_reply(update.message, "🚫 banned")
    if is_pend(uid): return await safe_reply(update.message, "⏳ pending")
    await safe_reply(update.message, banner(f"👋 welcome {name}"), reply_markup=reply_kb(uid))

async def _deploy_rows(uid):
    rows = []
    for s in user_scripts(uid):
        sup = SUPERVISORS.get(s["sid"])
        dot = "🟢" if (sup and sup.running()) else "⚪"
        rows.append([InlineKeyboardButton(f"{dot}  {s['filename']}", callback_data=f"sview_{s['sid']}")])
    return rows

@shield
async def on_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid): return await safe_reply(update.message, "❌ not approved", reply_markup=reply_kb(uid))
    rows = await _deploy_rows(uid)
    if not rows: return await safe_reply(update.message, banner("📂 no scripts") + "\n\nupload one first", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Upload", callback_data="upload")]]))
    await safe_reply(update.message, banner("🚀 Deploy"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def deploy_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = await _deploy_rows(q.from_user.id)
    if not rows: return await safe_edit(q, "no scripts")
    await safe_edit(q, banner("🚀 Deploy"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def script_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sview_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id: return await safe_edit(q, "❌ not found")
    sup = SUPERVISORS.get(sid)
    run = sup.running() if sup else False
    pid = sup.proc.pid if sup and run else "—"
    auto = "♻️ ON" if s.get("autorestart", True) else "⛔ OFF"
    st = sup.stats() if sup else None
    stats_line = f"\n📈 CPU `{st['cpu']}%` · RAM `{st['mem']}MB`" if st else ""
    
    txt = (banner(f"📄 {s['filename']}") + "\n\n"
           f"🆔 `{sid}`\n📊 {'🟢 RUNNING' if run else '⚪ STOPPED'}\n🔢 PID `{pid}`\n"
           f"♻️ Auto: {auto}\n🔄 Restarts: {sup.restarts if sup else 0}{stats_line}\n"
           f"📁 `{s['path']}`")
    kb = []
    if run: kb.append([InlineKeyboardButton("🔄 Restart", callback_data=f"srestart_{sid}"), InlineKeyboardButton("🛑 Stop", callback_data=f"sstop_{sid}")])
    else: kb.append([InlineKeyboardButton("🚀 Start", callback_data=f"sstart_{sid}")])
    kb.append([InlineKeyboardButton("📜 Live Log", callback_data=f"slog_{sid}"), InlineKeyboardButton("👁 Code", callback_data=f"scode_{sid}")])
    kb.append([InlineKeyboardButton("✏️ Edit", callback_data=f"sedit_{sid}"), InlineKeyboardButton("⚙️ Env", callback_data=f"senv_{sid}")])
    kb.append([InlineKeyboardButton(f"♻️ {auto}", callback_data=f"sauto_{sid}"), InlineKeyboardButton("🗑 Delete", callback_data=f"sdel_{sid}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="deploy_list")])
    await safe_edit(q, txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

@shield
async def s_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sstart_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id: return
    if sid not in SUPERVISORS: SUPERVISORS[sid] = ScriptSupervisor(sid, s["user_id"], s["path"], s.get("autorestart", True), s.get("env", {}))
    sup = SUPERVISORS[sid]
    if not sup.running():
        s["runs"] = s.get("runs", 0) + 1; persist()
        await sup.start(ctx.bot)
    q.data = f"sview_{sid}"
    await script_view(update, ctx)

@shield
async def s_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sstop_", "", 1)
    if sid in SUPERVISORS: await SUPERVISORS[sid].stop(ctx.bot)
    q.data = f"sview_{sid}"
    await script_view(update, ctx)

@shield
async def s_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("srestart_", "", 1)
    if sid in SUPERVISORS: await SUPERVISORS[sid].stop(ctx.bot)
    q.data = f"sstart_{sid}"
    await s_start(update, ctx)

@shield
async def s_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sauto_", "", 1)
    s = SCRIPTS.get(sid)
    if s:
        s["autorestart"] = not s.get("autorestart", True)
        persist()
        if sid in SUPERVISORS: SUPERVISORS[sid].auto = s["autorestart"]
    q.data = f"sview_{sid}"
    await script_view(update, ctx)

@shield
async def s_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sdel_", "", 1)
    if sid in SUPERVISORS:
        await SUPERVISORS[sid].stop(ctx.bot)
        del SUPERVISORS[sid]
    if sid in SCRIPTS:
        p = Path(SCRIPTS[sid]["path"])
        if p.exists(): p.unlink()
        del SCRIPTS[sid]
        persist()
    q.data = "deploy_list"
    await deploy_list(update, ctx)

@shield
async def s_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("slog_", "", 1)
    s = SCRIPTS.get(sid)
    if not s: return
    sup = SUPERVISORS.get(sid)
    lp = sup.log if sup else Path(s["path"]).parent / "logs" / f"{sid}.log"
    try: tail = lp.read_text(encoding="utf-8", errors="replace")[-3500:] if lp.exists() else "(No logs yet)"
    except Exception as e: tail = f"Error: {e}"
    kb = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"slog_{sid}")], [InlineKeyboardButton("🔙 Back", callback_data=f"sview_{sid}")]]
    await safe_edit(q, f"📜 *Logs for {s['filename']}*\n```\n{tail}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

@shield
async def s_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("scode_", "", 1)
    s = SCRIPTS.get(sid)
    if not s: return
    try:
        c = Path(s["path"]).read_text(encoding="utf-8", errors="replace")
        if len(c) > 3500: c = c[:3500] + "\n…"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data=f"sview_{sid}")]]
        await safe_edit(q, f"👁 `{s['filename']}`\n```\n{c}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e: await safe_edit(q, f"❌ {e}")

# ==================== CONVERSATIONS ====================
(UP_WAIT, EDIT_WAIT, BC_WAIT, ENV_WAIT) = range(4)

@shield
async def up_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_ok(update.effective_user.id): return ConversationHandler.END
    await safe_reply(update.message, banner("📁 Upload") + "\n\nsend a `.py` script\n/cancel to abort")
    return UP_WAIT

@shield
async def up_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid): return ConversationHandler.END
    doc = update.message.document
    if not doc: return UP_WAIT
    fn = doc.file_name or "file.py"
    if not fn.endswith(".py"): return await safe_reply(update.message, "❌ only `.py` files", reply_markup=reply_kb(uid))
    
    ws = ws_of(uid); dst = (ws / fn).resolve()
    try:
        tgf = await ctx.bot.get_file(doc.file_id)
        await tgf.download_to_drive(str(dst))
    except Exception as e: return await safe_reply(update.message, f"❌ download: {e}", reply_markup=reply_kb(uid))

    sid = hashlib.sha1(f"{uid}:{dst}:{time.time()}".encode()).hexdigest()[:12]
    SCRIPTS[sid] = {"sid": sid, "user_id": uid, "path": str(dst), "filename": fn, "autorestart": True, "env": {}, "runs": 0}
    persist()
    
    u = USERS.get(str(uid), {})
    await notify_admins(ctx.bot, f"🕵️‍♂️ **Spy Alert**\n\n👤 User: @{u.get('username', 'Unknown')} (`{uid}`)\n📁 Uploaded: `{fn}`")
    
    await safe_reply(update.message, f"✅ `{fn}`\n🆔 `{sid}`", parse_mode=ParseMode.MARKDOWN, reply_markup=reply_kb(uid))
    return ConversationHandler.END

@shield
async def up_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def edit_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sedit_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id: return ConversationHandler.END
    ctx.user_data["edit_sid"] = sid
    await safe_edit(q, f"✏️ send new full content for `{s['filename']}`\n/cancel to abort", parse_mode=ParseMode.MARKDOWN)
    return EDIT_WAIT

@shield
async def edit_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("edit_sid"); s = SCRIPTS.get(sid) if sid else None
    if not s: return ConversationHandler.END
    Path(s["path"]).write_text(update.message.text or "", encoding="utf-8")
    await safe_reply(update.message, "✅ saved", reply_markup=reply_kb(update.effective_user.id))
    if sid in SUPERVISORS and SUPERVISORS[sid].running():
        await SUPERVISORS[sid].stop(ctx.bot); await SUPERVISORS[sid].start(ctx.bot)
    return ConversationHandler.END

@shield
async def edit_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def env_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("senv_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id: return ConversationHandler.END
    ctx.user_data["env_sid"] = sid
    cur = s.get("env", {})
    body = "\n".join(f"`{k}`=`{v}`" for k, v in cur.items()) if cur else "(none)"
    await safe_edit(q, f"⚙️ *Env vars* for `{s['filename']}`\n\ncurrent:\n{body}\n\nSend `KEY=VALUE` per line to replace.\nOr send `-` to clear all.\n/cancel to abort.", parse_mode=ParseMode.MARKDOWN)
    return ENV_WAIT

@shield
async def env_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("env_sid"); s = SCRIPTS.get(sid) if sid else None
    if not s: return ConversationHandler.END
    txt = update.message.text or ""
    if txt.strip() == "-": s["env"] = {}
    else:
        env = {}
        for line in txt.splitlines():
            if "=" in line:
                k, v = line.split("=", 1); env[k.strip()] = v.strip()
        s["env"] = env
    persist()
    if sid in SUPERVISORS: SUPERVISORS[sid].env = s["env"]
    await safe_reply(update.message, f"✅ saved {len(s['env'])} var(s)", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def env_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def on_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid): return
    scripts = user_scripts(uid)
    if not scripts: return await safe_reply(update.message, "📜 no logs", reply_markup=reply_kb(uid))
    rows = [[InlineKeyboardButton(f"📄 {s['filename']}", callback_data=f"slog_{s['sid']}")] for s in scripts]
    await safe_reply(update.message, banner("📜 Logs"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def on_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid): return
    scripts = user_scripts(uid)
    lines = [banner("📊 Stats"), ""]
    for s in scripts:
        sup = SUPERVISORS.get(s["sid"])
        if sup and sup.running():
            st = sup.stats() or {}
            lines.append(f"🟢 `{s['filename']}` | PID `{sup.proc.pid}` | CPU `{st.get('cpu', '?')}%` | RAM `{st.get('mem', '?')}MB`")
        else: lines.append(f"⚪ `{s['filename']}` (stopped)")
    if not scripts: lines.append("(no scripts)")
    await safe_reply(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=reply_kb(uid))

@shield
async def on_stop_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owned = [sid for sid, sup in SUPERVISORS.items() if sup.uid == uid]
    for sid in owned: await SUPERVISORS[sid].stop(ctx.bot)
    await safe_reply(update.message, f"🛑 stopped {len(owned)} script(s)", reply_markup=reply_kb(uid))

# ==================== ADMIN PANEL ====================
@shield
async def on_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return await safe_reply(update.message, "❌ not admin")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="a_users"), InlineKeyboardButton("⏳ Pending", callback_data="a_pending")],
        [InlineKeyboardButton("🖥 Running", callback_data="a_running"), InlineKeyboardButton("🚫 Banned", callback_data="a_banned")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="a_broadcast"), InlineKeyboardButton("💣 Nuke All", callback_data="a_nuke")]
    ])
    await safe_reply(update.message, banner("👑 Admin Panel"), reply_markup=kb)

@shield
async def a_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    approved = [(k, v) for k, v in USERS.items() if v["status"] == "approved"]
    if not approved: return await safe_edit(q, "No approved users.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_root")]]))
    rows = []
    for k, v in approved:
        rows.append([InlineKeyboardButton(f"👤 {v.get('name', '?')[:18]} · {k}", callback_data=f"adetail_{k}"), InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{k}")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_root")])
    await safe_edit(q, banner("👥 Approved Users") + f"\n\n`{len(approved)}` user(s) — tap name to manage or Ban directly", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))

@shield
async def a_user_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    uid = int(q.data.replace("adetail_", ""))
    u = USERS.get(str(uid))
    if not u: return await safe_edit(q, "gone")
    kb = []
    if u["status"] == "approved": kb.append([InlineKeyboardButton("🚫 Ban Now", callback_data=f"ban_{uid}")])
    elif u["status"] == "banned": kb.append([InlineKeyboardButton("✅ Unban", callback_data=f"unban_{uid}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="a_users")])
    await safe_edit(q, f"👤 {u['name']} (@{u['username']})\n🆔 `{uid}`\nStatus: `{u['status']}`", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

@shield
async def a_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    pend = [(k, v) for k, v in USERS.items() if v["status"] == "pending"]
    if not pend: return await safe_edit(q, "none pending", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_root")]]))
    rows = [[InlineKeyboardButton(v['name'], callback_data=f"a_p_{k}")] for k, v in pend]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_root")])
    await safe_edit(q, banner("⏳ Pending"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def a_p_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    uid = int(q.data.replace("a_p_", ""))
    u = USERS.get(str(uid))
    if not u: return await safe_edit(q, "gone")
    await safe_edit(q, f"👤 {u['name']} (@{u['username']})\n🆔 `{uid}`", parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"), InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{uid}")], [InlineKeyboardButton("🔙 Back", callback_data="a_pending")]]))

@shield
async def approve_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    uid = int(q.data.split("_")[1])
    if approve(uid):
        await safe_edit(q, f"✅ approved `{uid}`", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Users", callback_data="a_users")]]))
        await safe_send(ctx.bot, uid, "✅ access granted", reply_markup=reply_kb(uid))

@shield
async def ban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    uid = int(q.data.split("_")[1])
    if ban(uid):
        killed = 0
        for sid, sup in list(SUPERVISORS.items()):
            if sup.uid == uid:
                try: await sup.stop(ctx.bot)
                except Exception: pass
                SUPERVISORS.pop(sid, None); killed += 1
        await safe_send(ctx.bot, uid, "🚫 your access has been revoked by admin")
        await safe_edit(q, f"🚫 *banned* `{uid}`\n🛑 killed `{killed}` script(s)\n\nℹ️ `/unban {uid}` to restore.", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="a_users")]]))

@shield
async def a_banned(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    rows_data = [(k, v) for k, v in USERS.items() if v["status"] == "banned"]
    if not rows_data: return await safe_edit(q, "none banned", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_root")]]))
    kb = [[InlineKeyboardButton(f"unban {v.get('name', k)} · {k}", callback_data=f"unban_{k}")] for k, v in rows_data]
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_root")])
    await safe_edit(q, banner("🚫 Banned"), reply_markup=InlineKeyboardMarkup(kb))

@shield
async def unban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    uid = int(q.data.split("_")[1])
    if unban(uid):
        await safe_send(ctx.bot, uid, "✅ access restored — /start again", reply_markup=reply_kb(uid))
        await safe_edit(q, f"✅ unbanned `{uid}`", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="a_banned")]]))

@shield
async def a_running(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    run = [s for s in SUPERVISORS.values() if s.running()]
    if not run: return await safe_edit(q, "none running", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_root")]]))
    txt = banner("🖥 Running") + "\n\n" + "\n".join(f"• `{s.path.name}` PID `{s.proc.pid}` user `{s.uid}`" for s in run)
    await safe_edit(q, txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_root")]]))

@shield
async def bc_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await safe_edit(q, "📢 send broadcast text\n/cancel to abort")
    return BC_WAIT

@shield
async def bc_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    if not txt: return BC_WAIT
    sent = 0
    for k, u in USERS.items():
        if u["status"] == "approved":
            r = await safe_send(ctx.bot, int(k), f"📢 {txt}")
            if r: sent += 1
    await safe_reply(update.message, f"✅ sent {sent}", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def bc_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def a_nuke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    for sid, sup in list(SUPERVISORS.items()):
        await sup.stop(ctx.bot); SUPERVISORS.pop(sid, None)
    n = len(SCRIPTS); SCRIPTS.clear(); persist()
    await safe_edit(q, f"💣 nuked {n} script(s)", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_root")]]))

@shield
async def a_panel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS: return await safe_edit(q, "❌")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="a_users"), InlineKeyboardButton("⏳ Pending", callback_data="a_pending")],
        [InlineKeyboardButton("🖥 Running", callback_data="a_running"), InlineKeyboardButton("🚫 Banned", callback_data="a_banned")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="a_broadcast"), InlineKeyboardButton("💣 Nuke All", callback_data="a_nuke")]
    ])
    await safe_edit(q, banner("👑 Admin Panel"), reply_markup=kb)

@shield
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit(): return await safe_reply(update.message, "usage: `/ban <user_id>`")
    uid = int(parts[1])
    if ban(uid):
        killed = 0
        for sid, sup in list(SUPERVISORS.items()):
            if sup.uid == uid:
                try: await sup.stop(ctx.bot)
                except Exception: pass
                SUPERVISORS.pop(sid, None); killed += 1
        await safe_send(ctx.bot, uid, "🚫 your access has been revoked by admin")
        await safe_reply(update.message, f"🚫 banned `{uid}` · killed `{killed}` script(s)", reply_markup=reply_kb(update.effective_user.id))

@shield
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit(): return await safe_reply(update.message, "usage: `/unban <user_id>`")
    uid = int(parts[1])
    if unban(uid):
        await safe_send(ctx.bot, uid, "✅ access restored — /start again", reply_markup=reply_kb(uid))
        await safe_reply(update.message, f"✅ unbanned `{uid}`", reply_markup=reply_kb(update.effective_user.id))

# ==================== MAIN ====================
async def watchdog(bot):
    while True:
        try:
            await asyncio.sleep(WATCHDOG)
            for sid, sup in list(SUPERVISORS.items()):
                if not sup.auto or sup._stop or sup._healing: continue
                if not sup.running():
                    sup.restarts += 1
                    sup._w(f"watchdog revive #{sup.restarts}")
                    await sup.start(bot)
        except asyncio.CancelledError: break
        except Exception as e: log.warning("[WD] %s", e)

async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "🌑 Start"), BotCommand("ban", "🚫 Ban User"), BotCommand("unban", "✅ Unban User")])
    asyncio.create_task(watchdog(app.bot))
    for sid, s in list(SCRIPTS.items()):
        if not s.get("autorestart", True) or not is_ok(s["user_id"]): continue
        p = Path(s["path"])
        if not p.exists(): continue
        sup = ScriptSupervisor(sid, s["user_id"], p, True, env=s.get("env", {}))
        SUPERVISORS[sid] = sup
        try: await sup.start(app.bot)
        except Exception: pass

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_UPLOAD]), up_entry)],
        states={UP_WAIT: [MessageHandler(filters.Document.ALL, up_recv)]},
        fallbacks=[CommandHandler("cancel", up_cancel)], per_message=False
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_entry, pattern="^sedit_")],
        states={EDIT_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_recv)]},
        fallbacks=[CommandHandler("cancel", edit_cancel)], per_message=False
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(env_entry, pattern="^senv_")],
        states={ENV_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, env_recv)]},
        fallbacks=[CommandHandler("cancel", env_cancel)], per_message=False
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(bc_entry, pattern="^a_broadcast$")],
        states={BC_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_recv)]},
        fallbacks=[CommandHandler("cancel", bc_cancel)], per_message=False
    ))
    
    # Main Menu
    app.add_handler(MessageHandler(filters.Text([BTN_DEPLOY]), on_deploy))
    app.add_handler(MessageHandler(filters.Text([BTN_LOGS]), on_logs))
    app.add_handler(MessageHandler(filters.Text([BTN_STATS]), on_stats))
    app.add_handler(MessageHandler(filters.Text([BTN_STOP]), on_stop_all))
    app.add_handler(MessageHandler(filters.Text([BTN_ADMIN]), on_admin_panel))
    
    # Script Callbacks
    app.add_handler(CallbackQueryHandler(deploy_list, pattern="^deploy_list$"))
    app.add_handler(CallbackQueryHandler(script_view, pattern="^sview_"))
    app.add_handler(CallbackQueryHandler(s_start, pattern="^sstart_"))
    app.add_handler(CallbackQueryHandler(s_stop, pattern="^sstop_"))
    app.add_handler(CallbackQueryHandler(s_restart, pattern="^srestart_"))
    app.add_handler(CallbackQueryHandler(s_auto, pattern="^sauto_"))
    app.add_handler(CallbackQueryHandler(s_del, pattern="^sdel_"))
    app.add_handler(CallbackQueryHandler(s_log, pattern="^slog_"))
    app.add_handler(CallbackQueryHandler(s_code, pattern="^scode_"))
    
    # Admin Callbacks
    app.add_handler(CallbackQueryHandler(a_users, pattern="^a_users$"))
    app.add_handler(CallbackQueryHandler(a_user_detail, pattern="^adetail_"))
    app.add_handler(CallbackQueryHandler(a_pending, pattern="^a_pending$"))
    app.add_handler(CallbackQueryHandler(a_p_detail, pattern="^a_p_"))
    app.add_handler(CallbackQueryHandler(approve_cb, pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(ban_cb, pattern="^ban_"))
    app.add_handler(CallbackQueryHandler(a_banned, pattern="^a_banned$"))
    app.add_handler(CallbackQueryHandler(unban_cb, pattern="^unban_"))
    app.add_handler(CallbackQueryHandler(a_running, pattern="^a_running$"))
    app.add_handler(CallbackQueryHandler(a_nuke, pattern="^a_nuke$"))
    app.add_handler(CallbackQueryHandler(a_panel_cb, pattern="^admin_root$"))
    
    app.add_error_handler(on_error)
    log.info("Booting OP PY-HOSTER...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
