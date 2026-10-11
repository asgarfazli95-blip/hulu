#!/usr/bin/env python3
"""
𝐒𝐇𝐀𝐃𝐎𝐖 𝐇𝐎𝐒𝐓𝐈𝐍𝐆 v9
- Resilience layer: safe_reply / safe_edit / safe_send / shield / on_error
- Terminal escape: menu button during TERM_SESSION exits cleanly + dispatches
- UTF-8 forced on Windows (cp1252 fix)
- env vars · live stdin · scheduler · file manager
- auto-restore on boot · crash notify · self-restart with backoff
- NO token fallback — BOT_TOKEN must come from env
"""

# ---- UTF-8 force on Windows ----
import os, sys
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONLEGACYWINDOWSSTDIO", "0")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def _boot():
    need = []
    for mod, pkg in [("telegram", "python-telegram-bot"), ("dotenv", "python-dotenv"),
                     ("psutil", "psutil"), ("flask", "flask")]:
        try: __import__(mod)
        except ImportError: need.append(pkg)
    if need:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", *need])
try:
    _boot()
except Exception as e:
    print(f"[SHADOW] boot deps failed: {e}", file=sys.stderr)

import asyncio, functools, hashlib, json, logging, re, shlex, shutil, time, traceback, zipfile
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Dict, Optional

import psutil
from dotenv import load_dotenv
from flask import Flask, jsonify
from telegram import (ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update, BotCommand)
from telegram.constants import ParseMode
from telegram.error import (BadRequest, Forbidden, RetryAfter, TimedOut,
                            NetworkError, TelegramError, Conflict)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ConversationHandler, MessageHandler, filters, ContextTypes)

# ==================== LOGGING ====================
log = logging.getLogger("shadow")
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
        try:
            return await coro_factory()
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            last = e
        except RETRYABLE as e:
            await asyncio.sleep(RETRY_BASE ** attempt)
            last = e
    if last: raise last

async def safe_reply(message, text, **kwargs):
    if message is None: return None
    async def _do():
        try:
            return await message.reply_text(text, **kwargs)
        except BadRequest as e:
            s = str(e).lower()
            if "not found" in s or "reply" in s or "message to be" in s:
                return await message.get_bot().send_message(
                    chat_id=message.chat_id, text=text, **kwargs)
            if "parse" in s or "entities" in s:
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await message.get_bot().send_message(
                    chat_id=message.chat_id, text=text, **k)
            raise
        except Forbidden:
            return None
    try:
        return await _retry(_do)
    except TelegramError as e:
        log.warning("safe_reply gave up: %s", e)
        return None

async def safe_edit(query, text, **kwargs):
    if query is None: return None
    async def _do():
        try:
            return await query.edit_message_text(text, **kwargs)
        except BadRequest as e:
            s = str(e).lower()
            if "not modified" in s:
                return None
            if "not found" in s or "message to edit" in s:
                return await query.get_bot().send_message(
                    chat_id=query.from_user.id, text=text, **kwargs)
            if "parse" in s or "entities" in s:
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await query.get_bot().send_message(
                    chat_id=query.from_user.id, text=text, **k)
            raise
    try:
        return await _retry(_do)
    except TelegramError as e:
        log.warning("safe_edit gave up: %s", e)
        return None

async def safe_send(bot, chat_id, text, **kwargs):
    async def _do():
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except BadRequest as e:
            if "parse" in str(e).lower() or "entities" in str(e).lower():
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await bot.send_message(chat_id=chat_id, text=text, **k)
            raise
        except Forbidden:
            return None
    try:
        return await _retry(_do)
    except TelegramError as e:
        log.warning("safe_send gave up: %s", e)
        return None

async def _nudge(update, context):
    try:
        if update is not None and getattr(update, "callback_query", None):
            try: await update.callback_query.answer()
            except Exception: pass
        chat_id = None
        if update is not None and getattr(update, "effective_chat", None):
            chat_id = update.effective_chat.id
        elif (update is not None and getattr(update, "callback_query", None)
              and update.callback_query.from_user):
            chat_id = update.callback_query.from_user.id
        if chat_id is None: return
        await context.bot.send_message(chat_id=chat_id,
            text="⚠️ that action hiccuped — try once more")
    except Exception:
        pass

def shield(fn):
    @functools.wraps(fn)
    async def _wrap(update, context, *a, **kw):
        try:
            return await fn(update, context, *a, **kw)
        except (BadRequest, Forbidden) as e:
            log.warning("shield[%s] soft: %s", fn.__name__, e)
            await _nudge(update, context)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try: return await fn(update, context, *a, **kw)
            except Exception as e2:
                log.warning("shield[%s] retry-fail: %s", fn.__name__, e2)
        except Conflict as e:
            log.error("TOKEN CONFLICT — another instance is polling: %s", e)
        except Exception as e:
            log.exception("shield[%s] hard: %s", fn.__name__, e)
            await _nudge(update, context)
    return _wrap

async def on_error(update, context):
    err = context.error
    if isinstance(err, Conflict):
        log.error("POLLING CONFLICT — same BOT_TOKEN elsewhere: %s", err); return
    if isinstance(err, RetryAfter):
        await asyncio.sleep(float(err.retry_after) + 0.5); return
    if isinstance(err, (BadRequest, Forbidden)):
        log.warning("error handler soft: %s", err); return
    log.exception("error handler:", exc_info=err)
    await _nudge(update, context)

# ==================== CONFIG ====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing — set it in env / .env file")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
CHANNEL    = os.getenv("CHANNEL_USERNAME", "").strip()
PORT       = int(os.getenv("PORT", "8080"))
WORKSPACE  = Path(os.getenv("WORKSPACE_BASE", "./workspaces")).resolve()
MAX_UP     = int(os.getenv("MAX_UPLOAD_SIZE", "50").replace("MB", "")) * 1024 * 1024
MAX_ZIP    = int(os.getenv("MAX_ARCHIVE_SIZE", "200").replace("MB", "")) * 1024 * 1024
MAX_SCRIPTS= int(os.getenv("MAX_SCRIPTS_PER_USER", "20"))
LOG_CAP    = int(os.getenv("LOG_CAP_MB", "10")) * 1024 * 1024
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
WATCHDOG   = int(os.getenv("WATCHDOG_INTERVAL", "5"))
AUTO_RT    = os.getenv("AUTO_INSTALL_RUNTIMES", "1") == "1"
IS_WIN     = os.name == "nt"
IS_ROOT    = hasattr(os, "geteuid") and os.geteuid() == 0

WORKSPACE.mkdir(parents=True, exist_ok=True)
DATA_FILE = WORKSPACE / "shadow_data.json"

# ==================== FONT / STYLE ====================
def fancy(s):
    o = []
    for c in s:
        n = ord(c)
        if 65 <= n <= 90: o.append(chr(0x1D400 + n - 65))
        elif 97 <= n <= 122: o.append(chr(0x1D41A + n - 97))
        elif 48 <= n <= 57: o.append(chr(0x1D7CE + n - 48))
        else: o.append(c)
    return "".join(o)

BRAND = fancy("Shadow Hosting")
SEP = "━" * 22

def banner(title: str = "") -> str:
    head = f"🌑 · {BRAND} · 🌑"
    return f"{head}\n{SEP}\n{title}" if title else f"{head}\n{SEP}"

def divider() -> str:
    return "· · · · · · · · · · · · · · · ·"

# ==================== DATA ====================
def _load():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {}

D = _load()
for k in ("users", "scripts", "installed"): D.setdefault(k, {})
USERS = D["users"]; SCRIPTS = D["scripts"]; INSTALLED = D["installed"]

def persist():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(D, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        log.warning("persist: %s", e)

def mark(k, n): INSTALLED[f"{k}:{n}"] = True; persist()
def has(k, n):  return INSTALLED.get(f"{k}:{n}", False)

# ==================== LANGS ====================
LANGS = {
    ".py":  ("python",     lambda p: [sys.executable, "-u", str(p)]),
    ".js":  ("node",       lambda p: ["node", str(p)]),
    ".mjs": ("node",       lambda p: ["node", str(p)]),
    ".cjs": ("node",       lambda p: ["node", str(p)]),
    ".ts":  ("tsx",        lambda p: ["npx", "tsx", str(p)]),
    ".sh":  ("bash",       lambda p: ["bash", str(p)]),
    ".bash":("bash",       lambda p: ["bash", str(p)]),
    ".php": ("php",        lambda p: ["php", str(p)]),
    ".rb":  ("ruby",       lambda p: ["ruby", str(p)]),
    ".go":  ("go",         lambda p: ["go", "run", str(p)]),
    ".lua": ("lua",        lambda p: ["lua", str(p)]),
    ".pl":  ("perl",       lambda p: ["perl", str(p)]),
    ".ps1": ("powershell", lambda p: ["powershell", "-File", str(p)]),
}
def lang_of(p): return LANGS.get(Path(p).suffix.lower(), (None, None))[0]

RUNTIME_PKGS = {
    "node":       ("nodejs npm", ["node", "npm"]),
    "tsx":        ("nodejs npm", ["node", "npm"]),
    "php":        ("php-cli php", ["php"]),
    "ruby":       ("ruby", ["ruby"]),
    "go":         ("golang", ["go"]),
    "lua":        ("lua5.4 lua5.3 lua", ["lua"]),
    "perl":       ("perl", ["perl"]),
    "bash":       ("bash", ["bash"]),
    "powershell": ("powershell", ["powershell", "pwsh"]),
}

def which_any(names):
    for n in names:
        p = shutil.which(n)
        if p: return p
    return None

async def run_cmd(cmd, timeout=600):
    env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUTF8"] = "1"
    try:
        proc = await asyncio.create_subprocess_exec(*cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="replace")
    except asyncio.TimeoutError:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)

async def install_runtime(lang):
    if lang not in RUNTIME_PKGS: return False, "no installer"
    if has("rt", lang): return True, "cached"
    pkgs, checks = RUNTIME_PKGS[lang]
    if which_any(checks): mark("rt", lang); return True, "present"
    if not AUTO_RT: return False, "auto-off"
    pkgs_list = pkgs.split()
    sudo = ["sudo"] if (not IS_WIN and not IS_ROOT and shutil.which("sudo")) else []
    cmds = []
    if   shutil.which("apt-get"): cmds = [[*sudo, "apt-get", "update"], [*sudo, "apt-get", "install", "-y", *pkgs_list]]
    elif shutil.which("apk"):     cmds = [["apk", "add", "--no-cache", *pkgs_list]]
    elif shutil.which("dnf"):     cmds = [["dnf", "install", "-y", *pkgs_list]]
    elif shutil.which("yum"):     cmds = [["yum", "install", "-y", *pkgs_list]]
    elif shutil.which("pacman"):  cmds = [["pacman", "-Sy", "--noconfirm", *pkgs_list]]
    elif shutil.which("brew"):    cmds = [["brew", "install", *pkgs_list]]
    elif shutil.which("choco"):   cmds = [["choco", "install", "-y", *pkgs_list]]
    else: return False, f"manual: {pkgs}"
    out_log = []
    for c in cmds:
        rc, out = await run_cmd(c)
        out_log.append(f"$ {' '.join(c)}\n{out[-250:]}")
        if rc != 0: return False, "\n".join(out_log)[-500:]
    if which_any(checks): mark("rt", lang); return True, "installed"
    return False, "\n".join(out_log)[-500:]

# ==================== ERROR PARSER ====================
PARSERS = [
    ("pip",   [r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]",
               r"ImportError: No module named ['\"]([^'\"]+)['\"]"]),
    ("npm",   [r"Cannot find module ['\"]([^'\"]+)['\"]"]),
    ("gem",   [r"cannot load such file -- ([^\s]+)"]),
    ("php",   [r"Class ['\"]([^'\"\\]+)['\"] not found"]),
    ("go",    [r"no required module provides package ([^\s;]+)"]),
    ("shell", [r"([\w\.\-]+): command not found",
               r"([\w\.\-]+) is not recognized as an internal"]),
]

def parse_err(t):
    for kind, pats in PARSERS:
        for p in pats:
            m = re.search(p, t)
            if m:
                name = m.group(1).split(".")[0]
                if kind == "shell" and name in {"python", "python3", "pip", "node", "npm"}:
                    continue
                return kind, name
    return None, None

async def install_piece(kind, name, cwd: Path):
    if has(kind, name): return True
    sudo = ["sudo"] if (not IS_WIN and not IS_ROOT and shutil.which("sudo")) else []
    cmd = None
    if   kind == "pip":   cmd = [sys.executable, "-m", "pip", "install", "--user", name]
    elif kind == "npm":   cmd = ["npm", "install", name]
    elif kind == "gem":   cmd = ["gem", "install", name]
    elif kind == "go":    cmd = ["go", "get", name]
    elif kind == "php":   cmd = [*sudo, "apt-get", "install", "-y", name]
    elif kind == "shell": cmd = [*sudo, "apt-get", "install", "-y", name]
    if not cmd: return False
    rc, _ = await run_cmd(cmd)
    if rc != 0 and kind == "npm":
        rc, _ = await run_cmd(["npm", "install", "-g", name])
    if rc == 0:
        mark(kind, name); return True
    return False

async def ai_fix(err):
    if not OPENAI_KEY: return ""
    try:
        import openai
        openai.api_key = OPENAI_KEY
        r = await asyncio.to_thread(openai.ChatCompletion.create,
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": "Reply 'FIX: <one-line fix>'"},
                      {"role": "user", "content": err[:1500]}],
            max_tokens=100)
        return r.choices[0].message.content.strip()
    except Exception as e:
        return f"(ai: {e})"

# ==================== SUPERVISOR ====================
SUPERVISORS: Dict[str, "ScriptSupervisor"] = {}

class ScriptSupervisor:
    def __init__(self, sid, uid, path, lang, auto=True, env=None):
        self.sid = sid; self.uid = uid
        self.path = Path(path).resolve()
        self.lang = lang; self.auto = auto
        self.env = env or {}
        self.log = self.path.parent / "logs" / f"{sid}.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.proc = None; self._task = None; self._stop = False
        self.restarts = 0; self.backoff = 1; self._healing = False
        self.started = None; self.last_exit = None; self.last_crash = ""

    def running(self): return self.proc is not None and self.proc.returncode is None

    def _w(self, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.log.exists() and self.log.stat().st_size > LOG_CAP:
                self.log.write_text("", encoding="utf-8")
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [SHADOW] {msg}\n")
        except Exception:
            pass

    def stats(self):
        if not self.running(): return None
        try:
            p = psutil.Process(self.proc.pid)
            with p.oneshot():
                return {
                    "cpu": round(p.cpu_percent(interval=0), 1),
                    "mem": round(p.memory_info().rss / 1024 / 1024, 1),
                    "uptime": int(time.time() - self.started.timestamp()) if self.started else 0,
                }
        except Exception:
            return None

    def send_input(self, text: str) -> bool:
        if not self.running() or not self.proc.stdin: return False
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8"))
            return True
        except Exception:
            return False

    async def _ensure_rt(self, bot=None):
        e = RUNTIME_PKGS.get(self.lang)
        if not e or which_any(e[1]): return True
        if bot:
            await safe_send(bot, self.uid, f"⚙️ installing runtime *{self.lang}*…",
                            parse_mode=ParseMode.MARKDOWN)
        ok, info = await install_runtime(self.lang)
        self._w(f"rt {self.lang}: {ok} {info[:200]}")
        return ok

    async def start(self, bot=None):
        if self.running(): return
        self._stop = False
        await self._ensure_rt(bot)
        _, builder = LANGS.get(self.path.suffix.lower(), (None, None))
        if not builder:
            self._w(f"unsupported {self.path.suffix}"); return
        cmd = builder(self.path)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONLEGACYWINDOWSSTDIO"] = "0"
        env["NODE_ENV"] = env.get("NODE_ENV", "production")
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        env.update(self.env)

        try:
            self._w(f"launching {self.path}")
            self.proc = await asyncio.create_subprocess_exec(*cmd,
                cwd=str(self.path.parent),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env)
            self.started = datetime.now()
            self._w(f"PID {self.proc.pid}")
            self._task = asyncio.create_task(self._pump(bot))
        except FileNotFoundError as e:
            self._w(f"rt missing: {e}")
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
        except Exception as e:
            self._w(f"pump: {e}")
        finally:
            if self.proc:
                await self.proc.wait()
                self.last_exit = self.proc.returncode
                self._w(f"exited {self.last_exit}")
            self.last_crash = "".join(tail[-80:])
            if self.auto and not self._stop and bot:
                await self._heal(bot, self.last_crash)

    async def _heal(self, bot, tail):
        if self._healing: return
        self._healing = True
        try:
            kind, name = parse_err(tail); note = ""
            if kind and await install_piece(kind, name, self.path.parent):
                note = f"📦 {kind} `{name}` installed"
            if not note and OPENAI_KEY:
                s = await ai_fix(tail)
                if s.startswith("FIX:"): note = f"🤖 {s[4:].strip()}"
            if note:
                await safe_send(bot, self.uid,
                    f"🩹 *auto-heal* · `{self.path.name}`\n{note}\n♻️ restarting…",
                    parse_mode=ParseMode.MARKDOWN)
            else:
                snippet = tail[-400:]
                await safe_send(bot, self.uid,
                    f"💥 *crash* · `{self.path.name}`\n```\n{snippet}\n```\n♻️ restarting…",
                    parse_mode=ParseMode.MARKDOWN)
            self.restarts += 1
            await asyncio.sleep(min(self.backoff, 60))
            self.backoff = min(self.backoff * 2, 60)
            await self.start(bot)
        finally:
            self._healing = False

    async def stop(self):
        self._stop = True
        if self._task and not self._task.done(): self._task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                try: await asyncio.wait_for(self.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.proc.kill(); await self.proc.wait()
            except ProcessLookupError:
                pass
        self._w("stopped")
        self.proc = None

def sid_for(uid, p): return hashlib.sha1(f"{uid}:{p}:{time.time()}".encode()).hexdigest()[:12]

# ==================== SCHEDULER ====================
async def scheduler_loop():
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            for sid, s in list(SCRIPTS.items()):
                iv = s.get("interval")
                if not iv: continue
                last = s.get("last_run", 0)
                if now - last < iv: continue
                s["last_run"] = now; persist()
                p = Path(s["path"]).resolve()
                if not p.exists(): continue
                _, builder = LANGS.get(p.suffix.lower(), (None, None))
                if not builder: continue
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUTF8"] = "1"
                env.update(s.get("env", {}))
                try:
                    proc = await asyncio.create_subprocess_exec(*builder(p),
                        cwd=str(p.parent),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT, env=env)
                    lg = p.parent / "logs" / f"{sid}.log"
                    lg.parent.mkdir(parents=True, exist_ok=True)
                    with open(lg, "a", encoding="utf-8") as f:
                        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [SCHED] fired\n")
                        try:
                            out, _ = await asyncio.wait_for(proc.communicate(), timeout=iv - 1)
                            f.write(out.decode(errors="replace")[-2000:])
                        except asyncio.TimeoutError:
                            proc.kill(); f.write("[SCHED] timed out\n")
                except Exception as e:
                    log.warning("[sched] %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("[sched loop] %s", e)

# ==================== USER / SCRIPT ====================
def get_user(uid): return USERS.get(str(uid))

def add_user(uid, name, uname):
    USERS[str(uid)] = {"status": "pending", "name": name, "username": uname,
                       "request_time": datetime.now().isoformat()}
    persist()

def approve(uid):
    u = USERS.get(str(uid))
    if u: u["status"] = "approved"; persist(); return True
    return False

def ban(uid):
    u = USERS.get(str(uid))
    if u: u["status"] = "banned"; persist(); return True
    return False

def unban(uid): return approve(uid)

def is_ok(uid):
    u = get_user(uid); return bool(u and u["status"] == "approved")
def is_pend(uid):
    u = get_user(uid); return bool(u and u["status"] == "pending")
def is_ban(uid):
    u = get_user(uid); return bool(u and u["status"] == "banned")
def user_scripts(uid): return [s for s in SCRIPTS.values() if s["user_id"] == uid]

def ws_of(uid):
    w = WORKSPACE / str(uid); w.mkdir(parents=True, exist_ok=True); return w

def safe_name(fn):
    n = os.path.basename(fn).replace("/", "").replace("\\", "")
    return re.sub(r"[^\w\.\-]", "_", n) or "file"

def inside(uid, p):
    try:
        Path(p).resolve().relative_to(ws_of(uid)); return True
    except ValueError:
        return False

def unzip(z, dest):
    try:
        with zipfile.ZipFile(z) as zf:
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_ZIP: return False, "too large"
            for m in zf.infolist():
                if m.filename.startswith("/") or ".." in m.filename:
                    return False, "bad path"
                t = dest / m.filename
                try: t.resolve().relative_to(Path(dest).resolve())
                except ValueError: return False, "traversal"
            zf.extractall(dest); return True, "ok"
    except Exception as e:
        return False, f"unzip: {e}"

def find_entry(d):
    for n in ("main.py", "app.py", "bot.py", "index.js", "server.js", "index.mjs",
              "index.ts", "main.go", "index.php", "app.rb", "main.lua", "run.sh"):
        for p in d.rglob(n):
            if lang_of(p): return lang_of(p), p
    for ext in LANGS:
        fs = list(d.rglob(f"*{ext}"))
        if fs: return LANGS[ext][0], fs[0]
    return None, None

async def install_manifest(d):
    out = []; ok = True
    if (d / "requirements.txt").exists():
        rc, _ = await run_cmd([sys.executable, "-m", "pip", "install", "-r", str(d / "requirements.txt")])
        out.append(f"pip {'ok' if rc == 0 else 'fail'}"); ok = ok and rc == 0
    if (d / "package.json").exists():
        rc, _ = await run_cmd(["npm", "install", "--production"])
        out.append(f"npm {'ok' if rc == 0 else 'fail'}"); ok = ok and rc == 0
    if (d / "go.mod").exists():
        rc, _ = await run_cmd(["go", "mod", "download"])
        out.append(f"go {'ok' if rc == 0 else 'fail'}"); ok = ok and rc == 0
    return ok, "\n".join(out) if out else "no manifest"

# ==================== BUTTONS ====================
BTN_DEPLOY = "🚀 Deploy"
BTN_UPLOAD = "📁 Upload"
BTN_TERM   = "💻 Terminal"
BTN_LOGS   = "📜 Logs"
BTN_STATS  = "📊 Stats"
BTN_STOP   = "🛑 Stop All"
BTN_FILES  = "📂 Files"
BTN_ADMIN  = "👑 Admin Panel"
BTN_TEXTS  = {BTN_DEPLOY, BTN_UPLOAD, BTN_TERM, BTN_LOGS,
              BTN_STATS, BTN_STOP, BTN_FILES, BTN_ADMIN}

def reply_kb(uid):
    rows = [[BTN_DEPLOY, BTN_UPLOAD], [BTN_TERM, BTN_FILES],
            [BTN_LOGS, BTN_STATS], [BTN_STOP]]
    if uid in ADMIN_IDS: rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup([[KeyboardButton(b) for b in r] for r in rows],
                               resize_keyboard=True, is_persistent=True)

def back(t="main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=t)]])

# ==================== START ====================
@shield
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    uid, name, uname = u.id, u.full_name, u.username or "no_username"
    if CHANNEL:
        try:
            m = await ctx.bot.get_chat_member(CHANNEL, uid)
            if m.status in ("left", "kicked"):
                await safe_reply(update.message, f"🔒 join {CHANNEL}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📢 Join", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
                        [InlineKeyboardButton("✅ Verify", callback_data="check_join")]]))
                return
        except Exception as e:
            log.warning("channel check: %s", e)
            await safe_reply(update.message, "⚠️ verify failed"); return
    if not get_user(uid):
        add_user(uid, name, uname)
        for aid in ADMIN_IDS:
            await safe_send(ctx.bot, aid,
                f"🔔 *New Request*\n👤 {name}\n🆔 `{uid}`\n📛 @{uname}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
                    InlineKeyboardButton("🚫 Ban",     callback_data=f"ban_{uid}")]]))
        await safe_reply(update.message, banner("✅ request sent to admin")); return
    if is_ban(uid):  await safe_reply(update.message, "🚫 banned"); return
    if is_pend(uid): await safe_reply(update.message, "⏳ pending"); return
    await safe_reply(update.message, banner(f"👋 welcome {name}"), reply_markup=reply_kb(uid))

@shield
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message,
        banner("ℹ️ Help") + "\n\n"
        "🚀  Deploy      — list & run uploaded scripts\n"
        "📁  Upload      — .py .js .ts .sh .php .rb .go .lua .pl .ps1 .zip\n"
        "💻  Terminal    — sandboxed shell\n"
        "📜  Logs        — per-script tail\n"
        "📊  Stats       — CPU/RAM/uptime per script\n"
        "📂  Files       — browse workspace\n"
        "🛑  Stop All    — kill everything you own\n\n"
        + divider() + "\n"
        "Missing runtimes/deps install themselves on first crash.",
        reply_markup=reply_kb(update.effective_user.id))

@shield
async def check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    if CHANNEL:
        try:
            m = await ctx.bot.get_chat_member(CHANNEL, uid)
            if m.status in ("left", "kicked"):
                await safe_edit(q, "❌ not joined"); return
        except Exception:
            await safe_edit(q, "⚠️ err"); return
    if not get_user(uid):
        add_user(uid, q.from_user.full_name, q.from_user.username or "no_username")
        await safe_edit(q, "✅ request sent"); return
    if is_ban(uid):  await safe_edit(q, "🚫"); return
    if is_pend(uid): await safe_edit(q, "⏳"); return
    await safe_edit(q, banner("📋 menu"), reply_markup=back())

@shield
async def main_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await safe_edit(q, banner("📋 menu"), reply_markup=back())

# ==================== DEPLOY ====================
async def _deploy_rows(uid):
    rows = []
    for s in user_scripts(uid):
        sup = SUPERVISORS.get(s["sid"])
        dot = "🟢" if (sup and sup.running()) else "⚪"
        rows.append([InlineKeyboardButton(f"{dot}  {s['filename']} · {s['lang']}",
                                          callback_data=f"sview_{s['sid']}")])
    return rows

@shield
async def on_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        await safe_reply(update.message, "❌ not approved", reply_markup=reply_kb(uid)); return
    rows = await _deploy_rows(uid)
    if not rows:
        await safe_reply(update.message, banner("📂 no scripts") + "\n\nupload one first",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Upload", callback_data="upload")]]))
        return
    await safe_reply(update.message, banner("🚀 Deploy"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def deploy_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rows = await _deploy_rows(q.from_user.id)
    if not rows:
        await safe_edit(q, "no scripts"); return
    await safe_edit(q, banner("🚀 Deploy"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def script_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sview_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌ not found"); return
    sup = SUPERVISORS.get(sid)
    run = sup.running() if sup else False
    pid = sup.proc.pid if sup and run else "—"
    auto = "♻️ ON" if s.get("autorestart", True) else "⛔ OFF"
    st = sup.stats() if sup else None
    stats_line = f"\n📈 CPU `{st['cpu']}%` · RAM `{st['mem']}MB` · up `{st['uptime']}s`" if st else ""
    iv = s.get("interval")
    sched_line = f"\n⏰ every `{iv}s`" if iv else ""
    txt = (banner(f"📄 {s['filename']}") + "\n\n"
           f"🆔 `{sid}`\n🧬 `{s['lang']}`\n"
           f"📊 {'🟢 RUNNING' if run else '⚪ STOPPED'}\n🔢 PID `{pid}`\n"
           f"♻️ Auto: {auto}\n🔄 Restarts: {sup.restarts if sup else 0}"
           f"{sched_line}{stats_line}\n"
           f"📁 `{s['path']}`")
    kb = []
    if run:
        kb.append([InlineKeyboardButton("🔄 Restart", callback_data=f"srestart_{sid}"),
                   InlineKeyboardButton("🛑 Stop",    callback_data=f"sstop_{sid}"),
                   InlineKeyboardButton("📥 Input",   callback_data=f"sinput_{sid}")])
    else:
        kb.append([InlineKeyboardButton("🚀 Start", callback_data=f"sstart_{sid}")])
    kb.append([InlineKeyboardButton("📜 Live Log", callback_data=f"slog_{sid}"),
               InlineKeyboardButton("👁 Code",     callback_data=f"scode_{sid}")])
    kb.append([InlineKeyboardButton("✏️ Edit",   callback_data=f"sedit_{sid}"),
               InlineKeyboardButton("⚙️ Env",    callback_data=f"senv_{sid}")])
    kb.append([InlineKeyboardButton("⏰ Schedule", callback_data=f"ssch_{sid}"),
               InlineKeyboardButton(f"♻️ {auto}", callback_data=f"sauto_{sid}")])
    kb.append([InlineKeyboardButton("🗑 Delete", callback_data=f"sdel_{sid}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="deploy_list")])
    await safe_edit(q, txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def _launch(uid, sid, bot):
    s = SCRIPTS.get(sid)
    if not s: return "❌ not found"
    p = Path(s["path"]).resolve()
    if not p.exists(): return f"❌ file missing: `{p}`"
    if sid not in SUPERVISORS:
        SUPERVISORS[sid] = ScriptSupervisor(sid, uid, p, s["lang"],
                                            s.get("autorestart", True),
                                            env=s.get("env", {}))
    sup = SUPERVISORS[sid]
    if sup.running(): return "🟢 already running"
    s["runs"] = s.get("runs", 0) + 1; persist()
    await sup.start(bot)
    if sup.running(): return f"🚀 launched · PID `{sup.proc.pid}`"
    tail = ""
    try:
        if sup.log.exists():
            tail = sup.log.read_text(encoding="utf-8", errors="replace")[-500:]
    except Exception:
        pass
    return f"❌ failed\n```\n{tail}\n```"

@shield
async def script_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sstart_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return
    await safe_edit(q, "⏳ starting…")
    msg = await _launch(q.from_user.id, sid, ctx.bot)
    await safe_edit(q, f"{msg}\n🆔 `{sid}`", parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"sview_{sid}")],
            [InlineKeyboardButton("📜 Log",     callback_data=f"slog_{sid}")]]))

@shield
async def script_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sstop_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return
    if sup := SUPERVISORS.get(sid): await sup.stop()
    await safe_edit(q, f"🛑 stopped `{sid}`", parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"sview_{sid}")]]))

@shield
async def script_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("srestart_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return
    if sup := SUPERVISORS.get(sid): await sup.stop()
    await safe_edit(q, "🔄 restarting…")
    msg = await _launch(q.from_user.id, sid, ctx.bot)
    await safe_edit(q, f"{msg}\n🆔 `{sid}`", parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"sview_{sid}")]]))

@shield
async def script_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sauto_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return
    s["autorestart"] = not s.get("autorestart", True); persist()
    if sup := SUPERVISORS.get(sid): sup.auto = s["autorestart"]
    await script_view(update, ctx)

@shield
async def script_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sdel_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return
    if sup := SUPERVISORS.pop(sid, None): await sup.stop()
    try: Path(s["path"]).unlink()
    except Exception: pass
    SCRIPTS.pop(sid, None); persist()
    await safe_edit(q, "🗑 deleted", reply_markup=back("deploy_list"))

@shield
async def script_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("slog_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return
    sup = SUPERVISORS.get(sid)
    lp = sup.log if sup else Path(s["path"]).parent / "logs" / f"{sid}.log"
    try:
        tail = lp.read_text(encoding="utf-8", errors="replace")[-3500:] if lp.exists() else "(no log yet)"
    except Exception as e:
        tail = f"err: {e}"
    await safe_edit(q, f"📜 *{s['filename']}*\n\n```\n{tail}\n```",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"slog_{sid}")],
            [InlineKeyboardButton("🔙 Back",     callback_data=f"sview_{sid}")]]))

@shield
async def script_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("scode_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return
    try:
        p = Path(s["path"]); c = p.read_text(encoding="utf-8", errors="replace")
        if len(c) > 3500: c = c[:3500] + "\n…"
        await safe_edit(q, f"👁 `{p.name}`\n\n```\n{c}\n```", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"sview_{sid}")]]))
    except Exception as e:
        await safe_edit(q, f"❌ {e}")

# ==================== CONVERSATIONS ====================
(UP_WAIT, TERM_SESSION, EDIT_WAIT, BC_WAIT, ENV_WAIT, INPUT_WAIT, SCH_WAIT) = range(7)

# --- Upload ---
@shield
async def up_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        await safe_reply(update.message, "❌ not approved", reply_markup=reply_kb(uid))
        return ConversationHandler.END
    if len(user_scripts(uid)) >= MAX_SCRIPTS:
        await safe_reply(update.message, f"❌ quota reached ({MAX_SCRIPTS})", reply_markup=reply_kb(uid))
        return ConversationHandler.END
    await safe_reply(update.message, banner("📁 Upload") + "\n\nsend a script or .zip\n/cancel to abort")
    return UP_WAIT

async def _store_script(uid, path: Path, lang, filename):
    sid = sid_for(uid, path)
    SCRIPTS[sid] = {"sid": sid, "user_id": uid, "path": str(Path(path).resolve()),
                    "filename": filename, "lang": lang, "autorestart": True,
                    "created": datetime.now().isoformat(), "runs": 0,
                    "env": {}, "interval": None}
    persist()
    return sid

@shield
async def up_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        await safe_reply(update.message, "❌ send a file"); return UP_WAIT
    fn = doc.file_name or "file.bin"
    ext = Path(fn).suffix.lower()
    if ext not in LANGS and ext != ".zip":
        await safe_reply(update.message, f"❌ `{ext}` not supported", parse_mode=ParseMode.MARKDOWN,
                         reply_markup=reply_kb(uid))
        return ConversationHandler.END
    if doc.file_size and doc.file_size > MAX_UP:
        await safe_reply(update.message, "❌ too large", reply_markup=reply_kb(uid))
        return ConversationHandler.END
    ws = ws_of(uid); safe = safe_name(fn); dst = (ws / safe).resolve()
    try:
        tgf = await ctx.bot.get_file(doc.file_id)
        await tgf.download_to_drive(str(dst))
    except Exception as e:
        await safe_reply(update.message, f"❌ download: {e}", reply_markup=reply_kb(uid))
        return ConversationHandler.END

    if ext == ".zip":
        ed = (ws / f"extracted_{int(time.time())}").resolve(); ed.mkdir(exist_ok=True)
        ok, msg = unzip(dst, ed)
        if not ok:
            await safe_reply(update.message, f"❌ {msg}", reply_markup=reply_kb(uid))
            return ConversationHandler.END
        lang, entry = find_entry(ed)
        if not entry:
            await safe_reply(update.message, "❌ no entry point", reply_markup=reply_kb(uid))
            return ConversationHandler.END
        dep_ok, dep_msg = await install_manifest(ed)
        sid = await _store_script(uid, entry, lang, entry.name)
        await safe_reply(update.message,
            f"{'✅' if dep_ok else '⚠️'} `{entry.name}` ({lang})\n🆔 `{sid}`\n_{dep_msg}_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=reply_kb(uid))
    else:
        lang = lang_of(dst)
        sid = await _store_script(uid, dst, lang, safe)
        await safe_reply(update.message,
            f"✅ `{safe}` ({lang})\n🆔 `{sid}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=reply_kb(uid))
    return ConversationHandler.END

@shield
async def up_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

# --- Terminal ---
ALLOWED = {"pwd", "ls", "dir", "cd", "cat", "type", "head", "tail", "mkdir", "cp", "copy",
           "mv", "move", "rm", "del", "grep", "find", "findstr", "wc", "stat", "du",
           "touch", "echo", "which", "where"}

@shield
async def term_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        await safe_reply(update.message, "❌ not approved", reply_markup=reply_kb(uid))
        return ConversationHandler.END
    ws = ws_of(uid); ctx.user_data["term_cwd"] = str(ws)
    await safe_reply(update.message,
        banner("💻 Terminal") + f"\n\n📁 `{ws}`\n\n"
        f"allowed: `{', '.join(sorted(ALLOWED))}`\n"
        "exit: /cancel · /exit · or press any menu button\n" + divider(),
        parse_mode=ParseMode.MARKDOWN)
    return TERM_SESSION

@shield
async def term_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    if not txt: return TERM_SESSION

    # ---- ESCAPE: menu button during terminal → exit + dispatch ----
    if txt in BTN_TEXTS:
        if txt == BTN_DEPLOY:    await on_deploy(update, ctx)
        elif txt == BTN_LOGS:    await on_logs(update, ctx)
        elif txt == BTN_STATS:   await on_stats(update, ctx)
        elif txt == BTN_STOP:    await on_stop_all(update, ctx)
        elif txt == BTN_FILES:   await on_files(update, ctx)
        elif txt == BTN_ADMIN and uid in ADMIN_IDS:
            await on_admin_panel(update, ctx)
        elif txt in (BTN_UPLOAD, BTN_TERM):
            await safe_reply(update.message, "🚪 terminal closed — press again to open",
                             reply_markup=reply_kb(uid))
        return ConversationHandler.END

    if txt.lower() in ("/cancel", "/exit", "/quit", "exit", "quit"):
        await safe_reply(update.message, "🚪 terminal closed", reply_markup=reply_kb(uid))
        return ConversationHandler.END

    try:
        parts = shlex.split(txt, posix=not IS_WIN)
    except ValueError as e:
        await safe_reply(update.message, f"❌ {e}"); return TERM_SESSION
    if not parts: return TERM_SESSION
    cmd = parts[0].lower()
    if cmd not in ALLOWED:
        await safe_reply(update.message, f"❌ `{cmd}` not allowed", parse_mode=ParseMode.MARKDOWN)
        return TERM_SESSION

    cwd = Path(ctx.user_data.get("term_cwd", str(ws_of(uid))))
    if cmd == "cd":
        t = Path(parts[1]) if len(parts) > 1 else cwd
        if not t.is_absolute(): t = cwd / t
        try:
            t = t.resolve()
            if not inside(uid, t) or not t.is_dir():
                await safe_reply(update.message, "❌ cannot cd there"); return TERM_SESSION
            ctx.user_data["term_cwd"] = str(t)
            await safe_reply(update.message, f"📁 `{t}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await safe_reply(update.message, f"❌ {e}")
        return TERM_SESSION

    proc = None
    try:
        env = os.environ.copy(); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUTF8"] = "1"
        proc = await asyncio.create_subprocess_exec(*parts, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        so, se = await asyncio.wait_for(proc.communicate(), timeout=30)
        out = (so.decode(errors="replace") + se.decode(errors="replace")) or "(no output)"
        if len(out) > 3800: out = out[:3800] + "\n…"
        await safe_reply(update.message, f"```\n{out}\n```", parse_mode=ParseMode.MARKDOWN)
    except asyncio.TimeoutError:
        if proc:
            try: proc.terminate()
            except Exception: pass
        await safe_reply(update.message, "❌ timed out")
    except Exception as e:
        await safe_reply(update.message, f"❌ {e}")
    return TERM_SESSION

@shield
async def term_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "🚪 terminal closed", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

# --- Edit ---
@shield
async def edit_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sedit_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return ConversationHandler.END
    ctx.user_data["edit_sid"] = sid
    await safe_edit(q, f"✏️ send new full content for `{s['filename']}`\n/cancel to abort",
                    parse_mode=ParseMode.MARKDOWN)
    return EDIT_WAIT

@shield
async def edit_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("edit_sid"); s = SCRIPTS.get(sid) if sid else None
    if not s:
        await safe_reply(update.message, "❌ lost", reply_markup=reply_kb(update.effective_user.id))
        return ConversationHandler.END
    try:
        Path(s["path"]).write_text(update.message.text or "", encoding="utf-8")
        await safe_reply(update.message, "✅ saved", reply_markup=reply_kb(update.effective_user.id))
        if (sup := SUPERVISORS.get(sid)) and sup.running():
            await sup.stop(); await sup.start(ctx.bot)
            await safe_reply(update.message, "🔄 restarted")
    except Exception as e:
        await safe_reply(update.message, f"❌ {e}")
    return ConversationHandler.END

@shield
async def edit_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

# --- Broadcast ---
@shield
async def bc_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return ConversationHandler.END
    await safe_edit(q, "📢 send broadcast text\n/cancel to abort")
    return BC_WAIT

@shield
async def bc_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    if not txt:
        await safe_reply(update.message, "❌ empty"); return BC_WAIT
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

# --- Env ---
@shield
async def env_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("senv_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return ConversationHandler.END
    ctx.user_data["env_sid"] = sid
    cur = s.get("env", {})
    body = "\n".join(f"`{k}`=`{v}`" for k, v in cur.items()) if cur else "(none)"
    await safe_edit(q,
        f"⚙️ *Env vars* for `{s['filename']}`\n\ncurrent:\n{body}\n\n"
        "Send `KEY=VALUE` per line to replace.\nOr send `-` to clear all.\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN)
    return ENV_WAIT

@shield
async def env_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("env_sid"); s = SCRIPTS.get(sid) if sid else None
    if not s:
        await safe_reply(update.message, "❌ lost", reply_markup=reply_kb(update.effective_user.id))
        return ConversationHandler.END
    txt = update.message.text or ""
    if txt.strip() == "-":
        s["env"] = {}
    else:
        env = {}
        for line in txt.splitlines():
            line = line.strip()
            if not line or "=" not in line: continue
            k, v = line.split("=", 1); env[k.strip()] = v.strip()
        s["env"] = env
    persist()
    if sup := SUPERVISORS.get(sid): sup.env = s["env"]
    await safe_reply(update.message, f"✅ saved {len(s['env'])} var(s)",
                     reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def env_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

# --- Live Input ---
@shield
async def input_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("sinput_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return ConversationHandler.END
    sup = SUPERVISORS.get(sid)
    if not sup or not sup.running():
        await safe_edit(q, "❌ not running"); return ConversationHandler.END
    ctx.user_data["input_sid"] = sid
    await safe_edit(q, f"📥 send text to pipe into `{s['filename']}` stdin\n/cancel to abort",
                    parse_mode=ParseMode.MARKDOWN)
    return INPUT_WAIT

@shield
async def input_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("input_sid")
    sup = SUPERVISORS.get(sid) if sid else None
    if not sup:
        await safe_reply(update.message, "❌ lost", reply_markup=reply_kb(update.effective_user.id))
        return ConversationHandler.END
    ok = sup.send_input(update.message.text or "")
    await safe_reply(update.message, "✅ sent" if ok else "❌ write failed",
                     reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

@shield
async def input_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

# --- Schedule ---
@shield
async def sch_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sid = q.data.replace("ssch_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        await safe_edit(q, "❌"); return ConversationHandler.END
    ctx.user_data["sch_sid"] = sid
    cur = s.get("interval")
    await safe_edit(q,
        f"⏰ *Schedule* `{s['filename']}`\n\n"
        f"current: {f'every {cur}s' if cur else 'off'}\n\n"
        "Send interval in seconds (min 30), or `0` to disable.\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN)
    return SCH_WAIT

@shield
async def sch_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("sch_sid"); s = SCRIPTS.get(sid) if sid else None
    if not s:
        await safe_reply(update.message, "❌ lost", reply_markup=reply_kb(update.effective_user.id))
        return ConversationHandler.END
    try:
        n = int((update.message.text or "").strip())
        if n == 0:
            s["interval"] = None
        elif n < 30:
            await safe_reply(update.message, "❌ min 30s"); return SCH_WAIT
        else:
            s["interval"] = n; s["last_run"] = 0
        persist()
        await safe_reply(update.message, f"✅ interval: {s.get('interval') or 'off'}",
                         reply_markup=reply_kb(update.effective_user.id))
    except ValueError:
        await safe_reply(update.message, "❌ send a number"); return SCH_WAIT
    return ConversationHandler.END

@shield
async def sch_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END

# --- File Manager ---
def _file_rows(uid, sub=""):
    root = ws_of(uid); base = (root / sub).resolve() if sub else root
    try:
        if not str(base).startswith(str(root)): base = root
    except Exception:
        base = root
    rows = []
    if base != root:
        parent = str(base.relative_to(root).parent)
        rows.append([InlineKeyboardButton("⬆️  ..", callback_data=f"fm_{parent}")])
    try:
        items = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception:
        items = []
    for p in items[:40]:
        try:
            if p.is_dir():
                rel = p.relative_to(root)
                rows.append([InlineKeyboardButton(f"📁  {p.name}", callback_data=f"fm_{rel}")])
            else:
                size = p.stat().st_size if p.exists() else 0
                rows.append([InlineKeyboardButton(f"📄  {p.name} ({size}B)",
                                                  callback_data=f"fmview_{p.relative_to(root)}")])
        except Exception:
            continue
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    return rows

@shield
async def on_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        await safe_reply(update.message, "❌ not approved", reply_markup=reply_kb(uid)); return
    await safe_reply(update.message, banner("📂 Files") + f"\n\n📁 `{ws_of(uid)}`",
                     parse_mode=ParseMode.MARKDOWN,
                     reply_markup=InlineKeyboardMarkup(_file_rows(uid)))

@shield
async def fm_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    sub = q.data.replace("fm_", "", 1)
    await safe_edit(q, banner(f"📂 /{sub}" if sub else "📂 Files"),
                    reply_markup=InlineKeyboardMarkup(_file_rows(q.from_user.id, sub)))

@shield
async def fm_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    rel = q.data.replace("fmview_", "", 1)
    p = (ws_of(q.from_user.id) / rel).resolve()
    if not inside(q.from_user.id, p) or not p.exists() or p.is_dir():
        await safe_edit(q, "❌"); return
    try:
        c = p.read_text(encoding="utf-8", errors="replace")[:3000]
        await safe_edit(q, f"👁 `{rel}`\n\n```\n{c}\n```", parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="fm_")]]))
    except Exception as e:
        await safe_edit(q, f"❌ {e}")

# ==================== LOGS / STATS / STOP ====================
@shield
async def on_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        await safe_reply(update.message, "❌"); return
    scripts = user_scripts(uid)
    if not scripts:
        await safe_reply(update.message, "📜 no logs", reply_markup=reply_kb(uid)); return
    rows = [[InlineKeyboardButton(f"📄  {s['filename']}", callback_data=f"slog_{s['sid']}")] for s in scripts]
    await safe_reply(update.message, banner("📜 Logs"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def on_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        await safe_reply(update.message, "❌"); return
    scripts = user_scripts(uid)
    lines = [banner("📊 Stats"), ""]
    for s in scripts:
        sup = SUPERVISORS.get(s["sid"])
        if sup and sup.running():
            st = sup.stats() or {}
            lines.append(f"🟢 `{s['filename']}` PID `{sup.proc.pid}` CPU `{st.get('cpu', '?')}%` "
                         f"RAM `{st.get('mem', '?')}MB` up `{st.get('uptime', '?')}s` ♻️{sup.restarts}")
        else:
            lines.append(f"⚪ `{s['filename']}` stopped")
    if not scripts: lines.append("(no scripts)")
    await safe_reply(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=reply_kb(uid))

@shield
async def on_stop_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owned = [sid for sid, sup in SUPERVISORS.items() if sup.uid == uid]
    for sid in owned:
        await SUPERVISORS[sid].stop()
    await safe_reply(update.message, f"🛑 stopped {len(owned)}", reply_markup=reply_kb(uid))

# ==================== ADMIN ====================
@shield
async def on_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await safe_reply(update.message, "❌ not admin"); return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users",     callback_data="a_users"),
         InlineKeyboardButton("⏳ Pending",   callback_data="a_pending")],
        [InlineKeyboardButton("🖥 Running",   callback_data="a_running"),
         InlineKeyboardButton("📊 Stats",     callback_data="a_stats")],
        [InlineKeyboardButton("🚫 Banned",    callback_data="a_banned"),
         InlineKeyboardButton("📢 Broadcast", callback_data="a_broadcast")],
        [InlineKeyboardButton("🧹 Cleanup",   callback_data="a_cleanup"),
         InlineKeyboardButton("💣 Nuke All",  callback_data="a_nuke")],
    ])
    await safe_reply(update.message, banner("👑 Admin Panel"), reply_markup=kb)

@shield
async def a_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    rows = [f"• {u['name']} — `{k}`" for k, u in USERS.items() if u["status"] == "approved"]
    await safe_edit(q, banner("👥 Approved") + "\n\n" + ("\n".join(rows) or "none"),
                    parse_mode=ParseMode.MARKDOWN)

@shield
async def a_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    pend = [(k, v) for k, v in USERS.items() if v["status"] == "pending"]
    if not pend:
        await safe_edit(q, "none pending"); return
    rows = [[InlineKeyboardButton(v['name'], callback_data=f"a_p_{k}")] for k, v in pend]
    await safe_edit(q, banner("⏳ Pending"), reply_markup=InlineKeyboardMarkup(rows))

@shield
async def a_p_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    try:
        uid = int(q.data.replace("a_p_", ""))
    except ValueError:
        await safe_edit(q, "bad id"); return
    u = USERS.get(str(uid))
    if not u:
        await safe_edit(q, "gone"); return
    await safe_edit(q, f"👤 {u['name']} (@{u['username']})\n🆔 `{uid}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
            InlineKeyboardButton("🚫 Ban",     callback_data=f"ban_{uid}")]]))

@shield
async def approve_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    try:
        uid = int(q.data.split("_")[1])
    except (ValueError, IndexError):
        await safe_edit(q, "bad"); return
    if approve(uid):
        await safe_edit(q, f"✅ approved `{uid}`", parse_mode=ParseMode.MARKDOWN)
        await safe_send(ctx.bot, uid, "✅ access granted", reply_markup=reply_kb(uid))

@shield
async def ban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    try:
        uid = int(q.data.split("_")[1])
    except (ValueError, IndexError):
        await safe_edit(q, "bad"); return
    if ban(uid):
        for sid, sup in list(SUPERVISORS.items()):
            if sup.uid == uid: await sup.stop()
        await safe_edit(q, f"🚫 banned `{uid}`", parse_mode=ParseMode.MARKDOWN)

@shield
async def a_running(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    run = [s for s in SUPERVISORS.values() if s.running()]
    if not run:
        await safe_edit(q, "none running"); return
    txt = banner("🖥 Running") + "\n\n" + "\n".join(
        f"• `{s.path.name}` PID `{s.proc.pid}` user `{s.uid}` ♻️{s.restarts}" for s in run)
    await safe_edit(q, txt, parse_mode=ParseMode.MARKDOWN)

@shield
async def a_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    await safe_edit(q,
        banner("📊 Stats") + f"\n\n👥 users: {len(USERS)}\n📁 scripts: {len(SCRIPTS)}\n"
        f"🖥 running: {sum(1 for s in SUPERVISORS.values() if s.running())}",
        parse_mode=ParseMode.MARKDOWN)

@shield
async def a_banned(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    rows = [(k, v) for k, v in USERS.items() if v["status"] == "banned"]
    if not rows:
        await safe_edit(q, "none"); return
    kb = [[InlineKeyboardButton(f"unban {v['name']}", callback_data=f"unban_{k}")] for k, v in rows]
    await safe_edit(q, banner("🚫 Banned"), reply_markup=InlineKeyboardMarkup(kb))

@shield
async def unban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    try:
        uid = int(q.data.split("_")[1])
    except (ValueError, IndexError):
        await safe_edit(q, "bad"); return
    if unban(uid):
        await safe_edit(q, f"✅ unbanned `{uid}`", parse_mode=ParseMode.MARKDOWN)

@shield
async def a_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    cutoff = datetime.now() - timedelta(days=7); n = 0
    for ws in WORKSPACE.iterdir():
        if ws.is_dir():
            for lg in ws.rglob("*.log"):
                try:
                    if datetime.fromtimestamp(lg.stat().st_mtime) < cutoff:
                        lg.unlink(); n += 1
                except Exception: pass
    await safe_edit(q, f"🧹 removed {n}")

@shield
async def a_nuke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await safe_edit(q, "❌"); return
    for sid, sup in list(SUPERVISORS.items()):
        await sup.stop(); SUPERVISORS.pop(sid, None)
    n = len(SCRIPTS); SCRIPTS.clear(); persist()
    await safe_edit(q, f"💣 nuked {n} script(s)", parse_mode=ParseMode.MARKDOWN)

# ==================== FLASK / WATCHDOG / BOOT ====================
flask_app = Flask(__name__)

@flask_app.route("/")
@flask_app.route("/api/healthz")
def health():
    return jsonify({"status": "ok",
                    "running": sum(1 for s in SUPERVISORS.values() if s.running())})

def run_flask():
    try:
        flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
    except Exception as e:
        log.warning("flask: %s", e)

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
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("[WD] %s", e)

async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([BotCommand("start", "🌑 Start"), BotCommand("help", "ℹ️ Help")])
    except Exception as e:
        log.warning("set_my_commands: %s", e)
    asyncio.create_task(watchdog(app.bot))
    asyncio.create_task(scheduler_loop())
    for sid, s in list(SCRIPTS.items()):
        if not s.get("autorestart", True): continue
        p = Path(s["path"]).resolve()
        if not p.exists(): continue
        sup = ScriptSupervisor(sid, s["user_id"], p, s["lang"], True, env=s.get("env", {}))
        SUPERVISORS[sid] = sup
        try: await sup.start(app.bot)
        except Exception as e:
            log.warning("[restore] %s: %s", sid, e)

def main():
    Thread(target=run_flask, daemon=True).start()
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(post_init)
           .read_timeout(30)
           .write_timeout(30)
           .connect_timeout(15)
           .pool_timeout(5)
           .get_updates_read_timeout(30)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_UPLOAD]), up_entry)],
        states={UP_WAIT: [MessageHandler(filters.Document.ALL, up_recv)]},
        fallbacks=[CommandHandler("cancel", up_cancel)], per_message=False, allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_TERM]), term_entry)],
        states={TERM_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, term_recv)]},
        fallbacks=[CommandHandler("cancel", term_cancel)], per_message=False, allow_reentry=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_entry, pattern="^sedit_")],
        states={EDIT_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_recv)]},
        fallbacks=[CommandHandler("cancel", edit_cancel)], per_message=False))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(env_entry, pattern="^senv_")],
        states={ENV_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, env_recv)]},
        fallbacks=[CommandHandler("cancel", env_cancel)], per_message=False))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(input_entry, pattern="^sinput_")],
        states={INPUT_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_recv)]},
        fallbacks=[CommandHandler("cancel", input_cancel)], per_message=False))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(sch_entry, pattern="^ssch_")],
        states={SCH_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sch_recv)]},
        fallbacks=[CommandHandler("cancel", sch_cancel)], per_message=False))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(bc_entry, pattern="^a_broadcast$")],
        states={BC_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_recv)]},
        fallbacks=[CommandHandler("cancel", bc_cancel)], per_message=False))

    app.add_handler(MessageHandler(filters.Text([BTN_DEPLOY]), on_deploy))
    app.add_handler(MessageHandler(filters.Text([BTN_LOGS]),   on_logs))
    app.add_handler(MessageHandler(filters.Text([BTN_STATS]),  on_stats))
    app.add_handler(MessageHandler(filters.Text([BTN_STOP]),   on_stop_all))
    app.add_handler(MessageHandler(filters.Text([BTN_FILES]),  on_files))
    app.add_handler(MessageHandler(filters.Text([BTN_ADMIN]),  on_admin_panel))

    app.add_handler(CallbackQueryHandler(check_join,     pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(main_menu_cb,   pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(deploy_list,    pattern="^deploy_list$"))
    app.add_handler(CallbackQueryHandler(script_view,    pattern="^sview_"))
    app.add_handler(CallbackQueryHandler(script_start,   pattern="^sstart_"))
    app.add_handler(CallbackQueryHandler(script_stop,    pattern="^sstop_"))
    app.add_handler(CallbackQueryHandler(script_restart, pattern="^srestart_"))
    app.add_handler(CallbackQueryHandler(script_auto,    pattern="^sauto_"))
    app.add_handler(CallbackQueryHandler(script_del,     pattern="^sdel_"))
    app.add_handler(CallbackQueryHandler(script_log,     pattern="^slog_"))
    app.add_handler(CallbackQueryHandler(script_code,    pattern="^scode_"))
    app.add_handler(CallbackQueryHandler(fm_browse,      pattern="^fm_"))
    app.add_handler(CallbackQueryHandler(fm_view,        pattern="^fmview_"))

    app.add_handler(CallbackQueryHandler(a_users,    pattern="^a_users$"))
    app.add_handler(CallbackQueryHandler(a_pending,  pattern="^a_pending$"))
    app.add_handler(CallbackQueryHandler(a_p_detail, pattern="^a_p_"))
    app.add_handler(CallbackQueryHandler(approve_cb, pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(ban_cb,     pattern="^ban_"))
    app.add_handler(CallbackQueryHandler(a_running,  pattern="^a_running$"))
    app.add_handler(CallbackQueryHandler(a_stats,    pattern="^a_stats$"))
    app.add_handler(CallbackQueryHandler(a_banned,   pattern="^a_banned$"))
    app.add_handler(CallbackQueryHandler(unban_cb,   pattern="^unban_"))
    app.add_handler(CallbackQueryHandler(a_cleanup,  pattern="^a_cleanup$"))
    app.add_handler(CallbackQueryHandler(a_nuke,     pattern="^a_nuke$"))

    app.add_error_handler(on_error)
    log.info("[SHADOW] booting…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    backoff = 5
    while True:
        try:
            main()
            backoff = 5
            break
        except KeyboardInterrupt:
            log.info("[SHADOW] bye")
            break
        except Conflict:
            log.error("[SHADOW] TOKEN CONFLICT — same token polling elsewhere. sleep 60s")
            time.sleep(60)
        except Exception:
            log.exception("[SHADOW] FATAL — restarting in %ss", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
