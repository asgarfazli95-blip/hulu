#!/usr/bin/env python3
"""
▸ PYHOST v4
- Strictly runs .py files.
- Pre-flight import resolver scans script source BEFORE launch.
- Every install verified by real subprocess import (find_spec lies).
- ensure_pip bootstrap if pip itself is broken.
- Local-module awareness: never pip-installs helper files next to the script.
- /diag dumps ground truth about the interpreter + pip state.
- Admin panel, spy alerts, autorestart, watchdog — all intact.
"""

import os
import sys
import asyncio
import json
import logging
import time
import hashlib
import re
import importlib.util
from pathlib import Path
from datetime import datetime

# ==================== CORE BOOT ====================
def _boot_core():
    pkgs = [
        ("telegram", "python-telegram-bot>=21"),
        ("dotenv", "python-dotenv"),
        ("psutil", "psutil"),
        ("requests", "requests"),
    ]

    import subprocess

    # 1. Ensure pip itself exists. If pip is broken, ensurepip fixes it.
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, timeout=20,
        )
        if r.returncode != 0:
            print("[PYHOST] pip missing — bootstrapping via ensurepip")
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--default-pip"],
                check=False, timeout=90,
            )
    except Exception as e:
        print(f"[PYHOST] pip probe failed: {e}", file=sys.stderr)

    # 2. Install any missing core packages.
    need = []
    for mod, pkg in pkgs:
        try:
            __import__(mod)
        except ImportError:
            need.append(pkg)

    if need:
        print(f"[PYHOST] Installing core: {', '.join(need)}")
        for flags in (["-U"], ["-U", "--user"], ["-U", "--break-system-packages"]):
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--quiet", *flags, *need],
                    check=True, timeout=300,
                )
                break
            except subprocess.CalledProcessError as e:
                print(f"[PYHOST] pip {flags} failed: {e}", file=sys.stderr)
            except subprocess.TimeoutExpired:
                print(f"[PYHOST] pip {flags} timed out", file=sys.stderr)

    # 3. Re-verify with real imports.
    for mod, pkg in pkgs:
        try:
            __import__(mod)
        except ImportError:
            print(
                f"[PYHOST] FATAL: {mod} ({pkg}) still not importable.\n"
                f"  Manual: {sys.executable} -m pip install {pkg}",
                file=sys.stderr,
            )

try:
    _boot_core()
except Exception as e:
    print(f"[PYHOST] Boot failed: {e}", file=sys.stderr)

import psutil
from dotenv import load_dotenv
from telegram import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton,
    InlineKeyboardMarkup, Update, BotCommand,
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest, Forbidden, RetryAfter, TimedOut,
    NetworkError, TelegramError, Conflict,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes,
)

# ==================== LOGGING ====================
log = logging.getLogger("pyhost")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    log.addHandler(_h)
log.setLevel(logging.INFO)

# ==================== RESILIENCE ====================
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
    if last:
        raise last


async def safe_reply(message, text, **kwargs):
    if message is None:
        return None

    async def _do():
        try:
            return await message.reply_text(text, **kwargs)
        except BadRequest as e:
            s = str(e).lower()
            if "not found" in s or "reply" in s or "message to be" in s:
                return await message.get_bot().send_message(
                    chat_id=message.chat_id, text=text, **kwargs
                )
            if "parse" in s or "entities" in s:
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await message.get_bot().send_message(
                    chat_id=message.chat_id, text=text, **k
                )
            raise
        except Forbidden:
            return None

    try:
        return await _retry(_do)
    except TelegramError as e:
        log.warning("safe_reply: %s", e)


async def safe_edit(query, text, **kwargs):
    if query is None:
        return None

    async def _do():
        try:
            return await query.edit_message_text(text, **kwargs)
        except BadRequest as e:
            s = str(e).lower()
            if "not modified" in s:
                return None
            if "not found" in s or "message to edit" in s:
                return await query.get_bot().send_message(
                    chat_id=query.from_user.id, text=text, **kwargs
                )
            if "parse" in s or "entities" in s:
                k = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                return await query.get_bot().send_message(
                    chat_id=query.from_user.id, text=text, **k
                )
            raise

    try:
        return await _retry(_do)
    except TelegramError as e:
        log.warning("safe_edit: %s", e)


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
        log.warning("safe_send: %s", e)


async def _answer(q):
    try:
        await q.answer()
    except Exception:
        pass


async def _nudge(update, context):
    try:
        if update is not None and getattr(update, "callback_query", None):
            await _answer(update.callback_query)
        chat_id = (
            update.effective_chat.id
            if getattr(update, "effective_chat", None) else None
        )
        if not chat_id and getattr(update, "callback_query", None):
            chat_id = update.callback_query.from_user.id
        if chat_id:
            await context.bot.send_message(
                chat_id=chat_id, text="⚠ hiccup — try again"
            )
    except Exception:
        pass


def shield(fn):
    async def _wrap(update, context, *a, **kw):
        try:
            return await fn(update, context, *a, **kw)
        except (BadRequest, Forbidden) as e:
            log.warning("shield[%s]: %s", fn.__name__, e)
            await _nudge(update, context)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
            try:
                return await fn(update, context, *a, **kw)
            except Exception:
                pass
        except Conflict as e:
            log.error("TOKEN CONFLICT: %s", e)
        except Exception as e:
            log.exception("shield[%s]: %s", fn.__name__, e)
            await _nudge(update, context)

    return _wrap


async def on_error(update, context):
    err = context.error
    if isinstance(err, Conflict):
        log.error("POLLING CONFLICT: %s", err); return
    if isinstance(err, RetryAfter):
        await asyncio.sleep(float(err.retry_after) + 0.5); return
    if isinstance(err, (BadRequest, Forbidden)):
        log.warning("soft error: %s", err); return
    log.exception("error:", exc_info=err)
    await _nudge(update, context)


# ==================== CONFIG ====================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS missing in .env")

WORKSPACE = Path(os.getenv("WORKSPACE_BASE", "./workspaces")).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
DATA_FILE = WORKSPACE / "shadow_data.json"
LOG_CAP = 10 * 1024 * 1024
WATCHDOG = 5


# ==================== UI PRIMITIVES ====================
BRAND = "▸ PYHOST"
RULE = "━" * 17


def banner(title: str = "") -> str:
    if title:
        return f"{BRAND} · {title}\n{RULE}"
    return f"{BRAND}\n{RULE}"


def _trunc(s: str, n: int = 26) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def state_dot(up: bool) -> str:
    return "●" if up else "○"


def plural(n: int, one: str, many: str = None) -> str:
    return one if n == 1 else (many or one + "s")


# ==================== IMPORT RESOLVER ====================
_STDLIB_FALLBACK = {
    "abc", "argparse", "atexit", "asyncio", "base64", "binascii", "builtins",
    "calendar", "codecs", "collections", "concurrent", "configparser",
    "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime",
    "decimal", "email", "enum", "errno", "faulthandler", "filecmp", "fnmatch",
    "fractions", "functools", "gc", "getpass", "gettext", "glob", "hashlib",
    "hmac", "http", "importlib", "inspect", "io", "itertools", "json",
    "locale", "logging", "marshal", "math", "mmap", "multiprocessing",
    "numbers", "operator", "os", "pathlib", "pickle", "pip", "platform",
    "pprint", "queue", "random", "re", "sched", "secrets", "select",
    "shlex", "shutil", "signal", "socket", "sqlite3", "ssl", "stat",
    "statistics", "string", "struct", "subprocess", "sys", "sysconfig",
    "tempfile", "textwrap", "threading", "time", "traceback", "types",
    "typing", "unittest", "urllib", "uuid", "venv", "warnings", "weakref",
    "zoneinfo", "__future__", "_thread",
}
STDLIB = set(getattr(sys, "stdlib_module_names", set())) | _STDLIB_FALLBACK

IMPORT_TO_PKG = {
    "telegram": "python-telegram-bot>=21",
    "dotenv": "python-dotenv",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "gtts": "gTTS",
    "edge_tts": "edge-tts",
    "yt_dlp": "yt-dlp",
    "telethon": "telethon",
    "requests": "requests",
    "aiohttp": "aiohttp",
    "httpx": "httpx",
    "numpy": "numpy",
    "pandas": "pandas",
    "psutil": "psutil",
    "fitz": "PyMuPDF",
    "lxml": "lxml",
    "dateutil": "python-dateutil",
    "Crypto": "pycryptodome",
    "nacl": "PyNaCl",
    "uvloop": "uvloop",
    "motor": "motor",
    "pymongo": "pymongo",
    "redis": "redis",
    "flask": "flask",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
    "pyrogram": "pyrogram",
    "tgcrypto": "TgCrypto",
}


def parse_imports(source: str):
    """Extract top-level module names from import / from-import statements."""
    names = set()
    for raw in source.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("import "):
            rest = line[7:]
            for part in rest.split(","):
                part = part.split(" as ", 1)[0].strip()
                top = part.split(".", 1)[0].strip()
                if top and top.isidentifier():
                    names.add(top)
        elif line.startswith("from "):
            rest = line[5:].strip()
            if " import " in rest:
                module = rest.split(" import ", 1)[0].strip()
                top = module.split(".", 1)[0].strip()
                if top and top.isidentifier():
                    names.add(top)
    return names


# ==================== DATA ====================
def _load():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}, "scripts": {}}


D = _load()
if "users" not in D:
    D["users"] = {}
if "scripts" not in D:
    D["scripts"] = {}
USERS = D["users"]
SCRIPTS = D["scripts"]


def persist():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(D, f, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        log.warning("persist: %s", e)


# ==================== SPY ====================
async def notify_admins(bot, text):
    if not bot:
        return
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=aid, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass


# ==================== RUNNER ====================
async def run_cmd(cmd, timeout=600):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return -1, "timeout"
        return proc.returncode, out.decode(errors="replace")
    except Exception as e:
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        return -1, str(e)


async def verify_import(mod: str, timeout: int = 20) -> bool:
    """Authoritative: actually import the module in a fresh subprocess."""
    rc, _ = await run_cmd([sys.executable, "-c", f"import {mod}"], timeout=timeout)
    return rc == 0


async def ensure_pip() -> bool:
    rc, _ = await run_cmd([sys.executable, "-m", "pip", "--version"], timeout=20)
    if rc == 0:
        return True
    await run_cmd(
        [sys.executable, "-m", "ensurepip", "--default-pip"], timeout=90
    )
    rc, _ = await run_cmd([sys.executable, "-m", "pip", "--version"], timeout=20)
    return rc == 0


async def pip_install(pkg: str, timeout: int = 300):
    strategies = [
        ["-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "--quiet", pkg],
        ["-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "--quiet", "--user", pkg],
        ["-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "--break-system-packages", pkg],
    ]
    logs = []
    last_rc = -1
    for flags in strategies:
        rc, out = await run_cmd([sys.executable, *flags], timeout=timeout)
        logs.append(f"$ pip install {pkg}\n{out}")
        last_rc = rc
        if rc == 0:
            return 0, "\n".join(logs)
    return last_rc, "\n".join(logs)


# ==================== SUPERVISOR ====================
SUPERVISORS = {}


class ScriptSupervisor:
    def __init__(self, sid, uid, path, auto=True, env=None):
        self.sid = sid
        self.uid = uid
        self.path = Path(path).resolve()
        self.auto = auto
        self.env = env or {}
        self.log = self.path.parent / "logs" / f"{sid}.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.proc = None
        self._task = None
        self._stop = False
        self.restarts = 0
        self.backoff = 1
        self._healing = False
        self.started = None

    def running(self):
        return self.proc is not None and self.proc.returncode is None

    def _w(self, msg):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            if self.log.exists() and self.log.stat().st_size > LOG_CAP:
                self.log.write_text("", encoding="utf-8")
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [PYHOST] {msg}\n")
        except Exception:
            pass

    def stats(self):
        if not self.running():
            return None
        try:
            p = psutil.Process(self.proc.pid)
            with p.oneshot():
                return {
                    "cpu": round(p.cpu_percent(interval=0), 1),
                    "mem": round(p.memory_info().rss / 1024 / 1024, 1),
                }
        except Exception:
            return None

    def _is_local(self, mod: str) -> bool:
        d = self.path.parent
        return (d / f"{mod}.py").exists() or (d / mod).is_dir()

    async def _install_missing(self, source_text: str):
        imported = parse_imports(source_text)
        missing = sorted(
            m for m in imported
            if m not in STDLIB
            and not self._is_local(m)
            and not await verify_import(m)
        )
        installed, failed, logs = [], [], []
        for mod in missing:
            pkg = IMPORT_TO_PKG.get(mod, mod)
            self._w(f"pip: {mod} → {pkg}")
            rc, out = await pip_install(pkg)
            if rc == 0 and await verify_import(mod):
                installed.append(pkg)
                continue
            alt = mod.lower()
            if alt != pkg.lower():
                self._w(f"pip: {mod} → {alt} (fallback)")
                rc2, out2 = await pip_install(alt)
                if rc2 == 0 and await verify_import(mod):
                    installed.append(alt)
                    continue
            failed.append(mod)
            logs.append(f"[{mod}] rc={rc}\n{out[-400:]}")
        return installed, failed, "\n".join(logs)

    async def pre_heal(self, bot=None) -> str:
        try:
            src = self.path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self._w(f"pre_heal read: {e}")
            return ""
        installed, failed, _ = await self._install_missing(src)
        if installed:
            self._w(f"pre_heal ✓ {installed}")
        if failed:
            self._w(f"pre_heal ✕ {failed}")
        if bot and (installed or failed):
            note = ""
            if installed:
                note += f"✓  installed  `{'`, `'.join(installed)}`\n"
            if failed:
                note += f"✕  failed     `{'`, `'.join(failed)}`\n"
            try:
                await safe_send(
                    bot, self.uid,
                    banner(f"pre-heal · {self.path.name}") + f"\n\n{note}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
        return ""

    async def start(self, bot=None):
        if self.running():
            return
        self._stop = False

        try:
            await self.pre_heal(bot)
        except Exception as e:
            self._w(f"pre_heal crash: {e}")

        if self._stop:
            self._w("start aborted — stopped mid pre-heal")
            return

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env.update(self.env)

        try:
            self._w(f"launch {self.path}")
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(self.path),
                cwd=str(self.path.parent),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.started = datetime.now()
            self._w(f"pid {self.proc.pid}")

            u = USERS.get(str(self.uid), {})
            uname = u.get("username", "unknown")
            await notify_admins(
                bot,
                banner(f"start · {self.path.name}")
                + f"\n\n@{uname}  ·  `{self.uid}`",
            )
            self._task = asyncio.create_task(self._pump(bot))
        except Exception as e:
            self._w(f"launch fail: {e}")

    async def _pump(self, bot=None):
        if not self.proc or not self.proc.stdout:
            return
        tail = []
        try:
            with open(self.log, "a", encoding="utf-8") as lf:
                while True:
                    ln = await self.proc.stdout.readline()
                    if not ln:
                        break
                    t = ln.decode(errors="replace")
                    lf.write(t); lf.flush()
                    tail.append(t)
                    if len(tail) > 300:
                        tail = tail[-300:]
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._w(f"pump: {e}")
        finally:
            if self.proc:
                try:
                    await self.proc.wait()
                except Exception:
                    pass
                self._w(f"exit {self.proc.returncode}")
            tail_text = "".join(tail[-120:])
            if self.auto and not self._stop and bot:
                await self._heal(bot, tail_text)

    async def _heal(self, bot, tail):
        if self._healing:
            return
        self._healing = True
        try:
            installed, failed, pip_log = [], [], ""

            # 1. Source scan
            try:
                src = self.path.read_text(encoding="utf-8", errors="replace")
                installed, failed, pip_log = await self._install_missing(src)
            except Exception as e:
                self._w(f"heal rescan: {e}")

            # 2. Tail hints
            hinted = set()
            for m in re.findall(r"pip\s+install\s+(.+)", tail):
                for part in m.replace('"', "").replace("'", "").split():
                    if (
                        part and not part.startswith("-")
                        and part not in ("pip", "install", "-U", "-r", "-m")
                    ):
                        hinted.add(part)
            for mod in re.findall(r"No module named ['\"]([^'\"]+)['\"]", tail):
                top = mod.split(".")[0]
                hinted.add(IMPORT_TO_PKG.get(top, top))

            def _base(p):
                return p.split(">=")[0].split("==")[0].split("<")[0].lower()

            have = {_base(x) for x in installed}
            for pkg in hinted:
                if not pkg or pkg.startswith("-"):
                    continue
                if _base(pkg) in have:
                    continue
                self._w(f"pip (tail): {pkg}")
                rc, out = await pip_install(pkg)
                if rc == 0:
                    installed.append(pkg)
                else:
                    failed.append(pkg)
                    pip_log += f"\n[{pkg}] rc={rc}\n{out[-400:]}"

            # 3. Fallback batch
            if not installed and not failed and (
                "missing dep" in tail or "No module named" in tail
            ):
                self._w("heal: fallback batch")
                for pkg in (
                    "requests", "python-telegram-bot>=21",
                    "telethon", "yt-dlp", "gTTS", "edge-tts",
                ):
                    rc, out = await pip_install(pkg)
                    if rc == 0:
                        installed.append(pkg)
                    else:
                        failed.append(pkg)
                        pip_log += f"\n[{pkg}] rc={rc}\n{out[-200:]}"

            note = ""
            if installed:
                note += f"✓  installed  `{'`, `'.join(dict.fromkeys(installed))}`\n"
            if failed:
                note += f"✕  failed     `{'`, `'.join(dict.fromkeys(failed))}`\n"
            if not note:
                note = f"⚠  unresolved\n```\n{tail[-400:]}\n```\n"
            if pip_log:
                note += f"\n```\n{pip_log[-800:]}\n```\n"

            await safe_send(
                bot, self.uid,
                banner(f"heal · {self.path.name}") + f"\n\n{note}restarting…",
                parse_mode=ParseMode.MARKDOWN,
            )

            self._healing = False
            self.restarts += 1
            await asyncio.sleep(min(self.backoff, 60))
            self.backoff = min(self.backoff * 2, 60)
            await self.start(bot)
        finally:
            self._healing = False

    async def stop(self, bot=None):
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self.proc.kill()
                    await self.proc.wait()
            except Exception:
                pass
        self._w("stopped")
        self.proc = None

        if bot:
            u = USERS.get(str(self.uid), {})
            uname = u.get("username", "unknown")
            await notify_admins(
                bot,
                banner(f"stop · {self.path.name}")
                + f"\n\n@{uname}  ·  `{self.uid}`",
            )


# ==================== HELPERS ====================
def get_user(uid):
    return USERS.get(str(uid))


def add_user(uid, name, uname):
    USERS[str(uid)] = {"status": "pending", "name": name, "username": uname}
    persist()


def approve(uid):
    u = USERS.get(str(uid))
    if u:
        u["status"] = "approved"
        persist()
        return True
    return False


def ban(uid):
    u = USERS.get(str(uid))
    if u:
        u["status"] = "banned"
        persist()
        return True
    return False


def unban(uid):
    return approve(uid)


def is_ok(uid):
    u = get_user(uid)
    return bool(u and u["status"] == "approved")


def is_pend(uid):
    u = get_user(uid)
    return bool(u and u["status"] == "pending")


def is_ban(uid):
    u = get_user(uid)
    return bool(u and u["status"] == "banned")


def user_scripts(uid):
    return [s for s in SCRIPTS.values() if s["user_id"] == uid]


def ws_of(uid):
    w = WORKSPACE / str(uid)
    w.mkdir(parents=True, exist_ok=True)
    return w


# ==================== KEYBOARD ====================
BTN_SCRIPTS = "Scripts"
BTN_UPLOAD = "Upload"
BTN_LOGS = "Logs"
BTN_STATS = "Stats"
BTN_STOP = "Stop All"
BTN_ADMIN = "Admin"


def reply_kb(uid):
    rows = [
        [BTN_SCRIPTS, BTN_UPLOAD],
        [BTN_LOGS, BTN_STATS],
        [BTN_STOP],
    ]
    if uid in ADMIN_IDS:
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(
        [[KeyboardButton(b) for b in r] for r in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


# ==================== RENDER ====================
async def _render_deploy_list(q):
    rows = []
    for s in user_scripts(q.from_user.id):
        sup = SUPERVISORS.get(s["sid"])
        up = bool(sup and sup.running())
        label = f"{state_dot(up)}  {_trunc(s['filename'], 28)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"sview_{s['sid']}")])
    if not rows:
        rows.append([InlineKeyboardButton("Upload a script to begin", callback_data="noop")])
    await safe_edit(
        q, banner("scripts"), reply_markup=InlineKeyboardMarkup(rows),
    )


async def _render_script_view(q, sid):
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        return await safe_edit(q, "script not found")

    sup = SUPERVISORS.get(sid)
    up = sup.running() if sup else False
    pid = sup.proc.pid if (sup and up) else "—"
    auto = "on" if s.get("autorestart", True) else "off"
    restarts = sup.restarts if sup else 0
    st = sup.stats() if sup else None

    state = "● running" if up else "○ stopped"
    if st:
        load = f"{st['cpu']}%  ·  {st['mem']} MB"
    else:
        load = "—"

    txt = (
        banner("script") + "\n\n"
        f"*{s['filename']}*\n\n"
        f"state    {state}\n"
        f"pid      {pid}\n"
        f"auto     {auto}  ·  {restarts} {plural(restarts, 'restart')}\n"
        f"load     {load}\n\n"
        f"`{s['path']}`"
    )

    kb = []
    if up:
        kb.append([
            InlineKeyboardButton("↻ Restart", callback_data=f"srestart_{sid}"),
            InlineKeyboardButton("■ Stop", callback_data=f"sstop_{sid}"),
        ])
    else:
        kb.append([InlineKeyboardButton("▶ Start", callback_data=f"sstart_{sid}")])
    kb.append([
        InlineKeyboardButton("Logs", callback_data=f"slog_{sid}"),
        InlineKeyboardButton("Code", callback_data=f"scode_{sid}"),
    ])
    kb.append([
        InlineKeyboardButton("Edit", callback_data=f"sedit_{sid}"),
        InlineKeyboardButton("Env", callback_data=f"senv_{sid}"),
    ])
    kb.append([
        InlineKeyboardButton(f"Auto · {auto}", callback_data=f"sauto_{sid}"),
        InlineKeyboardButton("✕ Delete", callback_data=f"sdel_{sid}"),
    ])
    kb.append([InlineKeyboardButton("‹ Back", callback_data="deploy_list")])

    await safe_edit(
        q, txt, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ==================== HANDLERS ====================
@shield
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    uid, name, uname = u.id, u.full_name, u.username or "no_username"

    if not get_user(uid):
        add_user(uid, name, uname)
        for aid in ADMIN_IDS:
            await safe_send(
                ctx.bot, aid,
                banner("request") + f"\n\n*{name}*\n@{uname}  ·  `{uid}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✓ Approve", callback_data=f"approve_{uid}"),
                    InlineKeyboardButton("⊘ Ban", callback_data=f"ban_{uid}"),
                ]]),
            )
        return await safe_reply(update.message, banner("request sent"))

    if is_ban(uid):
        return await safe_reply(update.message, "access revoked")
    if is_pend(uid):
        return await safe_reply(update.message, "awaiting approval")
    await safe_reply(
        update.message,
        banner("welcome") + f"\n\n  {name}\n  ready.",
        reply_markup=reply_kb(uid),
    )


@shield
async def cmd_diag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await safe_reply(update.message, "admin only")

    lines = [
        banner("diagnostics"),
        f"`{sys.executable}`",
        f"python `{sys.version.split()[0]}`",
    ]

    for mod in ("requests", "telegram", "psutil"):
        spec = None
        try:
            spec = importlib.util.find_spec(mod)
        except Exception:
            pass
        spec_str = spec.origin if spec and spec.origin else "(none)"
        rc, out = await run_cmd(
            [sys.executable, "-c", f"import {mod}; print({mod}.__file__)"],
            timeout=15,
        )
        mark = "✓" if rc == 0 else "✕"
        loc = out.strip().splitlines()[-1] if out.strip() else ""
        lines.append(
            f"\n*{mod}*  {mark}\n"
            f"spec `{spec_str}`\n"
            f"real `{loc or 'FAILED'}`"
        )

    rc, out = await run_cmd(
        [sys.executable, "-m", "pip", "show", "requests"], timeout=20
    )
    pip_view = out.strip()[:400] if rc == 0 else f"(rc={rc}) {out.strip()[:200]}"
    lines.append(f"\n*`pip show requests`*\n```\n{pip_view}\n```")

    rc, out = await run_cmd(
        [sys.executable, "-c", "import sys; print('\\n'.join(sys.path))"],
        timeout=15,
    )
    lines.append(f"\n*`sys.path`*\n```\n{(out.strip()[:600] if rc == 0 else '(failed)')}\n```")

    body = "\n".join(lines)
    for i in range(0, len(body), 3500):
        await safe_send(
            ctx.bot, update.effective_user.id,
            body[i:i + 3500], parse_mode=ParseMode.MARKDOWN,
        )
        await asyncio.sleep(0.3)


@shield
async def on_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        return await safe_reply(update.message, "not approved", reply_markup=reply_kb(uid))
    rows = []
    for s in user_scripts(uid):
        sup = SUPERVISORS.get(s["sid"])
        up = bool(sup and sup.running())
        label = f"{state_dot(up)}  {_trunc(s['filename'], 28)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"sview_{s['sid']}")])
    if not rows:
        return await safe_reply(
            update.message,
            banner("scripts") + "\n\n  no scripts yet — tap Upload",
            reply_markup=reply_kb(uid),
        )
    await safe_reply(
        update.message, banner("scripts"),
        reply_markup=InlineKeyboardMarkup(rows),
    )


@shield
async def deploy_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    await _render_deploy_list(q)


@shield
async def script_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("sview_", "", 1)
    await _render_script_view(q, sid)


@shield
async def s_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("sstart_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        return
    if sid not in SUPERVISORS:
        SUPERVISORS[sid] = ScriptSupervisor(
            sid, s["user_id"], s["path"],
            s.get("autorestart", True), s.get("env", {}),
        )
    sup = SUPERVISORS[sid]
    if not sup.running():
        s["runs"] = s.get("runs", 0) + 1
        persist()
        await sup.start(ctx.bot)
    await _render_script_view(q, sid)


@shield
async def s_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("sstop_", "", 1)
    if sid in SUPERVISORS:
        await SUPERVISORS[sid].stop(ctx.bot)
    await _render_script_view(q, sid)


@shield
async def s_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("srestart_", "", 1)
    if sid in SUPERVISORS:
        await SUPERVISORS[sid].stop(ctx.bot)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        return
    if sid not in SUPERVISORS:
        SUPERVISORS[sid] = ScriptSupervisor(
            sid, s["user_id"], s["path"],
            s.get("autorestart", True), s.get("env", {}),
        )
    sup = SUPERVISORS[sid]
    if not sup.running():
        s["runs"] = s.get("runs", 0) + 1
        persist()
        await sup.start(ctx.bot)
    await _render_script_view(q, sid)


@shield
async def s_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("sauto_", "", 1)
    s = SCRIPTS.get(sid)
    if s:
        s["autorestart"] = not s.get("autorestart", True)
        persist()
        if sid in SUPERVISORS:
            SUPERVISORS[sid].auto = s["autorestart"]
    await _render_script_view(q, sid)


@shield
async def s_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("sdel_", "", 1)
    if sid in SUPERVISORS:
        await SUPERVISORS[sid].stop(ctx.bot)
        del SUPERVISORS[sid]
    if sid in SCRIPTS:
        p = Path(SCRIPTS[sid]["path"])
        if p.exists():
            p.unlink()
        del SCRIPTS[sid]
        persist()
    await _render_deploy_list(q)


@shield
async def s_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("slog_", "", 1)
    s = SCRIPTS.get(sid)
    if not s:
        return
    sup = SUPERVISORS.get(sid)
    lp = sup.log if sup else Path(s["path"]).parent / "logs" / f"{sid}.log"
    try:
        tail = lp.read_text(encoding="utf-8", errors="replace")[-3400:] if lp.exists() else "(no logs yet)"
    except Exception as e:
        tail = f"error: {e}"
    kb = [
        [InlineKeyboardButton("↻ Refresh", callback_data=f"slog_{sid}"),
         InlineKeyboardButton("‹ Back", callback_data=f"sview_{sid}")],
    ]
    await safe_edit(
        q,
        banner(f"logs · {_trunc(s['filename'], 20)}") + f"\n```\n{tail}\n```",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


@shield
async def s_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("scode_", "", 1)
    s = SCRIPTS.get(sid)
    if not s:
        return
    try:
        c = Path(s["path"]).read_text(encoding="utf-8", errors="replace")
        if len(c) > 3400:
            c = c[:3400] + "\n…"
        kb = [[InlineKeyboardButton("‹ Back", callback_data=f"sview_{sid}")]]
        await safe_edit(
            q,
            banner(f"source · {_trunc(s['filename'], 20)}") + f"\n```\n{c}\n```",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception as e:
        await safe_edit(q, f"error: {e}")


# ==================== CONVERSATIONS ====================
(UP_WAIT, EDIT_WAIT, BC_WAIT, ENV_WAIT) = range(4)


@shield
async def up_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_ok(update.effective_user.id):
        return ConversationHandler.END
    await safe_reply(
        update.message,
        banner("upload") + "\n\n  send a .py file\n  /cancel to abort",
    )
    return UP_WAIT


@shield
async def up_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        return ConversationHandler.END
    doc = update.message.document
    if not doc:
        return UP_WAIT
    fn = doc.file_name or "file.py"
    if not fn.endswith(".py"):
        return await safe_reply(update.message, "only .py files", reply_markup=reply_kb(uid))

    ws = ws_of(uid)
    dst = (ws / fn).resolve()
    try:
        tgf = await ctx.bot.get_file(doc.file_id)
        await tgf.download_to_drive(str(dst))
    except Exception as e:
        return await safe_reply(update.message, f"download failed: {e}", reply_markup=reply_kb(uid))

    sid = hashlib.sha1(f"{uid}:{dst}:{time.time()}".encode()).hexdigest()[:12]
    SCRIPTS[sid] = {
        "sid": sid, "user_id": uid, "path": str(dst),
        "filename": fn, "autorestart": True, "env": {}, "runs": 0,
    }
    persist()

    u = USERS.get(str(uid), {})
    await notify_admins(
        ctx.bot,
        banner(f"upload · {_trunc(fn, 24)}")
        + f"\n\n@{u.get('username', 'unknown')}  ·  `{uid}`",
    )
    await safe_reply(
        update.message,
        banner("uploaded") + f"\n\n  {fn}\n  id  `{sid}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_kb(uid),
    )
    return ConversationHandler.END


@shield
async def up_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END


@shield
async def edit_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("sedit_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        return ConversationHandler.END
    ctx.user_data["edit_sid"] = sid
    await safe_edit(
        q,
        banner("edit") + f"\n\n  {s['filename']}\n\n  send new full content\n  /cancel to abort",
    )
    return EDIT_WAIT


@shield
async def edit_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("edit_sid")
    s = SCRIPTS.get(sid) if sid else None
    if not s:
        return ConversationHandler.END
    Path(s["path"]).write_text(update.message.text or "", encoding="utf-8")
    await safe_reply(update.message, "saved", reply_markup=reply_kb(update.effective_user.id))
    if sid in SUPERVISORS and SUPERVISORS[sid].running():
        await SUPERVISORS[sid].stop(ctx.bot)
        await SUPERVISORS[sid].start(ctx.bot)
    return ConversationHandler.END


@shield
async def edit_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END


@shield
async def env_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    sid = q.data.replace("senv_", "", 1)
    s = SCRIPTS.get(sid)
    if not s or s["user_id"] != q.from_user.id:
        return ConversationHandler.END
    ctx.user_data["env_sid"] = sid
    cur = s.get("env", {})
    body = "\n".join(f"  {k}  =  {v}" for k, v in cur.items()) if cur else "  (none)"
    await safe_edit(
        q,
        banner("env") + f"\n\n  {s['filename']}\n\n{body}\n\n"
        f"  send KEY=VALUE per line to replace\n"
        f"  or send `-` to clear all\n  /cancel to abort",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ENV_WAIT


@shield
async def env_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sid = ctx.user_data.get("env_sid")
    s = SCRIPTS.get(sid) if sid else None
    if not s:
        return ConversationHandler.END
    txt = update.message.text or ""
    if txt.strip() == "-":
        s["env"] = {}
    else:
        env = {}
        for line in txt.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        s["env"] = env
    persist()
    if sid in SUPERVISORS:
        SUPERVISORS[sid].env = s["env"]
    n = len(s["env"])
    await safe_reply(
        update.message,
        f"saved · {n} {plural(n, 'var')}",
        reply_markup=reply_kb(update.effective_user.id),
    )
    return ConversationHandler.END


@shield
async def env_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END


@shield
async def on_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        return
    scripts = user_scripts(uid)
    if not scripts:
        return await safe_reply(update.message, "no scripts yet", reply_markup=reply_kb(uid))
    rows = [
        [InlineKeyboardButton(_trunc(s['filename'], 28), callback_data=f"slog_{s['sid']}")]
        for s in scripts
    ]
    await safe_reply(
        update.message, banner("logs"),
        reply_markup=InlineKeyboardMarkup(rows),
    )


@shield
async def on_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_ok(uid):
        return
    scripts = user_scripts(uid)
    if not scripts:
        return await safe_reply(update.message, "no scripts yet", reply_markup=reply_kb(uid))

    blocks = []
    for s in scripts:
        sup = SUPERVISORS.get(s["sid"])
        if sup and sup.running():
            st = sup.stats() or {}
            blocks.append(
                f"●  {s['filename']}\n"
                f"   pid {sup.proc.pid}  ·  "
                f"{st.get('cpu', '?')}%  ·  {st.get('mem', '?')} MB"
            )
        else:
            blocks.append(f"○  {s['filename']}\n   stopped")

    txt = banner("stats") + "\n\n" + "\n\n".join(blocks)
    await safe_reply(
        update.message, txt,
        parse_mode=ParseMode.MARKDOWN, reply_markup=reply_kb(uid),
    )


@shield
async def on_stop_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owned = [sid for sid, sup in SUPERVISORS.items() if sup.uid == uid]
    for sid in owned:
        await SUPERVISORS[sid].stop(ctx.bot)
    await safe_reply(
        update.message,
        f"stopped · {len(owned)} {plural(len(owned), 'script')}",
        reply_markup=reply_kb(uid),
    )


# ==================== ADMIN ====================
@shield
async def on_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await safe_reply(update.message, "admin only")

    approved_n = sum(1 for v in USERS.values() if v["status"] == "approved")
    pending_n = sum(1 for v in USERS.values() if v["status"] == "pending")
    banned_n = sum(1 for v in USERS.values() if v["status"] == "banned")
    running_n = sum(1 for s in SUPERVISORS.values() if s.running())

    txt = (
        banner("admin") + "\n\n"
        f"  users      {approved_n}\n"
        f"  pending    {pending_n}\n"
        f"  banned     {banned_n}\n"
        f"  running    {running_n}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Users", callback_data="a_users"),
         InlineKeyboardButton("Pending", callback_data="a_pending")],
        [InlineKeyboardButton("Running", callback_data="a_running"),
         InlineKeyboardButton("Banned", callback_data="a_banned")],
        [InlineKeyboardButton("Broadcast", callback_data="a_broadcast"),
         InlineKeyboardButton("✕ Nuke All", callback_data="a_nuke")],
    ])
    await safe_reply(update.message, txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@shield
async def a_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    approved = [(k, v) for k, v in USERS.items() if v["status"] == "approved"]
    if not approved:
        return await safe_edit(
            q, banner("users") + "\n\n  no approved users",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("‹ Back", callback_data="admin_root")]]
            ),
        )
    rows = []
    for k, v in approved:
        rows.append([
            InlineKeyboardButton(
                f"{_trunc(v.get('name', '?'), 20)}  ·  {k}",
                callback_data=f"adetail_{k}",
            ),
            InlineKeyboardButton("⊘", callback_data=f"ban_{k}"),
        ])
    rows.append([InlineKeyboardButton("‹ Back", callback_data="admin_root")])
    await safe_edit(
        q,
        banner(f"users · {len(approved)}"),
        reply_markup=InlineKeyboardMarkup(rows),
    )


@shield
async def a_user_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    try:
        uid = int(q.data.replace("adetail_", ""))
    except ValueError:
        return await safe_edit(q, "bad id")
    u = USERS.get(str(uid))
    if not u:
        return await safe_edit(q, "gone")

    kb = []
    if u["status"] == "approved":
        kb.append([InlineKeyboardButton("⊘ Ban", callback_data=f"ban_{uid}")])
    elif u["status"] == "banned":
        kb.append([InlineKeyboardButton("✓ Unban", callback_data=f"unban_{uid}")])
    kb.append([InlineKeyboardButton("‹ Back", callback_data="a_users")])

    await safe_edit(
        q,
        banner("user") + f"\n\n"
        f"*{u['name']}*\n"
        f"@{u['username']}\n"
        f"id   `{uid}`\n"
        f"status   {u['status']}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


@shield
async def a_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    pend = [(k, v) for k, v in USERS.items() if v["status"] == "pending"]
    if not pend:
        return await safe_edit(
            q, banner("pending") + "\n\n  none waiting",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("‹ Back", callback_data="admin_root")]]
            ),
        )
    rows = [
        [InlineKeyboardButton(
            f"{_trunc(v.get('name', '?'), 22)}  ·  {k}",
            callback_data=f"a_p_{k}",
        )]
        for k, v in pend
    ]
    rows.append([InlineKeyboardButton("‹ Back", callback_data="admin_root")])
    await safe_edit(
        q,
        banner(f"pending · {len(pend)}"),
        reply_markup=InlineKeyboardMarkup(rows),
    )


@shield
async def a_p_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    try:
        uid = int(q.data.replace("a_p_", ""))
    except ValueError:
        return await safe_edit(q, "bad id")
    u = USERS.get(str(uid))
    if not u:
        return await safe_edit(q, "gone")
    await safe_edit(
        q,
        banner("request") + f"\n\n*{u['name']}*\n@{u['username']}\nid   `{uid}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✓ Approve", callback_data=f"approve_{uid}"),
             InlineKeyboardButton("⊘ Ban", callback_data=f"ban_{uid}")],
            [InlineKeyboardButton("‹ Back", callback_data="a_pending")],
        ]),
    )


@shield
async def approve_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    try:
        uid = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        return await safe_edit(q, "bad id")
    if approve(uid):
        await safe_edit(
            q, f"approved · `{uid}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("‹ Back", callback_data="a_users")]]
            ),
        )
        await safe_send(ctx.bot, uid, "access granted", reply_markup=reply_kb(uid))


@shield
async def ban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    try:
        uid = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        return await safe_edit(q, "bad id")
    if ban(uid):
        killed = 0
        for sid, sup in list(SUPERVISORS.items()):
            if sup.uid == uid:
                try:
                    await sup.stop(ctx.bot)
                except Exception:
                    pass
                SUPERVISORS.pop(sid, None)
                killed += 1
        await safe_send(ctx.bot, uid, "access revoked")
        await safe_edit(
            q,
            f"banned · `{uid}`\n"
            f"killed · {killed} {plural(killed, 'script')}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("‹ Back", callback_data="a_users")]]
            ),
        )


@shield
async def a_banned(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    rows_data = [(k, v) for k, v in USERS.items() if v["status"] == "banned"]
    if not rows_data:
        return await safe_edit(
            q, banner("banned") + "\n\n  none banned",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("‹ Back", callback_data="admin_root")]]
            ),
        )
    kb = [
        [InlineKeyboardButton(
            f"✓ unban  ·  {_trunc(v.get('name', k), 18)}  ·  {k}",
            callback_data=f"unban_{k}",
        )]
        for k, v in rows_data
    ]
    kb.append([InlineKeyboardButton("‹ Back", callback_data="admin_root")])
    await safe_edit(
        q,
        banner(f"banned · {len(rows_data)}"),
        reply_markup=InlineKeyboardMarkup(kb),
    )


@shield
async def unban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    try:
        uid = int(q.data.split("_", 1)[1])
    except (ValueError, IndexError):
        return await safe_edit(q, "bad id")
    if unban(uid):
        await safe_send(ctx.bot, uid, "access restored — /start again", reply_markup=reply_kb(uid))
        await safe_edit(
            q, f"unbanned · `{uid}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("‹ Back", callback_data="a_banned")]]
            ),
        )


@shield
async def a_running(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    run = [s for s in SUPERVISORS.values() if s.running()]
    if not run:
        return await safe_edit(
            q, banner("running") + "\n\n  none running",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("‹ Back", callback_data="admin_root")]]
            ),
        )
    blocks = [
        f"●  {s.path.name}\n   pid {s.proc.pid}  ·  user {s.uid}"
        for s in run
    ]
    txt = banner(f"running · {len(run)}") + "\n\n" + "\n\n".join(blocks)
    await safe_edit(
        q, txt, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("‹ Back", callback_data="admin_root")]]
        ),
    )


@shield
async def bc_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await safe_edit(q, banner("broadcast") + "\n\n  send text\n  /cancel to abort")
    return BC_WAIT


@shield
async def bc_recv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text or ""
    if not txt:
        return BC_WAIT
    sent = 0
    for k, u in USERS.items():
        if u["status"] == "approved":
            r = await safe_send(ctx.bot, int(k), f"{BRAND} · broadcast\n\n{txt}")
            if r:
                sent += 1
    await safe_reply(
        update.message,
        f"sent · {sent} {plural(sent, 'user')}",
        reply_markup=reply_kb(update.effective_user.id),
    )
    return ConversationHandler.END


@shield
async def bc_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "cancelled", reply_markup=reply_kb(update.effective_user.id))
    return ConversationHandler.END


@shield
async def a_nuke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")
    for sid, sup in list(SUPERVISORS.items()):
        try:
            await sup.stop(ctx.bot)
        except Exception:
            pass
        SUPERVISORS.pop(sid, None)
    n = len(SCRIPTS)
    SCRIPTS.clear()
    persist()
    await safe_edit(
        q, f"nuked · {n} {plural(n, 'script')}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("‹ Back", callback_data="admin_root")]]
        ),
    )


@shield
async def a_panel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await _answer(q)
    if q.from_user.id not in ADMIN_IDS:
        return await safe_edit(q, "admin only")

    approved_n = sum(1 for v in USERS.values() if v["status"] == "approved")
    pending_n = sum(1 for v in USERS.values() if v["status"] == "pending")
    banned_n = sum(1 for v in USERS.values() if v["status"] == "banned")
    running_n = sum(1 for s in SUPERVISORS.values() if s.running())

    txt = (
        banner("admin") + "\n\n"
        f"  users      {approved_n}\n"
        f"  pending    {pending_n}\n"
        f"  banned     {banned_n}\n"
        f"  running    {running_n}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Users", callback_data="a_users"),
         InlineKeyboardButton("Pending", callback_data="a_pending")],
        [InlineKeyboardButton("Running", callback_data="a_running"),
         InlineKeyboardButton("Banned", callback_data="a_banned")],
        [InlineKeyboardButton("Broadcast", callback_data="a_broadcast"),
         InlineKeyboardButton("✕ Nuke All", callback_data="a_nuke")],
    ])
    await safe_edit(q, txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@shield
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await safe_reply(update.message, "usage: `/ban <user_id>`")
    uid = int(parts[1])
    if ban(uid):
        killed = 0
        for sid, sup in list(SUPERVISORS.items()):
            if sup.uid == uid:
                try:
                    await sup.stop(ctx.bot)
                except Exception:
                    pass
                SUPERVISORS.pop(sid, None)
                killed += 1
        await safe_send(ctx.bot, uid, "access revoked")
        await safe_reply(
            update.message,
            f"banned · `{uid}` · killed {killed}",
            reply_markup=reply_kb(update.effective_user.id),
        )


@shield
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await safe_reply(update.message, "usage: `/unban <user_id>`")
    uid = int(parts[1])
    if unban(uid):
        await safe_send(ctx.bot, uid, "access restored — /start again", reply_markup=reply_kb(uid))
        await safe_reply(
            update.message, f"unbanned · `{uid}`",
            reply_markup=reply_kb(update.effective_user.id),
        )


# ==================== MAIN ====================
async def watchdog(bot):
    while True:
        try:
            await asyncio.sleep(WATCHDOG)
            for sid, sup in list(SUPERVISORS.items()):
                if not sup.auto or sup._stop or sup._healing:
                    continue
                if not sup.running():
                    sup.restarts += 1
                    sup._w(f"watchdog revive #{sup.restarts}")
                    await sup.start(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("[WD] %s", e)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Start"),
        BotCommand("diag", "Diagnose environment"),
        BotCommand("ban", "Ban user"),
        BotCommand("unban", "Unban user"),
    ])
    asyncio.create_task(watchdog(app.bot))

    revived = 0
    for sid, s in list(SCRIPTS.items()):
        if not s.get("autorestart", True) or not is_ok(s["user_id"]):
            continue
        p = Path(s["path"])
        if not p.exists():
            continue
        sup = ScriptSupervisor(sid, s["user_id"], p, True, env=s.get("env", {}))
        SUPERVISORS[sid] = sup
        try:
            await sup.start(app.bot)
            revived += 1
        except Exception:
            pass
    if revived:
        log.info("revived %d script(s)", revived)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))

    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_UPLOAD]), up_entry)],
        states={UP_WAIT: [MessageHandler(filters.Document.ALL, up_recv)]},
        fallbacks=[CommandHandler("cancel", up_cancel)],
        per_message=False,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_entry, pattern="^sedit_")],
        states={EDIT_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_recv)]},
        fallbacks=[CommandHandler("cancel", edit_cancel)],
        per_message=False,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(env_entry, pattern="^senv_")],
        states={ENV_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, env_recv)]},
        fallbacks=[CommandHandler("cancel", env_cancel)],
        per_message=False,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(bc_entry, pattern="^a_broadcast$")],
        states={BC_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_recv)]},
        fallbacks=[CommandHandler("cancel", bc_cancel)],
        per_message=False,
    ))

    app.add_handler(MessageHandler(filters.Text([BTN_SCRIPTS]), on_deploy))
    app.add_handler(MessageHandler(filters.Text([BTN_LOGS]), on_logs))
    app.add_handler(MessageHandler(filters.Text([BTN_STATS]), on_stats))
    app.add_handler(MessageHandler(filters.Text([BTN_STOP]), on_stop_all))
    app.add_handler(MessageHandler(filters.Text([BTN_ADMIN]), on_admin_panel))

    app.add_handler(CallbackQueryHandler(deploy_list, pattern="^deploy_list$"))
    app.add_handler(CallbackQueryHandler(script_view, pattern="^sview_"))
    app.add_handler(CallbackQueryHandler(s_start, pattern="^sstart_"))
    app.add_handler(CallbackQueryHandler(s_stop, pattern="^sstop_"))
    app.add_handler(CallbackQueryHandler(s_restart, pattern="^srestart_"))
    app.add_handler(CallbackQueryHandler(s_auto, pattern="^sauto_"))
    app.add_handler(CallbackQueryHandler(s_del, pattern="^sdel_"))
    app.add_handler(CallbackQueryHandler(s_log, pattern="^slog_"))
    app.add_handler(CallbackQueryHandler(s_code, pattern="^scode_"))

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
    log.info("PYHOST v4 online")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
