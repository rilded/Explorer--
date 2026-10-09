# -*- coding: utf-8 -*-
import sys
import os
import re
import shutil
import string
import ctypes
from ctypes import wintypes
import stat
import time
import threading
import json
import hashlib
import zipfile
import tarfile
import tempfile
import fnmatch
import subprocess
import queue
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    import psutil
    HAS_PSUTIL = True
except Exception:          # в WinPE psutil может отсутствовать или не загрузиться
    psutil = None
    HAS_PSUTIL = False


if psutil is None:
    # ----------------------------------------------------------------------
    # Запасной слой для работы с процессами без psutil (WinPE).
    # Реализует только то, что нужно проводнику: список процессов
    # (pid / имя / путь к exe), завершение процесса. Через ctypes + Toolhelp32.
    # ----------------------------------------------------------------------
    import types as _types

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    class _TimeoutExpired(Exception):
        pass

    if os.name == "nt":
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _TH32CS_SNAPPROCESS = 0x2
        _PROCESS_QUERY_LIMITED = 0x1000
        _PROCESS_TERMINATE = 0x1
        _SYNCHRONIZE = 0x100000
        _INVALID_HANDLE = ctypes.c_void_p(-1).value

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]

        _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        _k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        _k32.OpenProcess.restype = ctypes.c_void_p
        _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _k32.CloseHandle.argtypes = [ctypes.c_void_p]
        _k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        _k32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
        _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]

        def _fb_snapshot():
            """[(pid, name)] всех процессов."""
            out = []
            snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
            if not snap or snap == _INVALID_HANDLE:
                return out
            try:
                pe = _PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
                while ok:
                    out.append((int(pe.th32ProcessID), pe.szExeFile))
                    ok = _k32.Process32NextW(snap, ctypes.byref(pe))
            finally:
                _k32.CloseHandle(snap)
            return out

        def _fb_exe(pid):
            h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buf))
                if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return buf.value
                return ""
            finally:
                _k32.CloseHandle(h)

        def _fb_terminate(pid):
            h = _k32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
            if not h:
                err = ctypes.get_last_error()
                if err == 87:           # ERROR_INVALID_PARAMETER — процесса уже нет
                    raise _NoSuchProcess(pid)
                raise _AccessDenied(pid)
            try:
                if not _k32.TerminateProcess(h, 1):
                    raise _AccessDenied(pid)
                return h
            except Exception:
                _k32.CloseHandle(h)
                raise
    else:
        def _fb_snapshot():
            return []

        def _fb_exe(pid):
            return ""

        def _fb_terminate(pid):
            raise _AccessDenied(pid)

    class _FbProc:
        def __init__(self, pid, name="", want_exe=False):
            self.pid = pid
            self.info = {"pid": pid, "name": name, "exe": None, "cmdline": []}
            if want_exe:
                self.info["exe"] = _fb_exe(pid)

        def open_files(self):
            return []               # без psutil файлы-дескрипторы не перечислить

        def terminate(self):
            h = _fb_terminate(self.pid)
            if h:
                _k32.CloseHandle(h)

        kill = terminate

        def wait(self, timeout=None):
            return None

    def _fb_process_iter(attrs=None):
        want_exe = bool(attrs) and "exe" in attrs
        for pid, name in _fb_snapshot():
            yield _FbProc(pid, name, want_exe)

    def _fb_Process(pid):
        for p, name in _fb_snapshot():
            if p == pid:
                return _FbProc(pid, name, True)
        raise _NoSuchProcess(pid)

    psutil = _types.SimpleNamespace(
        process_iter=_fb_process_iter, Process=_fb_Process,
        NoSuchProcess=_NoSuchProcess, AccessDenied=_AccessDenied,
        TimeoutExpired=_TimeoutExpired)


try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
    TkBase = TkinterDnD.Tk
except ImportError:
    HAS_DND = False
    DND_FILES = None
    TkBase = tk.Tk


# ===========================================================================
# Константы / конфиг "плавности"
# ===========================================================================
DARK = {
    "bg":         "#1e1e1e",
    "fg":         "#d4d4d4",
    "hidden_fg":  "#8a8a8a",
    "busy_fg":    "#ff8080",
    "input_bg":   "#2d2d2d",
    "border":     "#3c3c3c",
    "select_bg":  "#094771",
    "select_fg":  "#ffffff",
    "hover":      "#2a2d2e",
    "list_bg":    "#252526",
    "muted":      "#808080",
    "active":     "#0e639c",
}

ARCHIVE_EXTS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")

# ---- тюнинг плавности ----
FPS              = 140
FRAME_MS         = max(1, 1000 // FPS)   # ~16 мс
INSERT_BATCH     = 400                    # максимум строк за один кадр
RENDER_BUDGET    = 0.008                  # сек. на вставку строк за кадр (UI не замирает)
HOVER_THROTTLE   = 1.0 / FPS              # не чаще 60 раз в секунду
WHEEL_UNITS      = 3                      # строк за один "щелчок" колеса
ADDR_DEBOUNCE_MS = 120

# ---- большие папки / автообновление ----
VIRTUAL_THRESHOLD = 4000      # больше строк — включается виртуальный список
FS_DEBOUNCE_MS    = 350       # пауза перед автообновлением после изменений на диске
FS_DEBOUNCE_BUSY  = 1500      # то же, пока идёт копирование/удаление
BUSY_MAX_FILES    = 3000      # сколько файлов папки проверять на "занятость"
BUSY_MAX_QUERIES  = 400       # лимит запросов Restart Manager за один скан
VIEW_CACHE_MAX    = 300       # сколько папок помнить позицию прокрутки/выделение

# ---- DPI (значения пересчитываются в init_dpi) ----
UI_SCALE      = 1.0
ROW_HEIGHT    = 22
HEADER_HEIGHT = 22
ICON_SIZE     = 16
HANDLE_W      = 6
_COL_BASE     = {"size": 90, "type": 100, "mtime": 140}
COL_KEYS      = ("size", "type", "mtime")
COL_DEFAULT   = dict(_COL_BASE)
COL_MIN       = 40
NAME_MIN_W    = 120
NAME_DEFAULT_W = 280


def S(x):
    """Пиксели с учётом масштаба экрана."""
    return int(round(x * UI_SCALE))


def enable_dpi_awareness():
    """Вызывать ДО создания окна Tk — иначе Windows растягивает окно и оно мылится."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def init_dpi(root):
    """Пересчитать все размеры в пикселях под текущий масштаб."""
    global UI_SCALE, ROW_HEIGHT, HEADER_HEIGHT, ICON_SIZE, HANDLE_W
    global COL_DEFAULT, COL_MIN, NAME_MIN_W, NAME_DEFAULT_W
    try:
        UI_SCALE = max(1.0, min(4.0, root.winfo_fpixels("1i") / 96.0))
    except Exception:
        UI_SCALE = 1.0
    ROW_HEIGHT = S(22)
    HEADER_HEIGHT = S(22)
    HANDLE_W = S(6)
    COL_DEFAULT = {k: S(v) for k, v in _COL_BASE.items()}
    COL_MIN = S(40)
    NAME_MIN_W = S(120)
    NAME_DEFAULT_W = S(280)
    ICON_SIZE = S(16)
    if os.name == "nt":
        try:
            m = ctypes.windll.user32.GetSystemMetrics(49)    # SM_CXSMICON
            if 8 <= m <= 64:
                ICON_SIZE = m
        except Exception:
            pass


# ---- длинные пути (>260 символов) ----
def lp(path):
    """Длинные пути Windows (префикс extended-length); короткие не меняет."""
    if os.name != "nt" or not path:
        return path
    if path.startswith("\\\\?\\"):
        return path
    try:
        ap = os.path.abspath(path)
    except Exception:
        return path
    if len(ap) < 240:
        return path
    if ap.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def parent_dir(path):
    """Родитель папки; для корня диска возвращает его же."""
    p = os.path.dirname(path.rstrip("\\/"))
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    return p or path

# ===========================================================================
# WinPE / окружение
# ===========================================================================
def is_winpe() -> bool:
    """Пытается определить Windows Preinstallation Environment."""
    if os.name != "nt":
        return False
    try:
        import winreg
        try:
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SYSTEM\CurrentControlSet\Control\MiniNT"):
                return True
        except OSError:
            pass
    except ImportError:
        pass
    # Fallback: X:\ + нет C:\Windows
    if os.environ.get("SystemDrive", "").rstrip(":").upper() == "X":
        if not os.path.isdir(r"C:\Windows"):
            return True
    return False


WINPE = is_winpe()


def _get_app_dir() -> str:
    """Ищет первое доступное для записи место под настройки."""
    candidates = [
        os.environ.get("APPDATA"),
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("TEMP"),
        os.path.dirname(os.path.abspath(sys.argv[0])),
        tempfile.gettempdir(),
    ]
    for base in candidates:
        if not base:
            continue
        try:
            path = os.path.join(base, "PyExplorer")
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".wprobe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            return path
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), "PyExplorer")


APP_DIR = _get_app_dir()
os.makedirs(APP_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")


# ===========================================================================
# Утилиты
# ===========================================================================
def human_size(n):
    if n is None:
        return ""
    if n < 1024:
        return f"{n} Б"
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} ПБ"


def fmt_mtime(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def file_type_name(path, is_dir):
    if is_dir:
        return "Папка"
    ext = os.path.splitext(path)[1].lower()
    return (ext[1:].upper() + "-файл") if ext else "Файл"


def parse_size(text):
    text = text.strip().lower().replace(",", ".")
    if not text:
        return None
    m = re.match(r"^([\d.]+)\s*([a-zа-я]*)$", text)
    if not m:
        return -1
    try:
        val = float(m.group(1))
    except ValueError:
        return -1
    unit = m.group(2).strip()
    mult = 1
    if unit in ("k", "kb", "к", "кб"):
        mult = 1024
    elif unit in ("m", "mb", "м", "мб"):
        mult = 1024 ** 2
    elif unit in ("g", "gb", "г", "гб"):
        mult = 1024 ** 3
    elif unit in ("t", "tb", "т", "тб"):
        mult = 1024 ** 4
    elif unit != "":
        return -1
    return int(val * mult)


def calc_dir_size(path, stop_flag=None):
    total = 0
    try:
        for root, _dirs, files in os.walk(lp(path)):
            if stop_flag and stop_flag():
                break
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("Не удалось сохранить настройки:", e)


def force_kill_pid(pid) -> bool:
    """Завершить процесс напрямую через WinAPI — работает и без psutil (WinPE)."""
    if os.name != "nt":
        try:
            os.kill(pid, 9)
            return True
        except OSError:
            return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x0001, False, pid)      # PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# --- классификация ошибок удаления -----------------------------------------
ERROR_ACCESS_DENIED     = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION    = 33
EXEC_EXTS = (".exe", ".dll", ".sys", ".scr", ".ocx", ".cpl", ".com", ".msi")


def os_error_code(e):
    """Код Windows-ошибки (winerror) либо errno."""
    code = getattr(e, "winerror", None)
    if code is None:
        code = getattr(e, "errno", None)
    return code


def classify_delete_error(e):
    """'busy'   — файл занят процессом (32/33);
       'denied' — отказано в доступе (5 / EACCES / EPERM);
       'other'  — всё остальное."""
    code = os_error_code(e)
    if code in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
        return "busy"
    if code == ERROR_ACCESS_DENIED or (
            os.name != "nt" and code in (1, 13)) or (
            isinstance(e, PermissionError) and code is None):
        return "denied"
    return "other"


def describe_os_error(e):
    """Настоящий текст ошибки ОС (с кодом и путём), без домыслов."""
    text = str(e).strip()
    return text or e.__class__.__name__


def is_exec_path(path):
    return bool(path) and path.lower().endswith(EXEC_EXTS)


def entry_is_hidden(entry, st=None):
    """Скрытый/системный по DirEntry — без лишних системных вызовов."""
    if entry.name.startswith("."):
        return True
    try:
        if st is None:
            st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
    except (OSError, AttributeError):
        return False


def is_archive(path):
    lower = path.lower()
    return any(lower.endswith(e) for e in ARCHIVE_EXTS)


def safe_startfile(path) -> bool:
    """os.startfile с фолбэком; безопасно в WinPE (нет ассоциаций)."""
    if os.name != "nt":
        return False
    try:
        os.startfile(path)  # noqa
        return True
    except OSError:
        pass
    try:
        subprocess.Popen(
            ["rundll32.exe", "shell32.dll,OpenAs_RunDLL", path],
            close_fds=True)
        return True
    except Exception:
        return False


def open_with_dialog(path):
    # сначала пробуем ассоциацию, потом диалог "Открыть с помощью"
    if safe_startfile(path):
        return True
    return False


# ===========================================================================
# Админ
# ===========================================================================
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    if is_admin():
        return False
    if WINPE:
        # В WinPE мы и так обычно SYSTEM; ShellExecuteW "runas" может вешать процесс
        return False
    params = " ".join(f'"{a}"' for a in sys.argv)
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
        return True
    except Exception as e:
        print("Не удалось запросить права:", e)
        return False


# ===========================================================================
# Кто держит файл: Restart Manager (rstrtmgr.dll) — точнее и быстрее, чем
# перебор open_files() у всех процессов. Если API недоступно (часть сборок
# WinPE) — rm_get_processes() вернёт None, и вызывающий код откатится на psutil.
# ===========================================================================
_RM = None
if os.name == "nt":
    try:
        class _RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", wintypes.DWORD),
                        ("ProcessStartTime", wintypes.FILETIME)]

        class _RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [("Process", _RM_UNIQUE_PROCESS),
                        ("strAppName", ctypes.c_wchar * 256),
                        ("strServiceShortName", ctypes.c_wchar * 64),
                        ("ApplicationType", ctypes.c_int),
                        ("AppStatus", wintypes.ULONG),
                        ("TSSessionId", wintypes.DWORD),
                        ("bRestartable", wintypes.BOOL)]

        _RM = ctypes.WinDLL("rstrtmgr", use_last_error=True)
        _RM.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD, wintypes.LPWSTR]
        _RM.RmStartSession.restype = wintypes.DWORD
        _RM.RmRegisterResources.argtypes = [
            wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
            wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p]
        _RM.RmRegisterResources.restype = wintypes.DWORD
        _RM.RmGetList.argtypes = [
            wintypes.DWORD, ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_RM_PROCESS_INFO),
            ctypes.POINTER(wintypes.DWORD)]
        _RM.RmGetList.restype = wintypes.DWORD
        _RM.RmEndSession.argtypes = [wintypes.DWORD]
        _RM.RmEndSession.restype = wintypes.DWORD
    except Exception:
        _RM = None

_RM_ERROR_MORE_DATA = 234


def rm_get_processes(files):
    """[(pid, имя)] процессов, использующих хотя бы один из files.
    [] — никто не использует; None — Restart Manager недоступен/ошибка."""
    if _RM is None or not files:
        return None
    session = wintypes.DWORD(0)
    key = ctypes.create_unicode_buffer(64)
    try:
        if _RM.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return None
    except Exception:
        return None
    try:
        arr = (wintypes.LPCWSTR * len(files))(*[lp(f) for f in files])
        if _RM.RmRegisterResources(session.value, len(files), arr,
                                   0, None, 0, None) != 0:
            return None
        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reasons = wintypes.DWORD(0)
        ret = _RM.RmGetList(session.value, ctypes.byref(needed),
                            ctypes.byref(count), None, ctypes.byref(reasons))
        if ret == 0:
            return []
        if ret != _RM_ERROR_MORE_DATA:
            return None
        for _attempt in range(3):
            n = needed.value + 4
            infos = (_RM_PROCESS_INFO * n)()
            count = wintypes.UINT(n)
            ret = _RM.RmGetList(session.value, ctypes.byref(needed),
                                ctypes.byref(count), infos, ctypes.byref(reasons))
            if ret == 0:
                return [(int(infos[i].Process.dwProcessId), infos[i].strAppName)
                        for i in range(count.value)]
            if ret != _RM_ERROR_MORE_DATA:
                return None
        return None
    except Exception:
        return None
    finally:
        try:
            _RM.RmEndSession(session.value)
        except Exception:
            pass


def rm_busy_map(files, max_queries=BUSY_MAX_QUERIES):
    """{normcase(путь): [(pid, имя)]} для занятых файлов (деление пополам:
    большинство папок — один запрос). None, если Restart Manager недоступен."""
    out = {}
    budget = [max_queries]

    def rec(group):
        if not group or budget[0] <= 0:
            return
        budget[0] -= 1
        procs = rm_get_processes(group)
        if procs is None:
            raise RuntimeError("rm unavailable")
        if not procs:
            return
        if len(group) == 1:
            out[os.path.normcase(group[0])] = procs
            return
        mid = len(group) // 2
        rec(group[:mid])
        rec(group[mid:])

    try:
        rec(list(files))
    except RuntimeError:
        return None
    return out


def proc_dict(pid, name=""):
    """Описание процесса в формате, который ждут остальные части программы."""
    exe, cmd = "", ""
    try:
        p = psutil.Process(pid)
        if HAS_PSUTIL:
            name = p.name() or name
            try:
                exe = p.exe() or ""
            except Exception:
                exe = ""
            try:
                cmd = " ".join(p.cmdline())[:200]
            except Exception:
                cmd = ""
        else:
            exe = p.info.get("exe") or ""
    except Exception:
        pass
    return {"pid": pid, "name": name or "?", "exe": exe, "cmdline": cmd}


def kill_processes(procs):
    """Завершить процессы без GUI. -> (сколько завершено, [тексты ошибок])."""
    killed, errors = 0, []
    me = os.getpid()
    for p in procs:
        pid = p["pid"]
        if pid == me:
            errors.append(f"{p['name']} ({pid}): это сам проводник")
            continue
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            killed += 1
        except psutil.NoSuchProcess:
            continue
        except Exception as e:
            if force_kill_pid(pid):
                killed += 1
            else:
                errors.append(f"{p['name']} ({pid}): {e}")
    return killed, errors


# ===========================================================================
# Иконки
# ===========================================================================
if os.name == "nt":
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _user32  = ctypes.WinDLL("user32",  use_last_error=True)
    _gdi32   = ctypes.WinDLL("gdi32",   use_last_error=True)
    _VP = ctypes.c_void_p

    class _SHFILEINFO(ctypes.Structure):
        _fields_ = [
            ("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
            ("dwAttributes", wintypes.DWORD),
            ("szDisplayName", ctypes.c_wchar * 260),
            ("szTypeName", ctypes.c_wchar * 80)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD)]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                    ("bmiColors", wintypes.DWORD * 3)]

    _shell32.SHGetFileInfoW.restype = ctypes.c_void_p
    _shell32.SHGetFileInfoW.argtypes = [
        ctypes.c_wchar_p, wintypes.DWORD,
        ctypes.POINTER(_SHFILEINFO), ctypes.c_uint, ctypes.c_uint]

    # Без argtypes ctypes передаёт Python-int как 32-битный C int, и на 64-битной
    # Windows дескрипторы (HICON/HDC/HBITMAP) падают с OverflowError.
    _user32.GetDC.restype = _VP
    _user32.GetDC.argtypes = [_VP]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.ReleaseDC.argtypes = [_VP, _VP]
    _user32.DrawIconEx.restype = wintypes.BOOL
    _user32.DrawIconEx.argtypes = [_VP, ctypes.c_int, ctypes.c_int, _VP,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                   _VP, wintypes.UINT]
    _user32.DestroyIcon.restype = wintypes.BOOL
    _user32.DestroyIcon.argtypes = [_VP]
    _gdi32.CreateDIBSection.restype = _VP
    _gdi32.CreateDIBSection.argtypes = [_VP, ctypes.POINTER(_BITMAPINFO),
                                        wintypes.UINT, ctypes.POINTER(_VP),
                                        _VP, wintypes.DWORD]
    _gdi32.CreateCompatibleDC.restype = _VP
    _gdi32.CreateCompatibleDC.argtypes = [_VP]
    _gdi32.SelectObject.restype = _VP
    _gdi32.SelectObject.argtypes = [_VP, _VP]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.DeleteDC.argtypes = [_VP]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.DeleteObject.argtypes = [_VP]

    SHGFI_ICON, SHGFI_SMALLICON = 0x100, 0x1
    SHGFI_USEFILEATTRIBUTES = 0x10
    FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_DIRECTORY = 0x80, 0x10
    DI_NORMAL = 0x3

    def _hicon_to_pil(hicon, size=16):
        from PIL import Image
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize        = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth       = size
        bmi.bmiHeader.biHeight      = -size
        bmi.bmiHeader.biPlanes      = 1
        bmi.bmiHeader.biBitCount    = 32
        bmi.bmiHeader.biCompression = 0
        hdc = _user32.GetDC(None)
        if not hdc:
            return None
        try:
            bits = ctypes.c_void_p()
            hbmp = _gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                           ctypes.byref(bits), None, 0)
            if not hbmp:
                return None
            try:
                ctypes.memset(bits, 0, size * size * 4)
                memdc = _gdi32.CreateCompatibleDC(hdc)
                old = _gdi32.SelectObject(memdc, hbmp)
                try:
                    _user32.DrawIconEx(memdc, 0, 0, hicon, size, size, 0,
                                       None, DI_NORMAL)
                finally:
                    _gdi32.SelectObject(memdc, old)
                    _gdi32.DeleteDC(memdc)
                raw = ctypes.string_at(bits, size * size * 4)
                return Image.frombuffer("RGBA", (size, size), raw,
                                        "raw", "BGRA", 0, 1).copy()
            finally:
                _gdi32.DeleteObject(hbmp)
        finally:
            _user32.ReleaseDC(None, hdc)


class IconCache:
    _SPECIFIC = {".exe", ".lnk", ".ico", ".url", ".msi", ".scr", ".cpl"}

    def __init__(self):
        self._cache = {}
        self._available = False
        self._root = None
        self.on_ready = None       # fn(path, photo) — в главном потоке
        self.wanted = None         # fn(path) -> bool: ещё нужна ли иконка
        self._q = queue.Queue()
        self._pending = set()
        self._worker = None
        if os.name != "nt":
            return
        try:
            from PIL import Image, ImageTk  # noqa
            self._ImageTk = ImageTk
            self._available = True
        except ImportError:
            pass

    def attach(self, root, on_ready, wanted):
        self._root = root
        self.on_ready = on_ready
        self.wanted = wanted

    def for_file(self, path, is_dir=False, is_drive=False):
        if not self._available:
            return None
        if is_drive:
            return self._get(self.drive_key(path), path, False, True)
        if is_dir:
            return self._get("__folder__", "folder", True, True)
        ext = os.path.splitext(path)[1].lower()
        if ext in self._SPECIFIC:
            # у exe/lnk/ico свои иконки: сразу отдаём типовую (без обращения
            # к файлу), настоящую достаём в фоне и подменяем на месте
            if path in self._cache:
                real = self._cache[path]
                return real if real else self._placeholder(ext)
            self._request(path)
            return self._placeholder(ext)
        key = ext or "__noext__"
        query = ("file" + ext) if ext else "file"
        return self._get(key, query, True, False)

    def _placeholder(self, ext):
        return self._get("ph:" + ext, "file" + ext, True, False)

    # ---- фоновая подгрузка настоящих иконок ----
    def _request(self, path):
        if self._root is None or path in self._pending:
            return
        self._pending.add(path)
        self._q.put(path)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, daemon=True)
            self._worker.start()

    def _work(self):
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        while True:
            try:
                path = self._q.get(timeout=30)
            except queue.Empty:
                return                      # поток сам завершится, перезапустится при надобности
            pil = None
            try:
                if self.wanted is not None and not self.wanted(path):
                    self._pending.discard(path)
                    continue
                pil = self._extract(path, False, False)
            except Exception:
                pil = None
            try:
                self._root.after(0, self._deliver, path, pil)
            except Exception:
                return

    def _deliver(self, path, pil):
        self._pending.discard(path)
        photo = None
        if pil is not None:
            try:
                photo = self._ImageTk.PhotoImage(pil)
            except Exception:
                photo = None
        self._cache[path] = photo
        if photo is not None and self.on_ready is not None:
            try:
                self.on_ready(path, photo)
            except Exception:
                pass

    def prune_specific(self, keep_dirs):
        """Забыть иконки файлов из папок, которые уже не открыты (память)."""
        for key in list(self._cache):
            if key.startswith(("ph:", "__")) or key.count(os.sep) == 0:
                continue
            if os.path.normcase(os.path.normpath(os.path.dirname(key))) not in keep_dirs:
                self._cache.pop(key, None)

    # --- диски: SHGetFileInfo по реальному пути может "зависать" на пустых
    # приводах и недоступных сетевых дисках, поэтому PIL-картинку получаем
    # в фоне, а PhotoImage создаём уже в главном потоке.
    @staticmethod
    def drive_key(path):
        return "__drive__:" + path[:2]

    def cached_drive(self, path):
        return self._cache.get(self.drive_key(path))

    def drive_pil(self, path):
        """Вызывать из рабочего потока. Tk здесь не трогаем."""
        if not self._available:
            return None
        return self._extract(path, False, True)

    def drive_photo(self, path, pil):
        """Вызывать из главного потока."""
        key = self.drive_key(path)
        if key in self._cache:
            return self._cache[key]
        photo = None
        if pil is not None:
            try:
                photo = self._ImageTk.PhotoImage(pil)
            except Exception:
                photo = None
        self._cache[key] = photo
        return photo

    def _get(self, key, query, attrs_query, is_dir):
        if key in self._cache:
            return self._cache[key]
        pil = self._extract(query, attrs_query, is_dir)
        if pil is None:
            self._cache[key] = None
            return None
        try:
            photo = self._ImageTk.PhotoImage(pil)
        except Exception:
            self._cache[key] = None
            return None
        self._cache[key] = photo
        return photo

    def _extract(self, query, attrs_query, is_dir):
        info = _SHFILEINFO()
        flags = SHGFI_ICON | SHGFI_SMALLICON
        attrs = 0
        if attrs_query:
            flags |= SHGFI_USEFILEATTRIBUTES
            attrs = FILE_ATTRIBUTE_DIRECTORY if is_dir else FILE_ATTRIBUTE_NORMAL
        try:
            _shell32.SHGetFileInfoW(query, attrs, ctypes.byref(info),
                                    ctypes.sizeof(info), flags)
        except Exception:
            return None
        if not info.hIcon:
            return None
        try:
            return _hicon_to_pil(info.hIcon, ICON_SIZE)
        except Exception:
            return None            # сбой иконки не должен ломать отрисовку списка
        finally:
            try:
                _user32.DestroyIcon(info.hIcon)
            except Exception:
                pass


# ===========================================================================
# Стили
# ===========================================================================
def setup_styles(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".",
                    background=DARK["bg"], foreground=DARK["fg"],
                    fieldbackground=DARK["input_bg"],
                    bordercolor=DARK["border"], lightcolor=DARK["border"],
                    darkcolor=DARK["border"])
    style.configure("TFrame", background=DARK["bg"])
    style.configure("TLabel", background=DARK["bg"], foreground=DARK["fg"])
    style.configure("TButton",
                    background=DARK["input_bg"], foreground=DARK["fg"],
                    bordercolor=DARK["border"], focuscolor=DARK["select_bg"],
                    padding=(6, 3), relief="flat")
    style.map("TButton",
              background=[("pressed", DARK["select_bg"]),
                          ("active", DARK["border"])],
              foreground=[("pressed", DARK["select_fg"])])
    style.configure("TEntry",
                    fieldbackground=DARK["input_bg"], foreground=DARK["fg"],
                    insertcolor=DARK["fg"], bordercolor=DARK["border"], padding=3)
    style.configure("Treeview",
                    background=DARK["list_bg"], foreground=DARK["fg"],
                    fieldbackground=DARK["list_bg"],
                    bordercolor=DARK["border"], rowheight=ROW_HEIGHT, borderwidth=0)
    style.map("Treeview",
              background=[("selected", DARK["select_bg"])],
              foreground=[("selected", DARK["select_fg"])])
    style.configure("Treeview.Heading",
                    background=DARK["input_bg"], foreground=DARK["fg"],
                    bordercolor=DARK["border"])
    style.configure("TPanedwindow", background=DARK["bg"])
    style.configure("Sash", background=DARK["border"], sashthickness=4)
    style.configure("Vertical.TScrollbar",
                    background=DARK["input_bg"], troughcolor=DARK["bg"],
                    bordercolor=DARK["border"], arrowcolor=DARK["fg"], gripcount=0)
    style.configure("Horizontal.TProgressbar",
                    background=DARK["active"], troughcolor=DARK["input_bg"],
                    bordercolor=DARK["border"], lightcolor=DARK["active"],
                    darkcolor=DARK["active"])
    root.configure(bg=DARK["bg"])


# ===========================================================================
# Рабочие потоки
# ===========================================================================
class DirLoader(threading.Thread):
    """Читает содержимое папки в фоне (os.scandir), умеет отменяться и
    сообщать прогресс. Колбэки вызываются из рабочего потока — вызывающий
    обязан сам перебросить их в главный поток (after)."""
    PROGRESS_EVERY = 0.15     # сек. между сообщениями о прогрессе

    def __init__(self, path, show_hidden, cancel_event, on_progress, on_done):
        super().__init__(daemon=True)
        self.path = path
        self.show_hidden = show_hidden
        self.cancel = cancel_event
        self.on_progress = on_progress
        self.on_done = on_done

    def run(self):
        entries = []
        err = None            # (kind, text) ; kind: denied | missing | other
        try:
            it = os.scandir(lp(self.path))
        except PermissionError:
            self._done([], ("denied", "Нет доступа: " + self.path)); return
        except (FileNotFoundError, NotADirectoryError):
            self._done([], ("missing", "Путь не найден или не папка: " + self.path)); return
        except OSError as e:
            code = os_error_code(e)
            kind = "missing" if code in (3, 53, 67, 123, 21, 2, 20) else "other"
            self._done([], (kind, "Ошибка: " + describe_os_error(e))); return

        last_report = time.monotonic()
        with it:
            while not self.cancel.is_set():
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as e:
                    err = ("other", "Ошибка чтения: " + describe_os_error(e))
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    st, size, mtime = None, 0, 0
                hidden = entry_is_hidden(entry, st)
                if hidden and not self.show_hidden:
                    continue
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                entries.append({"name": entry.name,
                                "full": os.path.join(self.path, entry.name),
                                "is_dir": is_dir, "size": size,
                                "mtime": mtime, "hidden": hidden})
                now = time.monotonic()
                if now - last_report >= self.PROGRESS_EVERY:
                    last_report = now
                    try:
                        self.on_progress(len(entries))
                    except Exception:
                        pass
        if self.cancel.is_set():
            return
        self._done(entries, err)

    def _done(self, entries, err):
        try:
            self.on_done(entries, err)
        except Exception:
            pass


class ProcessSearchWorker(threading.Thread):
    def __init__(self, filepath, callback, quick=False):
        super().__init__(daemon=True)
        self.filepath = filepath
        self.callback = callback
        # quick: только "этот файл — exe запущенного процесса"; без поиска
        # по именам и без перечисления открытых файлов
        self.quick = quick

    @staticmethod
    def _variants(text):
        if not text:
            return set()
        t = text.lower()
        return {t, t.replace("_", " "), t.replace("-", " "),
                t.replace("_", " ").replace("-", " "),
                t.replace("_", "").replace("-", ""),
                t.replace("_", "").replace("-", "").replace(" ", "")}

    @staticmethod
    def _proc_info(proc):
        try:
            info = proc.info
            return {"pid": info.get("pid"), "name": info.get("name") or "?",
                    "exe": info.get("exe") or "",
                    "cmdline": " ".join(info.get("cmdline") or [])[:200]}
        except Exception:
            return {"pid": proc.pid, "name": "?", "exe": "", "cmdline": ""}

    @staticmethod
    def _norm(p):
        try:
            return os.path.normcase(os.path.abspath(p))
        except Exception:
            return ""

    def run(self):
        try:
            result = self._search()
        except Exception as e:
            print("Ошибка поиска процесса:", e)
            result = {"exact": [], "name_match": []}
        self.callback(self.filepath, result)

    def _search(self):
        filepath = os.path.abspath(self.filepath)
        file_norm = self._norm(filepath)
        basename = os.path.basename(filepath)
        stem, _ = os.path.splitext(basename)
        target_variants = self._variants(stem)
        target_variants.add(basename.lower())
        exact, name_match = [], []

        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                info = proc.info
                pname = (info.get("name") or "").lower()
                if not pname:
                    continue
                pexe = info.get("exe") or ""
                cmdline = info.get("cmdline") or []
                if pexe and self._norm(pexe) == file_norm:
                    exact.append(self._proc_info(proc)); continue
                if self.quick:
                    continue
                hit = False
                for arg in cmdline:
                    if arg and self._norm(arg) == file_norm:
                        hit = True; break
                if hit:
                    exact.append(self._proc_info(proc)); continue
                pname_stem, _ = os.path.splitext(pname)
                pexe_base = os.path.basename(pexe) if pexe else ""
                pexe_stem, _ = os.path.splitext(pexe_base)
                if (target_variants & self._variants(pname_stem)) or \
                   (target_variants & self._variants(pexe_stem)):
                    name_match.append(self._proc_info(proc))
            except Exception:
                continue

        # кто держит файл по данным Restart Manager (быстро и точно)
        rm = rm_get_processes([filepath])
        if rm:
            have = {p["pid"] for p in exact}
            for pid, pname in rm:
                if pid not in have and pid != os.getpid():
                    exact.append(proc_dict(pid, pname))
        if not exact and not self.quick and rm is None:
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
                try:
                    for f in proc.open_files():
                        try:
                            if self._norm(f.path) == file_norm:
                                info = self._proc_info(proc)
                                if info not in exact:
                                    exact.append(info)
                                break
                        except OSError:
                            continue
                except Exception:
                    continue

        exact_pids = {p["pid"] for p in exact}
        seen, exact_out = set(), []
        for p in exact:
            if p["pid"] not in seen:
                seen.add(p["pid"]); exact_out.append(p)
        seen, nm_out = set(), []
        for p in name_match:
            if p["pid"] in exact_pids or p["pid"] in seen:
                continue
            seen.add(p["pid"]); nm_out.append(p)
        return {"exact": exact_out, "name_match": nm_out}


class FolderProcessSearchWorker(threading.Thread):
    def __init__(self, folderpath, callback, quick=False):
        super().__init__(daemon=True)
        self.folderpath = folderpath
        self.callback = callback
        self.quick = quick     # только exe запущенных процессов внутри папки

    _variants = staticmethod(ProcessSearchWorker._variants)
    _proc_info = staticmethod(ProcessSearchWorker._proc_info)
    _norm = staticmethod(ProcessSearchWorker._norm)

    def run(self):
        try:
            result = self._search()
        except Exception as e:
            print("Ошибка поиска для папки:", e)
            result = {"exact": [], "name_match": []}
        self.callback(self.folderpath, result)

    def _search(self):
        folder = os.path.abspath(self.folderpath)
        folder_norm = os.path.normcase(folder)
        folder_prefix = folder_norm.rstrip("\\/") + os.sep
        all_files = []
        if not self.quick:
            try:
                for root, _dirs, files in os.walk(folder):
                    for fn in files:
                        all_files.append(os.path.join(root, fn))
            except OSError:
                pass
        file_norms = {self._norm(f) for f in all_files}
        file_norms.discard("")

        folder_name = os.path.basename(folder.rstrip("\\/")).lower()
        folder_stem, _ = os.path.splitext(folder_name)
        folder_variants = self._variants(folder_stem)
        folder_variants.add(folder_name)

        exact, name_match = [], []
        exact_pids = set()

        for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
            try:
                info = proc.info
                pexe = info.get("exe") or ""
                cmdline = info.get("cmdline") or []
                hit = False
                if pexe:
                    pn = self._norm(pexe)
                    if pn and (pn == folder_norm or pn.startswith(folder_prefix)):
                        hit = True
                if not hit and file_norms:
                    for arg in cmdline:
                        if arg and self._norm(arg) in file_norms:
                            hit = True; break
                if hit:
                    p = self._proc_info(proc)
                    exact.append(p); exact_pids.add(p["pid"])
            except Exception:
                continue

        if file_norms:
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
                if proc.pid in exact_pids:
                    continue
                try:
                    for f in proc.open_files():
                        try:
                            if self._norm(f.path) in file_norms:
                                p = self._proc_info(proc)
                                if p["pid"] not in exact_pids:
                                    exact.append(p); exact_pids.add(p["pid"])
                                break
                        except OSError:
                            continue
                except Exception:
                    continue

        for proc in ([] if self.quick else psutil.process_iter(["pid", "name"])):
            if proc.pid in exact_pids:
                continue
            try:
                pname = (proc.info.get("name") or "").lower()
                if not pname:
                    continue
                pname_stem, _ = os.path.splitext(pname)
                if (folder_variants & self._variants(pname_stem)) or \
                   (pname in folder_variants):
                    name_match.append(self._proc_info(proc))
            except Exception:
                continue

        seen, nm = set(), []
        for p in name_match:
            if p["pid"] not in seen and p["pid"] not in exact_pids:
                seen.add(p["pid"]); nm.append(p)
        return {"exact": exact, "name_match": nm}


class BusyScanWorker(threading.Thread):
    """Находит занятые файлы папки. Основной способ — Restart Manager
    (один-два запроса на папку); без него — перебор open_files() через psutil."""
    def __init__(self, dir_path, callback):
        super().__init__(daemon=True)
        self.dir_path = dir_path
        self.callback = callback

    def _list_files(self):
        files = []
        try:
            with os.scandir(lp(self.dir_path)) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False):
                            files.append(os.path.join(self.dir_path, e.name))
                    except OSError:
                        continue
                    if len(files) >= BUSY_MAX_FILES:
                        break
        except OSError:
            pass
        return files

    def _psutil_scan(self):
        busy = {}
        if not HAS_PSUTIL:
            return busy
        prefix = os.path.normcase(
            os.path.abspath(self.dir_path)).rstrip("\\/") + os.sep
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                for f in proc.open_files():
                    try:
                        fn = os.path.normcase(os.path.abspath(f.path))
                        if fn.startswith(prefix):
                            busy.setdefault(fn, []).append(
                                (proc.pid, proc.info.get("name") or "?"))
                    except OSError:
                        continue
            except Exception:
                continue
        return busy

    def run(self):
        busy = {}
        try:
            files = self._list_files()
            m = rm_busy_map(files) if files else {}
            busy = m if m is not None else self._psutil_scan()
        except Exception as e:
            print("Ошибка сканирования занятых:", e)
        try:
            self.callback(self.dir_path, busy)
        except Exception:
            pass


# ===========================================================================
# Автообновление списка: ReadDirectoryChangesW (запасной вариант — опрос mtime)
# ===========================================================================
class DirWatcher(threading.Thread):
    """Сообщает callback(), когда в папке что-то изменилось. callback вызывается
    из рабочего потока — вызывающий сам переводит его в главный поток."""
    FILTER = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x40   # имена, атрибуты, размер, запись
    POLL_INTERVAL = 2.0                            # запасной вариант без WinAPI

    def __init__(self, path, callback):
        super().__init__(daemon=True)
        self.path = path
        self.callback = callback
        self._halt = threading.Event()
        self._handle = None
        self._k32 = None

    def stop(self):
        self._halt.set()
        if self._handle and self._k32 is not None:
            try:
                self._k32.CancelIoEx(self._handle, None)   # разбудить блокирующий вызов
            except Exception:
                pass

    def run(self):
        if os.name == "nt":
            try:
                self._run_win()
                return
            except Exception:
                pass
        self._run_poll()

    def _run_win(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p]
        k.ReadDirectoryChangesW.restype = wintypes.BOOL
        k.ReadDirectoryChangesW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            ctypes.c_void_p]
        k.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        invalid = ctypes.c_void_p(-1).value
        # FILE_LIST_DIRECTORY, share read|write|delete, OPEN_EXISTING, BACKUP_SEMANTICS
        h = k.CreateFileW(lp(self.path), 0x0001, 0x7, None, 3, 0x02000000, None)
        if not h or h == invalid:
            raise OSError("cannot open directory for watching")
        self._k32, self._handle = k, h
        buf = ctypes.create_string_buffer(65536)
        ret = wintypes.DWORD(0)
        try:
            while not self._halt.is_set():
                ok = k.ReadDirectoryChangesW(h, buf, len(buf), False, self.FILTER,
                                             ctypes.byref(ret), None, None)
                if self._halt.is_set():
                    break
                self.callback()
                if not ok:          # папка удалена / доступ потерян
                    break
                time.sleep(0.05)
        finally:
            self._handle = None
            k.CloseHandle(h)

    def _run_poll(self):
        last = None
        while not self._halt.wait(self.POLL_INTERVAL):
            try:
                m = os.stat(self.path).st_mtime
            except OSError:
                self.callback()
                return
            if last is not None and m != last:
                self.callback()
            last = m


# ===========================================================================
# Файловые операции в фоне: удаление / копирование / перенос
# ===========================================================================
COPY_CHUNK = 2 * 1024 * 1024


class JobCancelled(Exception):
    pass


class JobAborted(Exception):
    pass


def is_reparse(path):
    """Папка-ссылка (junction / symlink): внутрь не заходим."""
    try:
        st = os.lstat(lp(path))
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def remove_link(path):
    p = lp(path)
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)


class JobState:
    """Разделяемое состояние: пишет рабочий поток, читает интерфейс."""
    def __init__(self, kind):
        self.kind = kind
        self.phase = "scan"          # scan -> run
        self.current = ""
        self.done_bytes = 0
        self.total_bytes = 0
        self.done_items = 0
        self.total_items = 0
        self.deleted = 0
        self.copied = 0
        self.skipped = 0
        self.errors = 0
        self.killed = 0
        self.cancelled = False
        self.finished = False
        self.created = []            # итоговые пути (чтобы выделить после вставки)
        self.error_log = []
        self.started = time.monotonic()


class FileJob(threading.Thread):
    """kind: 'delete' | 'copy' | 'move'.

    ask(what, **kw) — блокирующий колбэк в интерфейс (диалоги):
      'conflict' (src, dst)           -> (action, apply_all) | None
      'error'    (path, text, can_retry) -> 'retry'|'skip'|'skip_all'|'abort'
      'kill'     (path, procs)        -> [процессы для завершения] | None
    """
    def __init__(self, kind, sources, dst_dir, ask):
        super().__init__(daemon=True)
        self.kind = kind
        self.sources = [s for s in sources if s]
        self.dst_dir = dst_dir
        self.ask = ask
        self.state = JobState(kind)
        self._cancel = threading.Event()
        self.skip_all = False
        self.conflict_all = None
        self._proc_tries = {}

    # ------------------------------------------------------------ управление
    def cancel(self):
        self._cancel.set()
        self.state.cancelled = True

    def _check(self):
        if self._cancel.is_set():
            raise JobCancelled()

    def run(self):
        st = self.state
        try:
            if self.kind == "delete":
                self._run_delete()
            else:
                self._run_copy(move=(self.kind == "move"))
        except (JobCancelled, JobAborted):
            st.cancelled = True
        except Exception as e:                     # не роняем поток молча
            st.errors += 1
            st.error_log.append(f"Внутренняя ошибка: {e!r}")
        finally:
            st.finished = True

    # ------------------------------------------------------------ ошибки
    def _report_error(self, path, text, can_retry=True):
        st = self.state
        if self.skip_all:
            st.errors += 1
            st.error_log.append(f"{path}: {text}")
            return "skip"
        r = self.ask("error", path=path, text=text, can_retry=can_retry)
        if r == "retry" and can_retry:
            return "retry"
        if r in ("abort", None):
            raise JobAborted()
        if r == "skip_all":
            self.skip_all = True
        st.errors += 1
        st.error_log.append(f"{path}: {text}")
        return "skip"

    def _try(self, path, fn, is_dir):
        """Выполнить fn(); при ошибке — поиск процесса / вопрос пользователю."""
        while True:
            self._check()
            try:
                fn()
                return True
            except FileNotFoundError:
                return True
            except OSError as e:
                if self._on_delete_error(path, e, is_dir) == "retry":
                    continue
                return False

    def _on_delete_error(self, path, e, is_dir):
        """Решаем по НАСТОЯЩЕЙ ошибке ОС: процесс ищем только если файл занят
        (32/33) либо это запущенный exe/dll (Windows отвечает "отказано")."""
        st = self.state
        kind = classify_delete_error(e)
        failed = getattr(e, "filename", None) or path
        note = ""
        if self._proc_tries.get(path, 0) == 0 and (
                kind == "busy" or (kind == "denied" and is_exec_path(failed))):
            self._proc_tries[path] = 1
            quick = kind != "busy"
            st.current = "Поиск процесса: " + os.path.basename(path)
            action, note = self._find_and_kill(path, is_dir, quick, kind)
            if action == "retry":
                return "retry"
        text = describe_os_error(e)
        if note:
            text = note + "\n\n" + text
        return self._report_error(path, text, can_retry=True)

    def _find_and_kill(self, path, is_dir, quick, kind):
        try:
            w = (FolderProcessSearchWorker(path, None, quick=quick) if is_dir
                 else ProcessSearchWorker(path, None, quick=quick))
            res = w._search()
        except Exception:
            res = {"exact": [], "name_match": []}
        me = os.getpid()
        exact = [p for p in res.get("exact", []) if p["pid"] != me]
        names = [p for p in res.get("name_match", []) if p["pid"] != me]
        st = self.state
        if exact:
            n, errs = kill_processes(exact)
            st.killed += n
            if n:
                time.sleep(0.4)
                return "retry", ""
            return "none", "Не удалось завершить процесс: " + "; ".join(errs)
        if names and not quick:
            chosen = self.ask("kill", path=path, procs=names)
            if chosen:
                n, errs = kill_processes(chosen)
                st.killed += n
                if n:
                    time.sleep(0.4)
                    return "retry", ""
                return "none", "Не удалось завершить процесс: " + "; ".join(errs)
            return "none", ""
        if kind == "busy":
            return "none", ("Объект занят, но процесс, который его держит, "
                            "определить не удалось.")
        return "none", ""

    # ------------------------------------------------------------ удаление
    def _run_delete(self):
        self.state.phase = "run"
        for src in self.sources:
            self._check()
            if not os.path.lexists(lp(src)):
                continue
            self._delete_entry(src)

    @staticmethod
    def _rm_file(path):
        p = lp(path)
        try:
            os.remove(p)
        except PermissionError:
            try:
                os.chmod(p, stat.S_IWRITE)
            except OSError:
                pass
            os.remove(p)

    @staticmethod
    def _rmdir(path):
        p = lp(path)
        try:
            os.rmdir(p)
        except PermissionError:
            try:
                os.chmod(p, stat.S_IWRITE)
            except OSError:
                pass
            os.rmdir(p)

    def _delete_entry(self, path):
        """True — объект удалён целиком."""
        self._check()
        st = self.state
        st.current = path
        p = lp(path)
        try:
            isdir = os.path.isdir(p)
        except OSError:
            isdir = False
        if isdir and is_reparse(path):
            ok = self._try(path, lambda: remove_link(path), True)
            if ok:
                st.deleted += 1
                st.done_items += 1
            return ok
        if not isdir:
            ok = self._try(path, lambda: self._rm_file(path), False)
            if ok:
                st.deleted += 1
                st.done_items += 1
            return ok
        all_ok = True
        try:
            with os.scandir(p) as it:
                names = [e.name for e in it]
        except OSError as e:
            if self._report_error(path, describe_os_error(e), can_retry=False) == "skip":
                return False
            names = []
        for name in names:
            if not self._delete_entry(os.path.join(path, name)):
                all_ok = False
        if not all_ok:
            return False
        st.current = path
        ok = self._try(path, lambda: self._rmdir(path), True)
        if ok:
            st.deleted += 1
            st.done_items += 1
        return ok

    # ------------------------------------------------------------ копирование
    def _scan(self, path):
        tb = ti = 0
        try:
            if not os.path.isdir(lp(path)) or is_reparse(path):
                try:
                    return os.lstat(lp(path)).st_size, 1
                except OSError:
                    return 0, 1
        except OSError:
            return 0, 1
        stack = [path]
        while stack:
            self._check()
            cur = stack.pop()
            self.state.current = cur
            try:
                with os.scandir(lp(cur)) as it:
                    for e in it:
                        ti += 1
                        try:
                            if e.is_dir(follow_symlinks=False) and not is_reparse(
                                    os.path.join(cur, e.name)):
                                stack.append(os.path.join(cur, e.name))
                            else:
                                tb += e.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
            except OSError:
                pass
        return tb, ti + 1

    def _unique_name(self, dst_dir, name, isdir):
        base, ext = (name, "") if isdir else os.path.splitext(name)
        i = 1
        while True:
            cand = os.path.join(dst_dir, f"{base} ({i}){ext}")
            if not os.path.lexists(lp(cand)):
                return cand
            i += 1

    def _resolve_conflict(self, src, dst):
        if self.conflict_all:
            return self.conflict_all
        r = self.ask("conflict", src=src, dst=dst)
        if not r:
            raise JobCancelled()
        action, apply_all = r
        if action not in ("replace", "skip", "rename"):
            raise JobCancelled()
        if apply_all:
            self.conflict_all = action
        return action

    def _run_copy(self, move):
        st = self.state
        if not self.dst_dir or not os.path.isdir(lp(self.dst_dir)):
            self._report_error(self.dst_dir or "", "Папка назначения не найдена",
                               can_retry=False)
            return
        sizes = {}
        for s in self.sources:
            self._check()
            sizes[s] = self._scan(s) if os.path.lexists(lp(s)) else (0, 0)
            st.total_bytes += sizes[s][0]
            st.total_items += sizes[s][1]
        st.phase = "run"
        for src in self.sources:
            self._check()
            if not os.path.lexists(lp(src)):
                continue
            self._transfer_top(src, move, sizes[src])

    def _transfer_top(self, src, move, size):
        st = self.state
        name = os.path.basename(src.rstrip("\\/"))
        dst = os.path.join(self.dst_dir, name)
        try:
            src_is_dir = os.path.isdir(lp(src)) and not is_reparse(src)
        except OSError:
            src_is_dir = False
        nsrc = os.path.normcase(os.path.abspath(src))
        ndst = os.path.normcase(os.path.abspath(dst))
        st.current = src

        if nsrc == ndst:
            if move:                               # некуда переносить
                st.skipped += 1
                st.done_bytes += size[0]
                return
            dst = self._unique_name(self.dst_dir, name, src_is_dir)
        elif src_is_dir and ndst.startswith(nsrc.rstrip("\\/") + os.sep):
            self._report_error(src, "Нельзя копировать или переносить папку в саму себя",
                               can_retry=False)
            return
        elif os.path.lexists(lp(dst)):
            action = self._resolve_conflict(src, dst)
            if action == "skip":
                st.skipped += 1
                st.done_bytes += size[0]
                st.done_items += size[1]
                return
            if action == "rename":
                dst = self._unique_name(self.dst_dir, name, src_is_dir)
            elif action == "replace":
                dst_is_dir = os.path.isdir(lp(dst)) and not is_reparse(dst)
                if dst_is_dir or src_is_dir:       # файл поверх файла просто перезапишется
                    if not self._delete_entry(dst):
                        st.skipped += 1
                        return

        if move:
            try:
                os.rename(lp(src), lp(dst))        # в пределах диска — мгновенно
                st.copied += size[1]
                st.done_bytes += size[0]
                st.done_items += size[1]
                st.created.append(dst)
                return
            except OSError:
                pass                               # другой диск и т.п. -> копия + удаление
        ok = self._copy_entry(src, dst)
        if ok or os.path.lexists(lp(dst)):
            st.created.append(dst)
        if move and ok:
            self._delete_entry(src)

    def _copy_entry(self, src, dst):
        self._check()
        st = self.state
        st.current = src
        try:
            isdir = os.path.isdir(lp(src))
        except OSError:
            isdir = False
        if isdir and is_reparse(src):
            st.skipped += 1                        # ссылки/junction не копируем (петли)
            return False
        if not isdir:
            return self._copy_file(src, dst)

        while True:
            self._check()
            try:
                os.makedirs(lp(dst), exist_ok=True)
                break
            except OSError as e:
                if self._report_error(src, f"Не удалось создать папку {dst}\n"
                                      f"{describe_os_error(e)}") == "retry":
                    continue
                return False
        all_ok = True
        try:
            with os.scandir(lp(src)) as it:
                names = [e.name for e in it]
        except OSError as e:
            self._report_error(src, describe_os_error(e), can_retry=False)
            return False
        for name in names:
            if not self._copy_entry(os.path.join(src, name), os.path.join(dst, name)):
                all_ok = False
        try:
            shutil.copystat(lp(src), lp(dst))
        except OSError:
            pass
        st.done_items += 1
        return all_ok

    def _copy_file(self, src, dst):
        st = self.state
        while True:
            self._check()
            base = st.done_bytes
            try:
                self._copy_file_once(src, dst)
                st.copied += 1
                st.done_items += 1
                return True
            except JobCancelled:
                raise
            except OSError as e:
                st.done_bytes = base
                res = self._report_error(
                    src, f"Копирование в {dst}\n{describe_os_error(e)}")
                if res == "retry":
                    continue
                try:
                    st.done_bytes = base + os.lstat(lp(src)).st_size
                except OSError:
                    pass
                st.done_items += 1
                return False

    def _copy_file_once(self, src, dst):
        ps, pd = lp(src), lp(dst)
        opened = False
        try:
            with open(ps, "rb") as fi:
                with open(pd, "wb") as fo:
                    opened = True
                    while True:
                        self._check()
                        buf = fi.read(COPY_CHUNK)
                        if not buf:
                            break
                        fo.write(buf)
                        self.state.done_bytes += len(buf)
        except BaseException:
            if opened:                              # не оставляем недописанный файл
                try:
                    os.remove(pd)
                except OSError:
                    pass
            raise
        try:
            shutil.copystat(ps, pd)
        except OSError:
            pass


class SearchWorker(threading.Thread):
    def __init__(self, root_dir, name_pattern, ext_filter,
                 min_size, max_size, callback, done_cb):
        super().__init__(daemon=True)
        self.root_dir = root_dir
        self.name_pattern = (name_pattern or "").strip()
        self.ext_filter = (ext_filter or "").strip()
        self.min_size = min_size
        self.max_size = max_size
        self.callback = callback
        self.done_cb = done_cb
        self._stop = False

    def stop(self):
        self._stop = True

    def _name_ok(self, filename):
        if not self.name_pattern:
            return True
        pat = self.name_pattern.lower()
        if "*" in pat or "?" in pat:
            return fnmatch.fnmatch(filename.lower(), pat)
        return pat in filename.lower()

    def _ext_ok(self, filename):
        if not self.ext_filter:
            return True
        raw = self.ext_filter.replace(";", ",")
        exts = []
        for e in raw.split(","):
            e = e.strip().lower().lstrip("*").lstrip(".").strip()
            if e:
                exts.append(e)
        if not exts:
            return True
        return os.path.splitext(filename)[1].lower().lstrip(".") in exts

    def run(self):
        found = []
        try:
            for root, _dirs, files in os.walk(self.root_dir):
                if self._stop:
                    break
                for fn in files:
                    if self._stop:
                        break
                    if not self._name_ok(fn):
                        continue
                    if not self._ext_ok(fn):
                        continue
                    full = os.path.join(root, fn)
                    try:
                        size, mtime = os.path.getsize(full), os.path.getmtime(full)
                    except OSError:
                        continue
                    if self.min_size is not None and size < self.min_size:
                        continue
                    if self.max_size is not None and size > self.max_size:
                        continue
                    found.append((fn, full, size, mtime))
        except Exception as e:
            print("Ошибка поиска:", e)
        self.callback(found)
        self.done_cb()


# ===========================================================================
# Диалог поиска
# ===========================================================================
class SearchDialog(tk.Toplevel):
    def __init__(self, parent, start_dir, explorer):
        super().__init__(parent)
        self.title("Поиск файлов")
        self.geometry("900x560")
        self.configure(bg=DARK["bg"])
        self.start_dir = start_dir
        self.explorer = explorer
        self.worker = None

        frm = tk.Frame(self, bg=DARK["bg"])
        frm.pack(fill="x", padx=10, pady=(10, 4))
        tk.Label(frm, text="Папка:", bg=DARK["bg"], fg=DARK["fg"]
                 ).grid(row=0, column=0, sticky="w")
        self.dir_var = tk.StringVar(value=start_dir)
        ttk.Entry(frm, textvariable=self.dir_var
                  ).grid(row=0, column=1, columnspan=5, sticky="ew", padx=4)
        tk.Label(frm, text="Имя:", bg=DARK["bg"], fg=DARK["fg"]
                 ).grid(row=1, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.name_var
                  ).grid(row=1, column=1, sticky="ew", padx=4)
        tk.Label(frm, text="Расширение:", bg=DARK["bg"], fg=DARK["fg"]
                 ).grid(row=1, column=2, sticky="w", padx=(8, 4))
        self.ext_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.ext_var, width=22
                  ).grid(row=1, column=3, sticky="w", padx=4)
        tk.Label(frm, text="Мин. размер:", bg=DARK["bg"], fg=DARK["fg"]
                 ).grid(row=2, column=0, sticky="w")
        self.min_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.min_var, width=14
                  ).grid(row=2, column=1, sticky="w", padx=4)
        tk.Label(frm, text="Макс. размер:", bg=DARK["bg"], fg=DARK["fg"]
                 ).grid(row=2, column=2, sticky="w", padx=(8, 4))
        self.max_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.max_var, width=14
                  ).grid(row=2, column=3, sticky="w", padx=4)
        tk.Label(frm,
                 text="Имя: подстрока или маска (*.txt, doc?.*, *report*).  "
                      "Расширение: txt,pdf,docx или .txt;.pdf.  "
                      "Размер: 100, 10kb, 2.5mb, 1gb.",
                 bg=DARK["bg"], fg=DARK["muted"], font=("Segoe UI", 8)
                 ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(0, 6))
        frm.columnconfigure(1, weight=1)

        btns = tk.Frame(self, bg=DARK["bg"])
        btns.pack(fill="x", padx=10)
        self.btn_start = ttk.Button(btns, text="Начать поиск", command=self._start)
        self.btn_start.pack(side="left")
        ttk.Button(btns, text="Стоп", command=self._stop).pack(side="left", padx=4)
        ttk.Button(btns, text="Открыть", command=self._open_selected
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Открыть папку", command=self._open_folder
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Копировать путь", command=self._copy_paths
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Закрыть", command=self._close).pack(side="right")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=4)

        self.results = ttk.Treeview(self, columns=("size", "mtime"),
                                    show="tree headings", selectmode="extended")
        self.results.heading("#0", text="Имя")
        self.results.heading("size", text="Размер")
        self.results.heading("mtime", text="Изменён")
        self.results.column("#0", width=520)
        self.results.column("size", width=100, anchor="e")
        self.results.column("mtime", width=150)
        self.results.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.results.bind("<Double-Button-1>", lambda e: self._open_selected())
        self.results.bind("<Return>", lambda e: self._open_selected())
        self.results.bind("<Button-3>", self._context_menu)
        self.transient(parent)

    def _start(self):
        min_s = parse_size(self.min_var.get())
        max_s = parse_size(self.max_var.get())
        if min_s == -1 or max_s == -1:
            messagebox.showwarning("Ошибка", "Неверный формат размера.", parent=self)
            return
        self.results.delete(*self.results.get_children())
        self.progress.start(12)
        self.btn_start.configure(state="disabled")
        self.worker = SearchWorker(self.dir_var.get(), self.name_var.get(),
                                   self.ext_var.get(), min_s, max_s,
                                   self._on_found, self._on_done)
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()

    def _close(self):
        self._stop()
        self.destroy()

    def _on_found(self, found):
        try:
            self.after(0, self._populate, found)
        except tk.TclError:
            pass

    def _populate(self, found):
        for name, full, size, mtime in found:
            self.results.insert("", "end", iid=full, text=" " + name,
                                values=(human_size(size), fmt_mtime(mtime)))

    def _on_done(self):
        try:
            self.after(0, self._finish)
        except tk.TclError:
            pass

    def _finish(self):
        self.progress.stop()
        self.btn_start.configure(state="normal")

    def _selected(self):
        return list(self.results.selection())

    def _open_selected(self):
        for p in self._selected():
            if not os.path.exists(p):
                continue
            if os.path.isdir(p):
                self.explorer.navigate_to(p)
            elif is_archive(p):
                self.explorer.open_archive_in_explorer(p)
            else:
                try:
                    os.startfile(p)  # noqa
                except Exception as e:
                    messagebox.showwarning("Ошибка", str(e), parent=self)

    def _open_folder(self):
        sel = self._selected()
        if not sel:
            return
        p = sel[0]
        target = os.path.dirname(p) if os.path.isfile(p) else p
        if os.path.isdir(target):
            self.explorer.navigate_to(target)

    def _copy_paths(self):
        sel = self._selected()
        if not sel:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(sel))

    def _context_menu(self, event):
        iid = self.results.identify_row(event.y)
        if iid and iid not in self.results.selection():
            self.results.selection_set(iid)
        sel = self._selected()
        m = tk.Menu(self, tearoff=0, bg=DARK["input_bg"], fg=DARK["fg"],
                    activebackground=DARK["select_bg"],
                    activeforeground=DARK["select_fg"])
        if sel:
            m.add_command(label="Открыть", command=self._open_selected)
            m.add_command(label="Открыть папку в проводнике",
                          command=self._open_folder)
            if any(is_archive(p) for p in sel):
                m.add_command(label="Открыть архив",
                              command=lambda: self._open_archives(sel))
            m.add_command(label="Копировать путь", command=self._copy_paths)
            m.add_separator()
            m.add_command(label="Свойства",
                          command=lambda: [PropertiesDialog(self, p) for p in sel[:5]])
            m.add_command(label="Хеш-суммы...", command=lambda: HashDialog(self, sel))
            m.add_separator()
            m.add_command(label="Удалить", command=self._delete_selected)
        else:
            m.add_command(label="Копировать все пути", command=self._copy_all_paths)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _open_archives(self, paths):
        for p in paths:
            if is_archive(p):
                self.explorer.open_archive_in_explorer(p)
                break

    def _copy_all_paths(self):
        paths = [i for i in self.results.get_children()]
        self.clipboard_clear()
        self.clipboard_append("\n".join(paths))

    def _delete_selected(self):
        sel = self._selected()
        if not sel:
            return
        if not messagebox.askyesno("Удаление",
                                   f"Удалить {len(sel)} объект(ов)?",
                                   parent=self):
            return
        self.explorer._delete_paths(sel, refresh_cb=self._remove_deleted)

    def _remove_deleted(self):
        for i in list(self.results.get_children()):
            if not os.path.exists(i):
                self.results.delete(i)


# ===========================================================================
# Свойства
# ===========================================================================
class PropertiesDialog(tk.Toplevel):
    def __init__(self, parent, path):
        super().__init__(parent)
        self.title("Свойства")
        self.geometry("460x360")
        self.configure(bg=DARK["bg"])
        self.path = path
        self._size_label = None
        try:
            st = os.stat(lp(path))
        except OSError as e:
            tk.Label(self, text=str(e), bg=DARK["bg"], fg=DARK["fg"]
                     ).pack(padx=20, pady=20)
            return
        is_dir = os.path.isdir(path)
        rows = [
            ("Имя", os.path.basename(path)),
            ("Путь", os.path.dirname(path)),
            ("Тип", file_type_name(path, is_dir)),
            ("Размер", "вычисляется..." if is_dir else human_size(st.st_size)),
            ("Создан", fmt_mtime(getattr(st, "st_ctime", 0))),
            ("Изменён", fmt_mtime(st.st_mtime)),
        ]
        try:
            attrs = st.st_file_attributes
            flags = []
            if attrs & stat.FILE_ATTRIBUTE_READONLY: flags.append("только чтение")
            if attrs & stat.FILE_ATTRIBUTE_HIDDEN:   flags.append("скрытый")
            if attrs & stat.FILE_ATTRIBUTE_SYSTEM:   flags.append("системный")
            rows.append(("Атрибуты", ", ".join(flags) or "обычный"))
        except (AttributeError, OSError):
            pass
        for i, (k, v) in enumerate(rows):
            tk.Label(self, text=k + ":", bg=DARK["bg"], fg=DARK["muted"],
                     anchor="w", width=12).grid(row=i, column=0, sticky="w",
                                                padx=(12, 4), pady=3)
            lbl = tk.Label(self, text=v, bg=DARK["bg"], fg=DARK["fg"],
                           anchor="w", wraplength=320, justify="left")
            lbl.grid(row=i, column=1, sticky="w", padx=(0, 12), pady=3)
            if k == "Размер" and is_dir:
                self._size_label = lbl
        ttk.Button(self, text="OK", command=self.destroy).grid(
            row=len(rows) + 1, column=0, columnspan=2, pady=12)
        if is_dir and self._size_label:
            threading.Thread(target=self._calc_size, daemon=True).start()

    def _calc_size(self):
        total = calc_dir_size(self.path)
        try:
            self.after(0, lambda: self._size_label.configure(text=human_size(total)))
        except tk.TclError:
            pass


class HashDialog(tk.Toplevel):
    def __init__(self, parent, paths):
        super().__init__(parent)
        self.title("Хеш-суммы")
        self.geometry("720x400")
        self.configure(bg=DARK["bg"])
        self.paths = paths
        tk.Label(self, text="Выберите алгоритм:", bg=DARK["bg"], fg=DARK["fg"]
                 ).pack(anchor="w", padx=12, pady=(10, 4))
        self.algo_var = tk.StringVar(value="md5")
        frm = tk.Frame(self, bg=DARK["bg"]); frm.pack(anchor="w", padx=12)
        for a in ("md5", "sha1", "sha256", "crc32"):
            tk.Radiobutton(frm, text=a.upper(), variable=self.algo_var, value=a,
                           bg=DARK["bg"], fg=DARK["fg"],
                           selectcolor=DARK["input_bg"],
                           activebackground=DARK["bg"], activeforeground=DARK["fg"],
                           highlightthickness=0).pack(side="left", padx=6)
        ttk.Button(self, text="Вычислить", command=self._run
                   ).pack(anchor="w", padx=12, pady=6)
        self.txt = tk.Text(self, bg=DARK["list_bg"], fg=DARK["fg"],
                           insertbackground=DARK["fg"], bd=0, wrap="none")
        self.txt.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _run(self):
        self.txt.delete("1.0", tk.END)
        threading.Thread(target=self._compute, args=(self.algo_var.get(),),
                         daemon=True).start()

    def _compute(self, algo):
        import zlib
        for p in self.paths:
            try:
                if algo == "crc32":
                    crc = 0
                    with open(p, "rb") as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            crc = zlib.crc32(chunk, crc)
                    h = f"{crc & 0xffffffff:08x}"
                else:
                    hh = hashlib.new(algo)
                    with open(p, "rb") as f:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            hh.update(chunk)
                    h = hh.hexdigest()
                line = f"{h}  {p}\n"
            except OSError as e:
                line = f"ERROR: {e}  {p}\n"
            try:
                self.after(0, lambda l=line: self.txt.insert(tk.END, l))
            except tk.TclError:
                return


class ConflictDialog(tk.Toplevel):
    def __init__(self, parent, src, dst):
        super().__init__(parent)
        self.title("Конфликт имён")
        self.geometry("460x220")
        self.configure(bg=DARK["bg"])
        self.result = None
        self.apply_all = tk.BooleanVar(value=False)
        tk.Label(self, text="Файл уже существует:", bg=DARK["bg"], fg=DARK["fg"]
                 ).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(self, text=dst, bg=DARK["bg"], fg=DARK["muted"],
                 wraplength=420, justify="left").pack(anchor="w", padx=12)
        tk.Label(self, text="Источник:", bg=DARK["bg"], fg=DARK["fg"]
                 ).pack(anchor="w", padx=12, pady=(10, 2))
        tk.Label(self, text=src, bg=DARK["bg"], fg=DARK["muted"],
                 wraplength=420, justify="left").pack(anchor="w", padx=12)
        tk.Checkbutton(self, text="Применить ко всем", variable=self.apply_all,
                       bg=DARK["bg"], fg=DARK["fg"],
                       selectcolor=DARK["input_bg"],
                       activebackground=DARK["bg"], activeforeground=DARK["fg"],
                       highlightthickness=0).pack(anchor="w", padx=12, pady=8)
        btns = tk.Frame(self, bg=DARK["bg"])
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btns, text="Заменить", command=lambda: self._set("replace")
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Пропустить", command=lambda: self._set("skip")
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Переименовать", command=lambda: self._set("rename")
                   ).pack(side="left", padx=4)
        ttk.Button(btns, text="Отмена", command=self._cancel).pack(side="right")
        self.transient(parent); self.grab_set()

    def _set(self, action):
        self.result = (action, self.apply_all.get()); self.destroy()

    def _cancel(self):
        self.result = None; self.destroy()


class ProcessChooserDialog(tk.Toplevel):
    def __init__(self, parent, procs):
        super().__init__(parent)
        self.title("Выберите процессы для завершения")
        self.geometry("720x420")
        self.configure(bg=DARK["bg"])
        self.result = None
        self.vars = []
        tk.Label(self, text=f"Найдено процессов: {len(procs)}.\nОтметьте нужные:",
                 bg=DARK["bg"], fg=DARK["fg"], justify="left"
                 ).pack(anchor="w", padx=12, pady=(10, 6))
        wrap = tk.Frame(self, bg=DARK["bg"]); wrap.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(wrap, bg=DARK["list_bg"], highlightthickness=0)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=DARK["list_bg"])
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        for p in procs:
            text = f"{p['name']}  (PID {p['pid']})"
            if p["exe"]:
                text += f"  —  {p['exe']}"
            var = tk.BooleanVar(value=True)
            tk.Checkbutton(inner, text=text, variable=var,
                           bg=DARK["list_bg"], fg=DARK["fg"],
                           selectcolor=DARK["input_bg"],
                           activebackground=DARK["list_bg"],
                           activeforeground=DARK["fg"],
                           anchor="w", justify="left",
                           highlightthickness=0, bd=0
                           ).pack(fill="x", padx=6, pady=2)
            self.vars.append((var, p))
        btns = tk.Frame(self, bg=DARK["bg"])
        btns.pack(side="bottom", fill="x", padx=12, pady=10)
        ttk.Button(btns, text="Завершить", command=self._ok).pack(side="right", padx=4)
        ttk.Button(btns, text="Отмена", command=self._cancel).pack(side="right")
        self.transient(parent); self.grab_set()

    def _ok(self):
        self.result = [p for v, p in self.vars if v.get()]; self.destroy()

    def _cancel(self):
        self.result = None; self.destroy()



class OpErrorDialog(tk.Toplevel):
    """Ошибка файловой операции: настоящий текст от ОС + выбор действия."""
    def __init__(self, parent, path, text, can_retry=True):
        super().__init__(parent)
        self.title("Ошибка операции")
        self.configure(bg=DARK["bg"])
        self.result = "abort"
        self.resizable(False, False)
        tk.Label(self, text="Не удалось выполнить операцию с:", bg=DARK["bg"],
                 fg=DARK["fg"]).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(self, text=path, bg=DARK["bg"], fg=DARK["muted"],
                 wraplength=S(520), justify="left").pack(anchor="w", padx=12)
        tk.Label(self, text=text, bg=DARK["bg"], fg=DARK["busy_fg"],
                 wraplength=S(520), justify="left").pack(anchor="w", padx=12, pady=10)
        btns = tk.Frame(self, bg=DARK["bg"])
        btns.pack(fill="x", padx=12, pady=(0, 12))
        if can_retry:
            ttk.Button(btns, text="Повторить",
                       command=lambda: self._set("retry")).pack(side="left", padx=3)
        ttk.Button(btns, text="Пропустить",
                   command=lambda: self._set("skip")).pack(side="left", padx=3)
        ttk.Button(btns, text="Пропустить все",
                   command=lambda: self._set("skip_all")).pack(side="left", padx=3)
        ttk.Button(btns, text="Прервать",
                   command=lambda: self._set("abort")).pack(side="right", padx=3)
        self.protocol("WM_DELETE_WINDOW", lambda: self._set("abort"))
        self.transient(parent)
        self.grab_set()

    def _set(self, r):
        self.result = r
        self.destroy()


def _short_path(text, limit=80):
    return text if len(text) <= limit else text[:limit // 2 - 1] + "…" + text[-(limit // 2):]


class JobWindow(tk.Toplevel):
    """Окно прогресса фоновой операции. Основное окно остаётся доступным."""
    TITLES = {"delete": "Удаление", "copy": "Копирование", "move": "Перемещение"}

    def __init__(self, parent, job):
        super().__init__(parent)
        self.job = job
        self.title(self.TITLES.get(job.kind, "Операция"))
        self.configure(bg=DARK["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.lbl_cur = tk.Label(self, text="", bg=DARK["bg"], fg=DARK["fg"],
                                anchor="w", justify="left", wraplength=S(460))
        self.lbl_cur.pack(fill="x", padx=12, pady=(12, 4))
        self.bar = ttk.Progressbar(self, mode="indeterminate", length=S(460),
                                   maximum=1000)
        self.bar.pack(fill="x", padx=12)
        self.lbl_info = tk.Label(self, text="", bg=DARK["bg"], fg=DARK["muted"],
                                 anchor="w")
        self.lbl_info.pack(fill="x", padx=12, pady=4)
        self.btn = ttk.Button(self, text="Отмена", command=self._cancel)
        self.btn.pack(pady=(2, 10))
        self._determinate = False
        self._t0 = None
        self._b0 = 0
        try:
            self.bar.start(15)
        except tk.TclError:
            pass

    def _cancel(self):
        self.job.cancel()
        try:
            self.btn.configure(state="disabled", text="Отмена…")
        except tk.TclError:
            pass

    def refresh_view(self):
        st = self.job.state
        try:
            if st.phase == "scan":
                self.lbl_cur.configure(text="Подсчёт: " + _short_path(st.current))
                self.lbl_info.configure(text=f"Найдено объектов: {st.total_items}")
                return
            self.lbl_cur.configure(text=_short_path(st.current))
            now = time.monotonic()
            if self._t0 is None:
                self._t0, self._b0 = now, st.done_bytes
            if self.job.kind == "delete":
                self.lbl_info.configure(
                    text=f"Удалено объектов: {st.deleted}"
                         + (f",  завершено процессов: {st.killed}" if st.killed else ""))
                return
            if st.total_bytes > 0:
                if not self._determinate:
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                    self._determinate = True
                self.bar["value"] = min(1000, st.done_bytes * 1000 // st.total_bytes)
            dt = max(0.001, now - self._t0)
            speed = max(0, st.done_bytes - self._b0) / dt
            eta = ""
            if speed > 1 and st.total_bytes > st.done_bytes:
                sec = int((st.total_bytes - st.done_bytes) / speed)
                eta = f",  осталось ~{sec // 60}:{sec % 60:02d}"
            self.lbl_info.configure(
                text=f"{human_size(st.done_bytes)} из {human_size(st.total_bytes)}"
                     f"   ({human_size(int(speed))}/с{eta})")
        except tk.TclError:
            pass


# ===========================================================================
# Панель списка файлов
# ===========================================================================
class FilePanel:
    def __init__(self, parent, explorer):
        self.explorer = explorer
        self.col_w = explorer.col_widths              # общие ширины колонок обеих панелей
        self.current_dir = ""
        self.history = []
        self.history_index = -1
        self.sort_key = "name"
        self.sort_dir = 1
        self.busy = {}

        # --- данные ---
        self._entries = []           # отсортированный полный список записей
        self._index = {}             # путь -> позиция в _entries
        self._by_path = {}           # путь -> запись
        self._kind = {}              # путь -> is_dir (без обращения к диску)
        self._loaded_dir = None      # папка, чей список сейчас показан целиком
        self._load_err = ""

        # --- фоновая загрузка ---
        self._load_token = 0
        self._cancel_evt = None
        self._prev_state = None      # (dir, history, index) для отката при ошибке
        self._pending_render = None
        self._render_index = 0
        self._render_job = None

        # --- разметка колонок ---
        self._name_w = None
        self._x_offset = 0
        self._applied_cols = None
        self._drag = None
        self._last_col_fixed = True

        # --- hover ---
        self._hover_item = None
        self._last_hover_ts = 0.0

        # --- виртуальный список (большие папки) ---
        self._virtual = False
        self._vtop = 0
        self._vcursor = None
        self._vanchor = None
        self._vsel = set()
        self._v_job = None

        # --- состояние вида ---
        self._view_cache = {}        # нормализованная папка -> {"top", "sel"}
        self._restore = None
        self._select_after = None
        self._select_near = None

        # --- автообновление ---
        self._watcher = None
        self._fs_timer = None

        self.frame = tk.Frame(parent, bg=DARK["bg"])

        # Полоска с текущим путём
        self.header = tk.Label(self.frame, text="", bg=DARK["input_bg"],
                               fg=DARK["fg"], anchor="w", padx=6)
        self.header.pack(fill="x")

        body = tk.Frame(self.frame, bg=DARK["bg"])
        body.pack(fill="both", expand=True)
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0, minsize=S(16))

        # --- КАСТОМНЫЙ заголовок колонок ---
        self._col_header = tk.Frame(body, bg=DARK["input_bg"], height=HEADER_HEIGHT)
        self._col_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._col_header.grid_propagate(False)
        self._header_labels = {}
        self._handles = []
        self._build_col_header()

        # --- Treeview БЕЗ штатного заголовка ---
        self.tree = ttk.Treeview(
            body, columns=("size", "type", "mtime"),
            show="tree", selectmode="extended")
        # Все колонки stretch=False: ширину "Имени" считаем сами из реальной
        # ширины виджета — колонки и подписи двигаются одним действием.
        self.tree.column("#0", width=NAME_DEFAULT_W, stretch=False,
                         minwidth=NAME_MIN_W)
        for k in COL_KEYS:
            self.tree.column(k, width=self.col_w[k], stretch=False, minwidth=COL_MIN)

        self.tree.tag_configure("hidden", foreground=DARK["hidden_fg"])
        self.tree.tag_configure("busy",   foreground=DARK["busy_fg"])
        self.tree.tag_configure("hover",  background=DARK["hover"])

        self._scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._scroll.set,
                            xscrollcommand=self._on_tree_xscroll)

        self.tree.grid(row=1, column=0, sticky="nsew")
        self._scroll.grid(row=1, column=1, sticky="ns")

        # --- события ---
        self.tree.bind("<Double-Button-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_context)
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Motion>", self._on_motion, add="+")
        self.tree.bind("<Leave>",  self._on_leave,  add="+")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.tree.bind(seq, self._on_wheel, add="+")
        for seq in ("<Up>", "<Down>", "<Prior>", "<Next>", "<Home>", "<End>",
                    "<Shift-Up>", "<Shift-Down>"):
            self.tree.bind(seq, self._on_nav_key)
        self.tree.bind("<Configure>", self._on_tree_configure, add="+")

        if HAS_DND:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._on_drop)

        # Подписи сразу ставим на их места (ширины по умолчанию совпадают с
        # колонками Treeview), а точную подгонку сделает первый <Configure>.
        self._place_header()
        self.frame.after_idle(self._sync_header)

    # ================================================================
    # Заголовок колонок
    # ================================================================
    def _build_col_header(self):
        for w in self._col_header.winfo_children():
            w.destroy()
        self._header_labels = {}
        self._handles = []
        for key, text in (("name", "Имя"), ("size", "Размер"),
                          ("type", "Тип"), ("mtime", "Изменён")):
            lbl = tk.Label(self._col_header, text=text,
                           bg=DARK["input_bg"], fg=DARK["fg"],
                           anchor="w", padx=6, cursor="hand2")
            lbl.place(x=0, y=0, width=S(90), height=HEADER_HEIGHT)
            lbl.bind("<Button-1>", lambda e, k=key: self._sort_by(k))
            self._header_labels[key] = lbl
        # границы колонок, которые можно тянуть мышью
        for i in range(3):
            h = tk.Frame(self._col_header, bg=DARK["input_bg"],
                         cursor="sb_h_double_arrow")
            line = tk.Frame(h, bg=DARK["border"], cursor="sb_h_double_arrow")
            line.place(relx=0.5, rely=0.15, relheight=0.7, width=1)
            h.place(x=0, y=0, width=HANDLE_W, height=HEADER_HEIGHT)
            for w in (h, line):
                w.bind("<Button-1>", lambda e, i=i: self._hdl_press(i, e))
                w.bind("<B1-Motion>", lambda e, i=i: self._hdl_move(i, e))
                w.bind("<ButtonRelease-1>", self._hdl_release)
            self._handles.append(h)

    def _fixed_sum(self):
        return sum(self.col_w[k] for k in COL_KEYS)

    def _on_tree_configure(self, event):
        self._sync_header(width=event.width)
        if self._virtual:
            self._v_schedule()

    def _on_tree_xscroll(self, first, last):
        """Treeview сам сдвигается по X, если колонки шире виджета —
        заголовок должен ехать вместе с ним."""
        try:
            total = (self._name_w or NAME_DEFAULT_W) + self._fixed_sum()
            off = int(round(float(first) * total))
        except (TypeError, ValueError):
            off = 0
        if off != self._x_offset:
            self._x_offset = off
            self._place_header()

    def _sync_header(self, event=None, width=None):
        """Единая раскладка колонок. Ширина "Имени" вычисляется из ФАКТИЧЕСКОЙ
        ширины виджета (а не читается из Treeview, который обновляется позже)."""
        try:
            if width is None:
                width = self.tree.winfo_width()
            if width <= 1:
                width = NAME_DEFAULT_W + self._fixed_sum()
            name_w = max(NAME_MIN_W, width - self._fixed_sum())
            cols = (name_w,) + tuple(self.col_w[k] for k in COL_KEYS)
            if cols != self._applied_cols:
                self._applied_cols = cols
                self._name_w = name_w
                self.tree.column("#0", width=name_w)
                for k in COL_KEYS:
                    self.tree.column(k, width=self.col_w[k])
                self._place_header()
        except tk.TclError:
            pass

    def _place_header(self):
        name_w = self._name_w or NAME_DEFAULT_W
        x = -self._x_offset
        xs = []
        try:
            self._header_labels["name"].place_configure(x=x, width=name_w)
            x += name_w
            xs.append(x)
            for key in COL_KEYS:
                w = self.col_w[key]
                self._header_labels[key].place_configure(x=x, width=w)
                x += w
                xs.append(x)
            for i, h in enumerate(self._handles):
                h.place_configure(x=xs[i] - HANDLE_W // 2)
                h.lift()
        except (tk.TclError, KeyError, IndexError):
            pass

    # --- перетаскивание границ колонок ---
    def _hdl_press(self, i, event):
        self._drag = (i, event.x_root, dict(self.col_w))

    def _hdl_move(self, i, event):
        if not self._drag:
            return
        idx, x0, w0 = self._drag
        dx = event.x_root - x0
        try:
            avail = self.tree.winfo_width()
        except tk.TclError:
            return
        if idx == 0:
            # граница "Имя|Размер": левый край "Размера" едет за мышью
            others = w0["type"] + w0["mtime"]
            hi = max(COL_MIN, avail - NAME_MIN_W - others)
            self.col_w["size"] = int(min(max(w0["size"] - dx, COL_MIN), hi))
        else:
            a, b = COL_KEYS[idx - 1], COL_KEYS[idx]
            total = w0[a] + w0[b]
            na = int(min(max(w0[a] + dx, COL_MIN), total - COL_MIN))
            self.col_w[a] = na
            self.col_w[b] = total - na
        self.explorer.apply_col_widths()

    def _hdl_release(self, event):
        self._drag = None

    def _update_sort_indicators(self):
        """Показать ▲/▼ у активной колонки."""
        arrow = "▲" if self.sort_dir > 0 else "▼"
        labels = {"name": "Имя", "size": "Размер",
                  "type": "Тип", "mtime": "Изменён"}
        for key, base in labels.items():
            text = base + ("  " + arrow if key == self.sort_key else "")
            try:
                self._header_labels[key].configure(text=text)
            except KeyError:
                pass

    def set_fixed_last_column(self, fixed):
        """Оставлено для совместимости."""
        self._last_col_fixed = True

    # ================================================================
    # Навигация
    # ================================================================
    @staticmethod
    def _norm(path):
        try:
            return os.path.normcase(os.path.normpath(path))
        except Exception:
            return path

    def navigate_to(self, path, add_history=True):
        """Мгновенно: проверку "это папка?" и чтение делает фоновый поток,
        поэтому UI не блокируется на медленных/недоступных дисках."""
        if not path:
            return False
        self._remember_view()
        self._prev_state = (self.current_dir, list(self.history), self.history_index)
        self.current_dir = path
        if add_history:
            self.history = self.history[:self.history_index + 1]
            if not self.history or self.history[-1] != path:
                self.history.append(path)
                self.history_index = len(self.history) - 1
        self.refresh()
        self.explorer.panel_dir_changed(self)
        return True

    def go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self.navigate_to(self.history[self.history_index], add_history=False)

    def go_forward(self):
        if self.history_index < len(self.history) - 1:
            self.history_index += 1
            self.navigate_to(self.history[self.history_index], add_history=False)

    def go_up(self):
        if not self.current_dir:
            return
        parent = parent_dir(self.current_dir)
        if parent and self._norm(parent) != self._norm(self.current_dir):
            self.navigate_to(parent)

    # ================================================================
    # Загрузка / обновление
    # ================================================================
    def _cancel_loading(self):
        if self._cancel_evt is not None:
            self._cancel_evt.set()
            self._cancel_evt = None
        if self._render_job:
            try:
                self.frame.after_cancel(self._render_job)
            except Exception:
                pass
            self._render_job = None

    def _post(self, fn, *args):
        """Безопасно перебросить вызов из рабочего потока в главный."""
        try:
            self.frame.after(0, fn, *args)
        except (tk.TclError, RuntimeError):
            pass

    def refresh(self, soft=None):
        """soft=None: если эта папка уже показана — обновляем «мягко»
        (только изменившиеся строки, прокрутка и выделение не сбрасываются);
        иначе — полная перерисовка."""
        if not self.current_dir:
            return
        if soft is None:
            soft = (self._loaded_dir is not None and
                    self._norm(self._loaded_dir) == self._norm(self.current_dir))
        self._cancel_loading()
        self._load_token += 1
        token = self._load_token
        if not soft:
            self._pending_render = None
            self._render_index = 0
            self._loaded_dir = None
            self._load_err = ""
            self._entries, self._index, self._by_path, self._kind = [], {}, {}, {}
            self._vsel = set()
            self._vtop = 0
            self._vcursor = self._vanchor = None
            self._hover_item = None
            self._leave_virtual()
            self.tree.delete(*self.tree.get_children())
            self._restore = self._view_cache.get(self._norm(self.current_dir))
            self.header.configure(text=self.current_dir + "   (загрузка…)")
            self.explorer.icons.prune_specific(self.explorer.open_dirs_norm())
            self._restart_watch()
        mode = "soft" if soft else "full"
        cancel = threading.Event()
        self._cancel_evt = cancel
        DirLoader(
            self.current_dir, self.explorer.show_hidden, cancel,
            lambda n: self._post(self._on_progress, token, n),
            lambda entries, err: self._post(self._on_loaded, token, entries, err, mode),
        ).start()

    def _on_progress(self, token, n):
        if token != self._load_token or self._loaded_dir is not None:
            return
        self.header.configure(text=f"{self.current_dir}   (загрузка… {n})")

    def _on_loaded(self, token, entries, err, mode="full"):
        if token != self._load_token:
            return
        self._cancel_evt = None
        self._load_err = ""
        if err:
            kind, text = err
            if kind == "missing":
                self._handle_missing(text)
                return
            self._load_err = text
            self.explorer.status(text)
        self._prev_state = None
        entries = self._sorted(entries)
        if mode == "soft" and self._loaded_dir is not None \
                and self._pending_render is None:
            self._apply_soft(entries)
        else:
            self._begin_full(entries, token)

    def _handle_missing(self, text):
        """Папки нет / это не папка: откатываемся туда, где были; если папка
        исчезла под ногами (удалили снаружи) — поднимаемся к существующему
        родителю."""
        self.explorer.status(text)
        prev, self._prev_state = self._prev_state, None
        if prev is not None and prev[0] and prev[0] != self.current_dir:
            self.current_dir, self.history, self.history_index = prev
            self._loaded_dir = None
            self.refresh()
            self.explorer.panel_dir_changed(self)
            return
        parent = parent_dir(self.current_dir)
        if parent and self._norm(parent) != self._norm(self.current_dir):
            self.navigate_to(parent)
            self._prev_state = None
            return
        home = os.path.expanduser("~")
        if self._norm(self.current_dir) != self._norm(home):
            self.navigate_to(home)
            self._prev_state = None
        else:
            self.header.configure(text=self.current_dir)

    # ---------- сортировка ----------
    def _sorted(self, entries):
        key = self.sort_key
        rev = self.sort_dir < 0

        def norm(e):
            if key == "name":  return e["name"].lower()
            if key == "size":  return e["size"] if not e["is_dir"] else -1
            if key == "type":  return file_type_name(e["full"], e["is_dir"]).lower()
            if key == "mtime": return e["mtime"]
            return e["name"].lower()

        dirs = [e for e in entries if e["is_dir"]]
        files = [e for e in entries if not e["is_dir"]]
        dirs.sort(key=norm, reverse=rev)
        files.sort(key=norm, reverse=rev)
        return dirs + files

    def _apply_sort(self):
        self._entries = self._sorted(self._entries)

    def _sort_by(self, key):
        if self.sort_key == key:
            self.sort_dir = -self.sort_dir
        else:
            self.sort_key = key
            self.sort_dir = 1
        self._update_sort_indicators()
        if self._loaded_dir is None or self._cancel_evt is not None:
            return                    # ещё грузится — отсортируется по приходу
        view = self._capture_view()
        self._entries = self._sorted(self._entries)
        self._reindex()
        self._restore = view
        self._load_token += 1
        if self._render_job:
            try:
                self.frame.after_cancel(self._render_job)
            except Exception:
                pass
            self._render_job = None
        self._rerender_all(self._load_token)

    def _reindex(self):
        self._index = {e["full"]: i for i, e in enumerate(self._entries)}
        self._by_path = {e["full"]: e for e in self._entries}
        self._kind = {e["full"]: e["is_dir"] for e in self._entries}

    # ---------- полная отрисовка ----------
    def _row_kwargs(self, e):
        icon = self.explorer.icons.for_file(e["full"], is_dir=e["is_dir"])
        tags = []
        if e["hidden"]:
            tags.append("hidden")
        if os.path.normcase(e["full"]) in self.busy:
            tags.append("busy")
        return dict(
            text=" " + e["name"], image=icon if icon else "",
            values=(("" if e["is_dir"] else human_size(e["size"])),
                    file_type_name(e["full"], e["is_dir"]),
                    fmt_mtime(e["mtime"])),
            tags=tuple(tags))

    def _begin_full(self, entries, token):
        self._entries = entries
        self._reindex()
        self._rerender_all(token)

    def _rerender_all(self, token):
        if len(self._entries) > VIRTUAL_THRESHOLD:
            self._enter_virtual()
            self._v_render_now()
            self._finish_render()
        else:
            self._leave_virtual()
            self.tree.delete(*self.tree.get_children())
            self._pending_render = list(self._entries)
            self._render_index = 0
            self._pump_render(token)

    def _pump_render(self, token):
        """Вставка строк с бюджетом по времени: каждый кадр не дольше
        RENDER_BUDGET, потом управление возвращается циклу событий."""
        if token != self._load_token or self._pending_render is None:
            return
        pend = self._pending_render
        i, n = self._render_index, len(pend)
        stop_at = time.perf_counter() + RENDER_BUDGET
        limit = min(n, i + INSERT_BATCH)
        while i < limit:
            e = pend[i]
            i += 1
            try:
                self.tree.insert("", "end", iid=e["full"], **self._row_kwargs(e))
            except tk.TclError:
                continue
            if time.perf_counter() >= stop_at:
                break
        self._render_index = i
        if i < n:
            self._render_job = self.frame.after(2, self._pump_render, token)
            self.header.configure(text=f"{self.current_dir}   ({i}/{n})")
        else:
            self._finish_render()

    def _finish_render(self):
        self._render_job = None
        self._pending_render = None
        self._loaded_dir = self.current_dir
        n_dirs = sum(1 for e in self._entries if e["is_dir"])
        n_files = len(self._entries) - n_dirs
        self.header.configure(text=self.current_dir or "(пусто)")
        self.explorer.status(
            self._load_err or
            f"{self.current_dir}   |   папок: {n_dirs}, файлов: {n_files}")
        self._apply_restore()
        self._apply_post_actions()

    # ---------- мягкое обновление ----------
    def _apply_soft(self, entries):
        """Применить свежий список к уже показанному: удалить исчезнувшие,
        добавить новые, обновить изменённые. Остальные строки не трогаем —
        поэтому прокрутка и выделение остаются на месте."""
        big = len(entries) > VIRTUAL_THRESHOLD
        if self._virtual or big:
            was_virtual = self._virtual
            view = self._capture_view()
            self._entries = entries
            self._reindex()
            self._vsel &= set(self._index)
            if was_virtual:
                self._v_clamp()
                self._v_render_now()
                self._finish_render()
            else:
                self._restore = view
                self._rerender_all(self._load_token)
            return

        tree = self.tree
        old = self._by_path
        newset = {e["full"] for e in entries}
        gone = [p for p in old if p not in newset and tree.exists(p)]
        if gone:
            tree.delete(*gone)
            if self._hover_item in gone:
                self._hover_item = None
        for i, e in enumerate(entries):
            p = e["full"]
            o = old.get(p)
            try:
                if o is None:
                    tree.insert("", i, iid=p, **self._row_kwargs(e))
                elif (o["size"] != e["size"] or o["mtime"] != e["mtime"]
                      or o["hidden"] != e["hidden"] or o["is_dir"] != e["is_dir"]):
                    kw = self._row_kwargs(e)
                    keep = [t for t in tree.item(p, "tags") if t == "hover"]
                    tree.item(p, values=kw["values"], tags=tuple(kw["tags"]) + tuple(keep))
            except tk.TclError:
                continue
        paths = [e["full"] for e in entries]
        children = list(tree.get_children())
        if children != paths:                       # порядок изменился (сортировка по дате и т.п.)
            for i, p in enumerate(paths):
                if i < len(children) and children[i] != p:
                    tree.move(p, "", i)
                    children.remove(p)
                    children.insert(i, p)
        self._entries = entries
        self._reindex()
        self._finish_render()

    # ================================================================
    # Виртуальный список для очень больших папок
    # ================================================================
    def _enter_virtual(self):
        if self._virtual:
            return
        self._virtual = True
        self.tree.configure(yscrollcommand=lambda *a: None)
        self._scroll.configure(command=self._v_scroll_cmd)

    def _leave_virtual(self):
        if not self._virtual:
            return
        self._virtual = False
        if self._v_job:
            try:
                self.frame.after_cancel(self._v_job)
            except Exception:
                pass
            self._v_job = None
        self.tree.configure(yscrollcommand=self._scroll.set)
        self._scroll.configure(command=self.tree.yview)

    def _rows_visible(self):
        try:
            h = self.tree.winfo_height()
        except tk.TclError:
            h = 0
        return max(1, h // ROW_HEIGHT) if h > 20 else 30

    def _v_clamp(self, vis=None):
        vis = vis or self._rows_visible()
        self._vtop = max(0, min(self._vtop, len(self._entries) - vis))

    def _v_schedule(self):
        if self._v_job is None:
            self._v_job = self.frame.after_idle(self._v_run)

    def _v_run(self):
        self._v_job = None
        self._v_render()

    def _v_render_now(self):
        if self._v_job:
            try:
                self.frame.after_cancel(self._v_job)
            except Exception:
                pass
            self._v_job = None
        self._v_render()

    def _v_render(self):
        """В Treeview лежит только видимое окно строк (+запас) — вставка
        тысяч строк больше не нужна."""
        if not self._virtual:
            return
        ents = self._entries
        n = len(ents)
        vis = self._rows_visible()
        self._v_clamp(vis)
        cnt = min(n - self._vtop, vis + 2)
        tree = self.tree
        ch = tree.get_children()
        if ch:
            tree.delete(*ch)
        win = ents[self._vtop:self._vtop + cnt]
        for e in win:
            try:
                tree.insert("", "end", iid=e["full"], **self._row_kwargs(e))
            except tk.TclError:
                pass
        sel = [e["full"] for e in win if e["full"] in self._vsel]
        if sel:
            tree.selection_set(sel)
        cur = self._vcursor
        if cur is not None and self._vtop <= cur < self._vtop + cnt:
            try:
                tree.focus(ents[cur]["full"])
            except tk.TclError:
                pass
        self._hover_item = None
        try:
            if n:
                self._scroll.set(self._vtop / n, min(1.0, (self._vtop + vis) / n))
            else:
                self._scroll.set(0, 1)
        except tk.TclError:
            pass

    def _v_scroll_cmd(self, *args):
        n = len(self._entries)
        if not n:
            return
        vis = self._rows_visible()
        if args and args[0] == "moveto":
            self._vtop = int(float(args[1]) * n)
        elif args and args[0] == "scroll":
            amount = int(args[1])
            if args[2].startswith("page"):
                amount *= max(1, vis - 1)
            self._vtop += amount
        self._v_clamp(vis)
        self._v_schedule()

    def _ensure_visible(self, idx):
        vis = self._rows_visible()
        if idx < self._vtop:
            self._vtop = idx
        elif idx >= self._vtop + vis:
            self._vtop = idx - vis + 1
        self._v_clamp(vis)

    def _sync_vsel(self):
        """Привести _vsel к реальному выделению в окне Treeview."""
        if not self._virtual:
            return
        try:
            window = set(self.tree.get_children())
            cur = set(self.tree.selection())
        except tk.TclError:
            return
        self._vsel = (self._vsel - window) | cur

    def _on_nav_key(self, event):
        """Стрелки/PgUp/PgDn/Home/End в виртуальном режиме (в обычном —
        штатная обработка Treeview)."""
        if not self._virtual:
            return None
        n = len(self._entries)
        if not n:
            return "break"
        self._sync_vsel()
        cur = self._vcursor if self._vcursor is not None else 0
        vis = self._rows_visible()
        k = event.keysym
        if k == "Up":
            cur -= 1
        elif k == "Down":
            cur += 1
        elif k == "Prior":
            cur -= max(1, vis - 1)
        elif k == "Next":
            cur += max(1, vis - 1)
        elif k == "Home":
            cur = 0
        elif k == "End":
            cur = n - 1
        cur = max(0, min(n - 1, cur))
        shift = bool(event.state & 0x1)
        if shift and self._vanchor is not None:
            a, b = sorted((self._vanchor, cur))
            self._vsel = {self._entries[i]["full"] for i in range(a, b + 1)}
        else:
            self._vanchor = cur
            self._vsel = {self._entries[cur]["full"]}
        self._vcursor = cur
        self._ensure_visible(cur)
        self._v_render_now()
        return "break"

    # ================================================================
    # Вид: запоминание позиции/выделения, действия после загрузки
    # ================================================================
    def _capture_view(self):
        if not self._entries or self._loaded_dir is None:
            return None
        try:
            if self._virtual:
                top = self._entries[min(self._vtop, len(self._entries) - 1)]["full"]
            else:
                top = self.tree.identify_row(2) or None
        except tk.TclError:
            top = None
        return {"top": top, "sel": self.selected_paths()[:2000]}

    def _remember_view(self):
        if (self.current_dir and self._loaded_dir is not None and
                self._norm(self._loaded_dir) == self._norm(self.current_dir)):
            v = self._capture_view()
            if v:
                self._view_cache[self._norm(self.current_dir)] = v
                if len(self._view_cache) > VIEW_CACHE_MAX:
                    self._view_cache.pop(next(iter(self._view_cache)))

    def _apply_restore(self):
        r, self._restore = self._restore, None
        if not r:
            return
        sel = [p for p in r.get("sel", []) if p in self._index]
        top = r.get("top")
        idx = self._index.get(top) if top else None
        if self._virtual:
            if idx is not None:
                self._vtop = idx
            if sel:
                self._vsel = set(sel)
                self._vcursor = self._vanchor = self._index[sel[0]]
            self._v_clamp()
            self._v_render_now()
        else:
            n = len(self._entries)
            if idx is not None and n > 1:
                try:
                    self.tree.update_idletasks()
                    self.tree.yview_moveto(idx / n)
                except tk.TclError:
                    pass
            if sel:
                self._set_tree_selection(sel)

    def _set_tree_selection(self, paths):
        ex = []
        for p in paths:
            try:
                if self.tree.exists(p):
                    ex.append(p)
            except tk.TclError:
                pass
        if ex:
            try:
                self.tree.selection_set(ex)
                self.tree.focus(ex[0])
            except tk.TclError:
                pass

    def _apply_post_actions(self):
        if self._select_after:
            paths = [p for p in self._select_after if p in self._index]
            if paths:
                self._select_after = None
                self.select_paths(paths, see=True)
                return
        if self._select_near is not None:
            idx, self._select_near = self._select_near, None
            if self._entries and not self.selected_paths():
                idx = max(0, min(idx, len(self._entries) - 1))
                self.select_paths([self._entries[idx]["full"]], see=True)

    # ---------- публичный API выделения ----------
    def selected_paths(self):
        if self._virtual:
            self._sync_vsel()
            return sorted((p for p in self._vsel if p in self._index),
                          key=self._index.get)
        try:
            return list(self.tree.selection())
        except tk.TclError:
            return []

    def select_paths(self, paths, see=True):
        paths = [p for p in paths if p in self._index]
        if not paths:
            return
        if self._virtual:
            self._vsel = set(paths)
            idx = min(self._index[p] for p in paths)
            self._vcursor = self._vanchor = idx
            if see:
                self._ensure_visible(idx)
            self._v_render_now()
            return
        self._set_tree_selection(paths)
        if see:
            try:
                self.tree.see(paths[0])
            except tk.TclError:
                pass

    def select_all(self):
        if self._virtual:
            self._vsel = set(self._index)
            self._v_render_now()
        else:
            try:
                self.tree.selection_set(self.tree.get_children())
            except tk.TclError:
                pass

    def remember_near(self):
        """Перед удалением: запомнить место, чтобы после него выделить соседа."""
        idxs = [self._index[p] for p in self.selected_paths() if p in self._index]
        self._select_near = min(idxs) if idxs else None

    def select_after(self, paths):
        """Выделить эти пути, как только они появятся в списке."""
        self._select_after = list(paths)

    # ================================================================
    # Автообновление
    # ================================================================
    def _restart_watch(self):
        self._stop_watch()
        if not self.current_dir:
            return
        w = DirWatcher(self.current_dir, lambda: self._post(self._on_fs_event))
        self._watcher = w
        w.start()

    def _stop_watch(self):
        w, self._watcher = self._watcher, None
        if w is not None:
            w.stop()
        if self._fs_timer is not None:
            try:
                self.frame.after_cancel(self._fs_timer)
            except Exception:
                pass
            self._fs_timer = None

    def _on_fs_event(self):
        if self._fs_timer is not None:
            return
        delay = FS_DEBOUNCE_BUSY if self.explorer.jobs_active() else FS_DEBOUNCE_MS
        self._fs_timer = self.frame.after(delay, self._fs_fire)

    def _fs_fire(self):
        self._fs_timer = None
        if not self.current_dir:
            return
        if self._cancel_evt is not None or self._pending_render is not None:
            self._fs_timer = self.frame.after(500, self._fs_fire)
            return
        self.refresh()

    # ================================================================
    # hover / колесо / клики
    # ================================================================
    def _on_motion(self, event):
        now = time.monotonic()
        if now - self._last_hover_ts < HOVER_THROTTLE:
            return
        self._last_hover_ts = now
        item = self.tree.identify_row(event.y)
        if item == self._hover_item:
            return
        self._set_hover(item)

    def _on_leave(self, event):
        self._set_hover(None)

    def _set_hover(self, item):
        prev = self._hover_item
        if prev and self.tree.exists(prev):
            tags = [t for t in self.tree.item(prev, "tags") if t != "hover"]
            self.tree.item(prev, tags=tuple(tags))
        if item and self.tree.exists(item):
            tags = list(self.tree.item(item, "tags"))
            if "hover" not in tags:
                tags.append("hover")
                self.tree.item(item, tags=tuple(tags))
        self._hover_item = item

    def _on_wheel(self, event):
        up = event.num == 4 or (hasattr(event, "delta") and event.delta > 0)
        down = event.num == 5 or (hasattr(event, "delta") and event.delta < 0)
        step = -WHEEL_UNITS if up else WHEEL_UNITS if down else 0
        if self._virtual:
            self._vtop += step
            self._v_clamp()
            self._v_schedule()
        elif step:
            self.tree.yview_scroll(step, "units")
        return "break"

    def _on_click(self, event):
        self.explorer._set_active_panel(self)
        if self._virtual and not (event.state & 0x5):      # без Shift/Ctrl
            self._vsel = set()
            iid = self.tree.identify_row(event.y)
            if iid in self._index:
                self._vcursor = self._vanchor = self._index[iid]

    def _on_select(self, event=None):
        if self._virtual:
            self._sync_vsel()
            f = self.tree.focus()
            if f in self._index:
                self._vcursor = self._index[f]
        self.explorer._set_active_panel(self)

    def _on_double(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        is_dir = self._kind.get(iid)
        if is_dir is None:
            is_dir = os.path.isdir(iid)
        if is_dir:
            self.navigate_to(iid)
        elif is_archive(iid):
            self.explorer.open_archive_in_explorer(iid)
        else:
            try:
                os.startfile(iid)  # noqa
            except OSError as e:
                self.explorer.status("Не удалось открыть: " + str(e))

    def _on_context(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.explorer.show_context_menu(event, iid, self)

    def _on_drop(self, event):
        if not self.current_dir:
            return
        try:
            srcs = list(self.explorer.root.tk.splitlist(event.data))
        except Exception:
            srcs = [event.data]
        todo = []
        for src in srcs:
            if not src or not os.path.exists(src):
                continue
            if os.path.normcase(os.path.abspath(src)).startswith(
                    os.path.normcase(self.current_dir)):
                continue
            todo.append(src)
        if todo:
            self.explorer.start_transfer(todo, self.current_dir, move=False)

    def apply_busy_tags(self, busy):
        self.busy = busy
        for iid in self.tree.get_children():
            in_busy = os.path.normcase(iid) in busy
            tags = list(self.tree.item(iid, "tags"))
            has_busy = "busy" in tags
            if in_busy and not has_busy:
                tags.append("busy")
                try:
                    self.tree.item(iid, tags=tuple(tags))
                except tk.TclError:
                    pass
            elif not in_busy and has_busy:
                tags = [t for t in tags if t != "busy"]
                try:
                    self.tree.item(iid, tags=tuple(tags))
                except tk.TclError:
                    pass

    def dispose(self):
        self._stop_watch()
        self._cancel_loading()


# ===========================================================================
# Главное окно
# ===========================================================================
class ExplorerWindow:
    def __init__(self, root, settings):
        self.root = root
        self.settings = settings
        self.show_hidden = settings.get("show_hidden", True)
        self.address_history = settings.get("address_history", [])
        self.bookmarks = settings.get("bookmarks", [])
        self.editor_cmd = settings.get("editor_cmd", "notepad")
        self.clipboard = None

        # ширины колонок (в настройках лежат в "базовых" пикселях, без DPI)
        saved = settings.get("col_widths") or {}
        self.col_widths = {}
        for k in COL_KEYS:
            try:
                base = int(saved.get(k, _COL_BASE[k]))
            except (TypeError, ValueError):
                base = _COL_BASE[k]
            self.col_widths[k] = max(COL_MIN, S(max(20, min(600, base))))
        try:
            self._dual_ratio = min(0.9, max(0.1, float(settings.get("dual_ratio", 0.5))))
        except (TypeError, ValueError):
            self._dual_ratio = 0.5

        self.icons = IconCache()
        self.icons.attach(root, self._icon_ready, self._icon_wanted)
        self._jobs = []
        self._active_jobs = 0
        self._closing = False
        self._busy_scan_running = False
        self._busy_scan_pending = None
        self._addr_debounce_id = None
        self._tree_loading = set()        # узлы дерева, которые сейчас грузятся
        self._pending_expand = set()      # узлы, которые надо раскрыть после загрузки
        self._reveal_target = None        # папка, которую дерево слева должно показать

        self.panels = []
        self.active_panel = None
        self.dual = tk.BooleanVar(value=settings.get("dual", False))
        self.hidden_var = tk.BooleanVar(value=self.show_hidden)

        self._build_ui()
        self._populate_drives()
        self._apply_bookmarks()
        self._restore_layout()

        # состояние панелей с прошлого запуска
        cfg = settings.get("panels") or []
        for p, c in zip(self.panels, cfg):
            if isinstance(c, dict):
                if c.get("sort_key") in ("name", "size", "type", "mtime"):
                    p.sort_key = c["sort_key"]
                p.sort_dir = -1 if c.get("sort_dir", 1) < 0 else 1
        for p in self.panels:
            p._update_sort_indicators()

        # существование папки проверяет фоновый загрузчик (при неудаче панель
        # сама откатится на домашнюю папку) — запуск не зависает на дисках
        home = os.path.expanduser("~")
        d1 = (cfg[0].get("dir") if cfg and isinstance(cfg[0], dict) else None) \
            or settings.get("last_dir") or home
        self.navigate_to(d1)
        if self.dual.get():
            d2 = (cfg[1].get("dir") if len(cfg) > 1 and isinstance(cfg[1], dict) else None) or d1
            self.panels[1].navigate_to(d2)
            self._toggle_dual()

    # ---------- общие ширины колонок ----------
    def apply_col_widths(self):
        for p in self.panels:
            p._sync_header()

    def jobs_active(self):
        return self._active_jobs > 0

    def open_dirs_norm(self):
        return {os.path.normcase(os.path.normpath(p.current_dir))
                for p in self.panels if p.current_dir}

    # ---------- иконки, подгруженные в фоне ----------
    def _icon_wanted(self, path):
        d = os.path.normcase(os.path.normpath(os.path.dirname(path)))
        return d in self.open_dirs_norm()

    def _icon_ready(self, path, photo):
        for p in self.panels:
            try:
                if p.tree.exists(path):
                    p.tree.item(path, image=photo)
            except tk.TclError:
                pass

    def _toggle_dual(self):
        want = self.dual.get()
        p2 = self.panels[1]
        if want and not p2.current_dir:
            p2.navigate_to(self.active_panel.current_dir
                           or os.path.expanduser("~"))
        # несколько попыток — PanedWindow не сразу получает финальную ширину
        for delay in (0, 30, 90, 200, 400):
            self.root.after(delay, self._apply_sash_state)
        self.settings["dual"] = want

    def _apply_sash_state(self):
        try:
            self.list_container.update_idletasks()
            if len(self.list_container.panes()) < 2:
                return
            w = self.list_container.winfo_width()
            if w < 200:
                return
            if self.dual.get():
                self.list_container.sashpos(0, int(w * self._dual_ratio))
            else:
                self.list_container.sashpos(0, w)
        except tk.TclError:
            pass

    def _on_list_container_configure(self, event=None):
        # В режиме 2 панелей sash принадлежит пользователю — не трогаем.
        if self.dual.get():
            return
        self.root.after_idle(self._apply_sash_state)

    def _sync_panel_fixed_flags(self):
        # оставлено для совместимости — колонки больше не двигаются физически
        for p in self.panels:
            p.set_fixed_last_column(True)

    # ---------- UI ----------
    def _build_ui(self):
        title = "Explorer--" + ("" if is_admin() else "  [не админ]")
        self.root.title(title)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        top = tk.Frame(self.root, bg=DARK["bg"])
        top.pack(fill="x", padx=6, pady=(6, 2))

        self.btn_back    = ttk.Button(top, text="←", width=3, command=self.go_back)
        self.btn_forward = ttk.Button(top, text="→", width=3, command=self.go_forward)
        self.btn_up      = ttk.Button(top, text="↑", width=3, command=self.go_up)
        self.btn_refresh = ttk.Button(top, text="⟳", width=3, command=self.refresh)
        self.btn_search  = ttk.Button(top, text="🔍", width=3, command=self.open_search)
        self.btn_new     = tk.Menubutton(top, text="＋", width=3,
                                          bg=DARK["input_bg"], fg=DARK["fg"],
                                          activebackground=DARK["select_bg"],
                                          activeforeground=DARK["select_fg"],
                                          relief="flat", bd=1)
        new_menu = tk.Menu(self.btn_new, tearoff=0,
                           bg=DARK["input_bg"], fg=DARK["fg"],
                           activebackground=DARK["select_bg"],
                           activeforeground=DARK["select_fg"])
        new_menu.add_command(label="Новая папка", command=self.create_folder)
        new_menu.add_command(label="Новый файл", command=self.create_file)
        new_menu.add_separator()
        for ext in ("txt", "py", "json", "md", "csv"):
            new_menu.add_command(
                label=f".{ext}",
                command=lambda e=ext: self.create_file(default_ext=e))
        self.btn_new.configure(menu=new_menu)

        for w in (self.btn_back, self.btn_forward, self.btn_up,
                  self.btn_refresh, self.btn_search, self.btn_new):
            w.pack(side="left", padx=1)

        self.address_var = tk.StringVar()
        self.address_bar = ttk.Entry(top, textvariable=self.address_var)
        self.address_bar.pack(side="left", fill="x", expand=True, padx=6)
        self.address_bar.bind("<Return>", lambda e: self.open_path_from_bar())
        self.address_bar.bind("<KeyRelease>", self._on_address_key)
        self.address_bar.bind("<Down>", self._on_address_down)
        self.address_bar.bind("<Escape>", lambda e: self._hide_suggest())
        self.address_bar.bind("<FocusOut>",
                              lambda e: self.root.after(150, self._hide_suggest))

        self.cb_hidden = tk.Checkbutton(
            top, text="Скрытые", variable=self.hidden_var,
            command=self._on_hidden_toggled,
            bg=DARK["bg"], fg=DARK["fg"], selectcolor=DARK["select_bg"],
            activebackground=DARK["bg"], activeforeground=DARK["fg"],
            highlightthickness=0, bd=0)
        self.cb_hidden.pack(side="left", padx=4)

        self.cb_dual = tk.Checkbutton(
            top, text="2 панели", variable=self.dual, command=self._toggle_dual,
            bg=DARK["bg"], fg=DARK["fg"], selectcolor=DARK["select_bg"],
            activebackground=DARK["bg"], activeforeground=DARK["fg"],
            highlightthickness=0, bd=0)
        self.cb_dual.pack(side="left", padx=4)

        # автодополнение
        self.suggest_list = tk.Listbox(
            self.root, bg=DARK["input_bg"], fg=DARK["fg"],
            selectbackground=DARK["select_bg"], selectforeground=DARK["select_fg"],
            bd=1, relief="solid", highlightthickness=0,
            activestyle="none", exportselection=False, font=("Segoe UI", 9))
        self.suggest_list.bind("<<ListboxSelect>>", self._on_suggest_select)
        self.suggest_list.bind("<Double-Button-1>", self._apply_suggest)
        self.suggest_list.bind("<Return>", self._apply_suggest)
        self.suggest_list.bind("<Escape>", lambda e: self._hide_suggest())
        self._suggest_items = []

        # закладки
        bm_frame = tk.Frame(self.root, bg=DARK["bg"])
        bm_frame.pack(fill="x", padx=6, pady=(0, 2))
        self.bm_list = tk.Listbox(bm_frame, height=3,
                                   bg=DARK["list_bg"], fg=DARK["fg"],
                                   selectbackground=DARK["select_bg"],
                                   selectforeground=DARK["select_fg"],
                                   bd=0, highlightthickness=0, activestyle="none")
        self.bm_list.pack(side="left", fill="x", expand=True)
        self.bm_list.bind("<Double-Button-1>", lambda e: self._goto_bookmark())
        self.bm_list.bind("<Button-3>", self._bookmark_menu)

        # paned
        self.paned = ttk.PanedWindow(self.root, orient="horizontal")
        self.paned.pack(fill="both", expand=True, padx=6, pady=2)

        tree_wrap = tk.Frame(self.paned, bg=DARK["bg"])
        self.tree = ttk.Treeview(tree_wrap, show="tree", selectmode="browse")
        self.tree.tag_configure("hidden", foreground=DARK["hidden_fg"])
        self.tree.tag_configure("hover",  background=DARK["hover"])
        # плавное колесо и в дереве
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.tree.bind(seq, self._on_tree_wheel, add="+")
        tscroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tscroll.pack(side="right", fill="y")
        self.paned.add(tree_wrap, weight=1)

        self.list_container = ttk.PanedWindow(self.paned, orient="horizontal")
        self.paned.add(self.list_container, weight=3)
        p1 = FilePanel(self.list_container, self)
        self.panels.append(p1)
        self.active_panel = p1

        p2 = FilePanel(self.list_container, self)
        self.panels.append(p2)

        # ОБЕ панели добавляются сразу и всегда — никаких forget/add
        self.list_container.add(p1.frame, weight=1)
        self.list_container.add(p2.frame, weight=1)
        self.root.after_idle(self._apply_sash_state)
        for _d in (30, 90, 200, 400, 800):
            self.root.after(_d, self._apply_sash_state)
        # в одиночном режиме держим sash у правого края при любом ресайзе
        self.list_container.bind("<Configure>", self._on_list_container_configure,
                                 add="+")

        # статус
        bottom = tk.Frame(self.root, bg=DARK["bg"])
        bottom.pack(fill="x", padx=8, pady=(0, 4))
        self.status_var = tk.StringVar(value="Готов")
        tk.Label(bottom, textvariable=self.status_var, anchor="w",
                 bg=DARK["bg"], fg=DARK["muted"],
                 font=("Segoe UI", 9)).pack(side="left")

        # bindings
        self.tree.bind("<<TreeviewOpen>>", self.on_tree_expand)
        self.tree.bind("<Double-Button-1>", self.on_tree_double_click)

        self.root.bind("<F2>", lambda e: self.rename_selected())
        self.root.bind("<Delete>", lambda e: self.delete_selected())
        self.root.bind("<F5>", lambda e: self.refresh())
        self.root.bind("<Control-c>", lambda e: self.copy_selected(cut=False))
        self.root.bind("<Control-x>", lambda e: self.copy_selected(cut=True))
        self.root.bind("<Control-v>", lambda e: self.paste_to_active())
        self.root.bind("<Control-a>", lambda e: self.select_all_active())
        self.root.bind("<Control-f>", lambda e: self.open_search())
        self.root.bind("<F3>", lambda e: self.open_search())
        self.root.bind("<Alt-Return>", lambda e: self.show_properties())

    # ---------- утилиты ----------
    def status(self, text):
        self.status_var.set(text)

    def _set_active_panel(self, panel):
        if panel in self.panels:
            changed = panel is not self.active_panel
            self.active_panel = panel
            if panel.current_dir:
                self.address_var.set(panel.current_dir)
            self._update_title_active()
            if changed and panel.current_dir:
                self._reveal_in_tree(panel.current_dir)

    def _update_title_active(self):
        for p in self.panels:
            if not p.frame.winfo_exists():
                continue
            p.header.configure(bg=DARK["active"] if p is self.active_panel
                               else DARK["input_bg"])

    def active(self):
        return self.active_panel

    # ---------- скрытые ----------
    def is_hidden(self, path):
        try:
            if os.path.basename(path).startswith("."):
                return True
        except Exception:
            pass
        try:
            attrs = os.stat(path).st_file_attributes
            return bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN or
                        attrs & stat.FILE_ATTRIBUTE_SYSTEM)
        except (OSError, AttributeError):
            return False

    def _on_hidden_toggled(self):
        self.show_hidden = bool(self.hidden_var.get())
        self._populate_drives()
        for p in self.panels:
            p.refresh()
        if self.active_panel and self.active_panel.current_dir:
            self._reveal_in_tree(self.active_panel.current_dir)

    # ---------- автодополнение с debounce ----------
    def _suggest_paths(self, text):
        if not text:
            return []
        text = text.replace("/", os.sep)
        drive, rest = os.path.splitdrive(text)
        if rest.endswith(os.sep):
            dirname, prefix = drive + rest, ""
        else:
            dirname, prefix = os.path.split(drive + rest)
            if not dirname:
                dirname = drive + os.sep if drive else os.curdir
        try:
            entries = os.listdir(dirname)
        except OSError:
            return []
        pl = prefix.lower()
        matches = []
        for e in entries:
            if e.lower().startswith(pl):
                full = os.path.join(dirname, e)
                if os.path.isdir(full):
                    full += os.sep
                matches.append(full)
        matches.sort(key=str.lower)
        return matches[:40]

    def _on_address_key(self, event):
        if event.keysym in ("Return", "Escape", "Up", "Down", "Left", "Right",
                            "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R",
                            "Alt_L", "Alt_R", "Home", "End"):
            return
        if self._addr_debounce_id:
            try: self.root.after_cancel(self._addr_debounce_id)
            except Exception: pass
        self._addr_debounce_id = self.root.after(
            ADDR_DEBOUNCE_MS, self._do_address_suggest)

    def _do_address_suggest(self):
        self._addr_debounce_id = None
        items = self._suggest_paths(self.address_var.get())
        self._suggest_items = items
        if not items:
            self._hide_suggest(); return
        self.suggest_list.delete(0, tk.END)
        for it in items:
            self.suggest_list.insert(tk.END, it)
        self.address_bar.update_idletasks()
        x = self.address_bar.winfo_rootx() - self.root.winfo_rootx()
        y = (self.address_bar.winfo_rooty() - self.root.winfo_rooty()
             + self.address_bar.winfo_height())
        w = self.address_bar.winfo_width()
        h = min(200, 18 * len(items) + 4)
        self.suggest_list.place(x=x, y=y, width=w, height=h)
        self.suggest_list.lift()

    def _on_address_down(self, event):
        if not self.suggest_list.winfo_ismapped():
            self._do_address_suggest(); return
        cur = self.suggest_list.curselection()
        idx = (min(cur[0] + 1, self.suggest_list.size() - 1)) if cur else 0
        self.suggest_list.selection_clear(0, tk.END)
        self.suggest_list.selection_set(idx)
        self.suggest_list.activate(idx)
        return "break"

    def _on_suggest_select(self, event):
        cur = self.suggest_list.curselection()
        if cur:
            self.address_var.set(self.suggest_list.get(cur[0]))

    def _apply_suggest(self, event=None):
        cur = self.suggest_list.curselection()
        if cur:
            val = self.suggest_list.get(cur[0])
            self.address_var.set(val)
            self._hide_suggest()
            if os.path.isdir(val):
                self.navigate_to(val)
        return "break"

    def _hide_suggest(self):
        try: self.suggest_list.place_forget()
        except tk.TclError: pass

    # ---------- колесо в дереве ----------
    def _on_tree_wheel(self, event):
        if event.num == 4 or (hasattr(event, "delta") and event.delta > 0):
            self.tree.yview_scroll(-WHEEL_UNITS, "units")
        elif event.num == 5 or (hasattr(event, "delta") and event.delta < 0):
            self.tree.yview_scroll(WHEEL_UNITS, "units")
        return "break"

    # ---------- дерево ----------
    PLACEHOLDER = "Загрузка..."

    def _populate_drives(self):
        expanded = self._collect_expanded()
        self.tree.delete(*self.tree.get_children())
        self._tree_loading.clear()
        if os.name == "nt":
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            drives = [f"{letter}:\\"
                      for i, letter in enumerate(string.ascii_uppercase)
                      if bitmask & (1 << i)]
        else:
            drives = ["/"]
        generic = self.icons.for_file("", is_dir=True)
        need_icons = []
        for d in drives:
            icon = self.icons.cached_drive(d) or generic
            self.tree.insert("", "end", iid=d, text=" " + d,
                             image=icon if icon else "")
            self.tree.insert(d, "end", text=self.PLACEHOLDER)
            if not self.icons.cached_drive(d):
                need_icons.append(d)
        self._restore_expanded(expanded)
        if need_icons:
            self._load_drive_icons_async(need_icons)

    def _load_drive_icons_async(self, drives):
        """Иконки дисков берём в фоне: опрос пустого привода или
        отключённого сетевого диска может занимать секунды."""
        def work():
            for d in drives:
                try:
                    pil = self.icons.drive_pil(d)
                except Exception:
                    pil = None
                try:
                    self.root.after(0, self._set_drive_icon, d, pil)
                except (tk.TclError, RuntimeError):
                    return
        threading.Thread(target=work, daemon=True).start()

    def _set_drive_icon(self, d, pil):
        photo = self.icons.drive_photo(d, pil)
        if photo and self.tree.exists(d):
            try:
                self.tree.item(d, image=photo)
            except tk.TclError:
                pass

    def _collect_expanded(self):
        result = []
        def walk(iid):
            for c in self.tree.get_children(iid):
                if self.tree.item(c, "open"):
                    result.append(c); walk(c)
        for top in self.tree.get_children():
            if self.tree.item(top, "open"):
                result.append(top); walk(top)
        return result

    def _restore_expanded(self, paths):
        """Раскрываем сверху вниз; вложенные узлы — по мере загрузки родителя."""
        self._pending_expand = set(paths)
        for p in paths:
            if self.tree.exists(p) and not self.tree.parent(p):
                self._expand_node_async(p)

    def _has_placeholder(self, item):
        ch = self.tree.get_children(item)
        return len(ch) == 1 and self.tree.item(ch[0], "text") == self.PLACEHOLDER

    def _expand_node_async(self, item):
        self._pending_expand.discard(item)
        if not self.tree.exists(item):
            return
        try:
            self.tree.item(item, open=True)
        except tk.TclError:
            return
        if self._has_placeholder(item):
            self._load_tree_node_async(item)

    def on_tree_expand(self, event=None):
        item = self.tree.focus()
        if item and self._has_placeholder(item):
            self._load_tree_node_async(item)

    def _load_tree_node_async(self, item):
        """Подпапки читаются в потоке; пока идёт чтение, в узле виден
        «Загрузка...», а интерфейс остаётся живым."""
        if item in self._tree_loading:
            return
        self._tree_loading.add(item)
        show_hidden = self.show_hidden

        def work():
            subdirs = []
            try:
                with os.scandir(item) as it:
                    for e in it:
                        try:
                            if not e.is_dir():
                                continue
                        except OSError:
                            continue
                        hidden = entry_is_hidden(e)
                        if hidden and not show_hidden:
                            continue
                        subdirs.append((e.path, hidden, e.name.lower()))
                subdirs.sort(key=lambda t: t[2])
            except OSError:
                subdirs = []
            try:
                self.root.after(0, self._on_tree_node_loaded, item, subdirs)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _on_tree_node_loaded(self, item, subdirs):
        self._tree_loading.discard(item)
        if not self.tree.exists(item):
            return
        for ch in self.tree.get_children(item):
            if self.tree.item(ch, "text") == self.PLACEHOLDER:
                self.tree.delete(ch)
        folder_icon = self.icons.for_file("", is_dir=True)
        for full, hidden, _k in subdirs:
            if self.tree.exists(full):
                continue
            tags = ("hidden",) if hidden else ()
            try:
                self.tree.insert(item, "end", iid=full, text=" " + full,
                                 image=folder_icon if folder_icon else "", tags=tags)
                self.tree.insert(full, "end", text=self.PLACEHOLDER)
            except tk.TclError:
                continue
        for full, _h, _k in subdirs:
            if full in self._pending_expand:
                self._expand_node_async(full)
        if self._reveal_target:
            self._reveal_step()

    # ---------- дерево слева следует за активной панелью ----------
    def _reveal_in_tree(self, path):
        if not path:
            return
        self._reveal_target = os.path.normpath(path)
        self._reveal_step()

    def _find_child(self, node, name):
        low = name.lower() if os.name == "nt" else name
        for c in self.tree.get_children(node):
            base = os.path.basename(c.rstrip("\\/")) or c
            if (base.lower() if os.name == "nt" else base) == low:
                return c
        return None

    def _reveal_step(self):
        """Идём от корня диска к целевой папке; не загруженные узлы подгружаем
        в фоне и продолжаем после загрузки (см. _on_tree_node_loaded)."""
        target = self._reveal_target
        if not target:
            return
        try:
            if os.name == "nt":
                drive, rest = os.path.splitdrive(target)
                if not drive or drive.startswith("\\\\"):
                    self._reveal_target = None
                    return
                root_iid = drive.upper() + "\\"
                comps = [c for c in rest.replace("/", "\\").split("\\") if c]
            else:
                root_iid = "/"
                comps = [c for c in target.split("/") if c]
            if not self.tree.exists(root_iid):
                self._reveal_target = None
                return
            node = root_iid
            for comp in comps:
                if self._has_placeholder(node):
                    try:
                        self.tree.item(node, open=True)
                    except tk.TclError:
                        pass
                    self._load_tree_node_async(node)
                    return                          # продолжим после загрузки узла
                nxt = self._find_child(node, comp)
                if nxt is None:
                    break                           # скрыта или недоступна — показываем что есть
                node = nxt
            self._reveal_target = None
            parent = self.tree.parent(node)
            while parent:
                self.tree.item(parent, open=True)
                parent = self.tree.parent(parent)
            self.tree.selection_set(node)
            self.tree.see(node)
        except tk.TclError:
            self._reveal_target = None

    def on_tree_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and self.tree.item(iid, "text") != self.PLACEHOLDER:
            self.navigate_to(iid)       # узлы дерева — всегда папки/диски

    # ---------- навигация ----------
    def navigate_to(self, path, add_history=True):
        if not path:
            return
        if self.active_panel:
            self.active_panel.navigate_to(path, add_history)
        self._hide_suggest()

    def panel_dir_changed(self, panel):
        """Панель сменила папку (любым способом): синхронизируем адресную
        строку, историю, дерево слева и поиск занятых файлов."""
        if panel is not self.active_panel or not panel.current_dir:
            return
        self.address_var.set(panel.current_dir)
        self._remember_address(panel.current_dir)
        self._update_title_active()
        self._start_busy_scan(panel.current_dir)
        self._hide_suggest()
        self._reveal_in_tree(panel.current_dir)

    on_panel_navigated = panel_dir_changed

    def go_back(self):
        if self.active_panel:
            self.active_panel.go_back()

    def go_forward(self):
        if self.active_panel:
            self.active_panel.go_forward()

    def go_up(self):
        if self.active_panel:
            self.active_panel.go_up()

    def refresh(self):
        for p in self.panels:
            p.refresh()
        if self.active_panel:
            self._start_busy_scan(self.active_panel.current_dir)

    def open_path_from_bar(self):
        path = self.address_var.get().strip().strip('"')
        self._hide_suggest()
        if not path:
            return
        self.status("Проверка пути…")

        def work():
            try:
                kind = ("dir" if os.path.isdir(path)
                        else "file" if os.path.isfile(path) else "none")
            except Exception:
                kind = "none"
            try:
                self.root.after(0, self._open_probed, path, kind)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=work, daemon=True).start()

    def _open_probed(self, path, kind):
        if kind == "dir":
            self.navigate_to(path)
        elif kind == "file":
            if is_archive(path):
                self.open_archive_in_explorer(path)
            else:
                try: os.startfile(path)  # noqa
                except OSError as e: self.status("Не удалось открыть: " + str(e))
        else:
            self.status("Путь не найден: " + path)

    def _remember_address(self, path):
        if path in self.address_history:
            self.address_history.remove(path)
        self.address_history.append(path)
        self.address_history = self.address_history[-50:]

    # ---------- busy scan ----------
    def _start_busy_scan(self, path):
        if not path:
            return
        if self._busy_scan_running:
            self._busy_scan_pending = path      # запомним — запустим после текущего
            return
        self._busy_scan_running = True
        BusyScanWorker(path, self._on_busy_scan).start()

    def _on_busy_scan(self, path, busy):
        def _apply():
            self._busy_scan_running = False
            for p in self.panels:
                if os.path.normcase(p.current_dir) == os.path.normcase(path):
                    p.apply_busy_tags(busy)
            nxt, self._busy_scan_pending = self._busy_scan_pending, None
            if nxt and os.path.normcase(nxt) != os.path.normcase(path):
                self._start_busy_scan(nxt)
        try: self.root.after(0, _apply)
        except tk.TclError: pass

    # ---------- архивы ----------
    def open_archive_in_explorer(self, path):
        if not os.path.isfile(path):
            return
        lower = path.lower()
        name = os.path.basename(path)
        for suf in (".tar.gz", ".tar.bz2", ".tar.xz"):
            if lower.endswith(suf):
                name = name[:-len(suf)]; break
        else:
            name = os.path.splitext(name)[0]
        target = os.path.join(tempfile.gettempdir(), "PyExplorer_archives", name)
        try:
            if os.path.exists(target):
                shutil.rmtree(target, ignore_errors=True)
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            messagebox.showwarning("Ошибка", str(e), parent=self.root); return

        self.status("Распаковка: " + os.path.basename(path))

        def _extract():
            err = None
            try:
                if lower.endswith(".zip"):
                    with zipfile.ZipFile(path, "r") as z:
                        z.extractall(target)
                elif lower.endswith((".tar", ".tar.gz", ".tgz",
                                     ".tar.bz2", ".tar.xz")):
                    with tarfile.open(path, "r:*") as t:
                        t.extractall(target)
                else:
                    try:
                        with zipfile.ZipFile(path, "r") as z:
                            z.extractall(target)
                    except zipfile.BadZipFile:
                        with tarfile.open(path, "r:*") as t:
                            t.extractall(target)
            except Exception as e:
                err = str(e)

            def _done():
                if err:
                    messagebox.showwarning("Ошибка распаковки",
                                           f"{path}\n{err}", parent=self.root)
                else:
                    self.status("Распаковано: " + target)
                    self.navigate_to(target)
            try: self.root.after(0, _done)
            except tk.TclError: pass

        threading.Thread(target=_extract, daemon=True).start()

    # ---------- действия ----------
    def select_all_active(self):
        if self.active_panel:
            self.active_panel.select_all()

    def rename_selected(self):
        p = self.active_panel
        if not p: return
        sel = p.selected_paths()
        if sel: self._rename_item(sel[0])

    def delete_selected(self):
        p = self.active_panel
        if not p: return
        sel = p.selected_paths()
        if not sel: return
        if not messagebox.askyesno("Удаление",
                                   f"Удалить {len(sel)} объект(ов)?",
                                   parent=self.root):
            return
        p.remember_near()               # после удаления выделим соседний элемент
        self._delete_paths(sel)

    def _delete_paths(self, paths, refresh_cb=None):
        job = FileJob("delete", paths, None, self._job_ask)
        self._launch_job(job, refresh_cb)

    def copy_selected(self, cut=False):
        p = self.active_panel
        if not p: return
        sel = p.selected_paths()
        if not sel: return
        self.clipboard = (list(sel), "cut" if cut else "copy")
        self.status(f"{'Вырезано' if cut else 'Скопировано'}: {len(sel)}")

    def paste_to_active(self):
        p = self.active_panel
        if not p or not self.clipboard or not p.current_dir: return
        srcs, mode = self.clipboard
        self.start_transfer(srcs, p.current_dir, move=(mode == "cut"))
        if mode == "cut":
            self.clipboard = None

    def start_transfer(self, sources, dst_dir, move=False):
        """Копирование/перенос в фоне (в т.ч. из Drag&Drop)."""
        if not sources or not dst_dir:
            return
        job = FileJob("move" if move else "copy", sources, dst_dir, self._job_ask)
        self._launch_job(job)

    # ---------- фоновые задания ----------
    def _launch_job(self, job, refresh_cb=None):
        self._active_jobs += 1
        self._jobs.append(job)
        job.start()
        ctx = {"win": None, "cb": refresh_cb}
        self.root.after(100, self._poll_job, job, ctx)

    def _poll_job(self, job, ctx):
        st = job.state
        if st.finished:
            self._finish_job(job, ctx)
            return
        if ctx["win"] is None and time.monotonic() - st.started > 0.35:
            try:
                ctx["win"] = JobWindow(self.root, job)
            except tk.TclError:
                ctx["win"] = None
        if ctx["win"] is not None:
            ctx["win"].refresh_view()
        verb = {"delete": "Удаление", "copy": "Копирование",
                "move": "Перемещение"}.get(job.kind, "Операция")
        self.status(f"{verb}: {_short_path(st.current, 90)}")
        self.root.after(100, self._poll_job, job, ctx)

    def _finish_job(self, job, ctx):
        st = job.state
        if ctx["win"] is not None:
            try:
                ctx["win"].destroy()
            except tk.TclError:
                pass
        self._active_jobs = max(0, self._active_jobs - 1)
        if job in self._jobs:
            self._jobs.remove(job)
        verb = {"delete": "Удалено", "copy": "Скопировано",
                "move": "Перемещено"}.get(job.kind, "Готово")
        n = st.deleted if job.kind == "delete" else st.copied
        parts = [f"{verb}: {n}"]
        if st.skipped: parts.append(f"пропущено: {st.skipped}")
        if st.errors:  parts.append(f"ошибок: {st.errors}")
        if st.killed:  parts.append(f"завершено процессов: {st.killed}")
        text = ", ".join(parts)
        if st.cancelled:
            text = "Прервано. " + text
        self.status(text)

        # выделяем то, что появилось в папке назначения
        if job.kind != "delete" and st.created and job.dst_dir:
            target = None
            for p in [self.active_panel] + self.panels:
                if p and p.current_dir and os.path.normcase(os.path.normpath(
                        p.current_dir)) == os.path.normcase(os.path.normpath(job.dst_dir)):
                    target = p
                    break
            if target is not None:
                target.select_after(st.created)
        for p in self.panels:
            p.refresh()
        if ctx["cb"]:
            try: ctx["cb"]()
            except Exception: pass
        # ошибки, которые пользователь «пропустил все», — одним итогом
        if job.skip_all and st.error_log:
            lines = st.error_log[:12]
            more = f"\n…и ещё {len(st.error_log) - 12}" if len(st.error_log) > 12 else ""
            messagebox.showwarning("Не всё выполнено",
                                   "\n\n".join(lines) + more, parent=self.root)

    def _job_ask(self, what, **kw):
        """Вызывается из рабочего потока: показывает диалог в главном потоке
        и ждёт ответа."""
        box = {}
        ev = threading.Event()

        def run():
            try:
                box["r"] = self._job_ask_ui(what, **kw)
            except Exception as e:
                print("Ошибка диалога операции:", e)
                box["r"] = None
            finally:
                ev.set()
        try:
            self.root.after(0, run)
        except (tk.TclError, RuntimeError):
            return None
        while not ev.wait(0.2):
            if self._closing:
                return None
        return box.get("r")

    def _job_ask_ui(self, what, **kw):
        if what == "conflict":
            dlg = ConflictDialog(self.root, kw["src"], kw["dst"])
            self.root.wait_window(dlg)
            return dlg.result
        if what == "error":
            dlg = OpErrorDialog(self.root, kw["path"], kw["text"],
                                kw.get("can_retry", True))
            self.root.wait_window(dlg)
            return dlg.result
        if what == "kill":
            procs, path = kw["procs"], kw["path"]
            if len(procs) == 1:
                p = procs[0]
                ok = messagebox.askyesno(
                    "Завершить процесс?",
                    f"Объект:\n{path}\n\nИмя совпадает, но путь другой:\n"
                    f"  {p['name']} (PID {p['pid']})\n  {p['exe']}\n\nЗавершить его?",
                    parent=self.root)
                return [p] if ok else None
            dlg = ProcessChooserDialog(self.root, procs)
            self.root.wait_window(dlg)
            return dlg.result
        return None

    # ---------- контекстное меню ----------
    def show_context_menu(self, event, iid, panel):
        menu = tk.Menu(self.root, tearoff=0, bg=DARK["input_bg"], fg=DARK["fg"],
                       activebackground=DARK["select_bg"],
                       activeforeground=DARK["select_fg"], bd=1, relief="solid")
        if iid:
            sel = panel.selected_paths()
            menu.add_command(label="Открыть", command=self.open_selected)
            if any(is_archive(p) for p in sel):
                menu.add_command(label="Открыть архив",
                                 command=lambda: [self.open_archive_in_explorer(p)
                                                  for p in sel if is_archive(p)])
            menu.add_command(label="Открыть с помощью...", command=self.open_with)
            menu.add_command(label="Открыть в терминале", command=self.open_in_terminal)
            menu.add_command(label="Открыть в редакторе", command=self.open_in_editor)
            menu.add_separator()
            menu.add_command(label="Копировать  (Ctrl+C)",
                             command=lambda: self.copy_selected(False))
            menu.add_command(label="Вырезать  (Ctrl+X)",
                             command=lambda: self.copy_selected(True))
            menu.add_command(label="Копировать путь",
                             command=lambda: self.copy_paths_to_clip(sel))
            menu.add_command(label="Копировать имя",
                             command=lambda: self.copy_names_to_clip(sel))
            menu.add_separator()
            menu.add_command(label="Переименовать  (F2)", command=self.rename_selected)
            menu.add_command(label="Свойства  (Alt+Enter)",
                             command=lambda: [PropertiesDialog(self.root, p)
                                              for p in sel[:5]])
            menu.add_command(label="Хеш-суммы...",
                             command=lambda: HashDialog(self.root, sel))
            menu.add_command(label="Добавить в закладки",
                             command=lambda: self.add_bookmark(sel[0]))
            menu.add_separator()
            if any(p.lower().endswith(".zip") for p in sel):
                menu.add_command(label="Распаковать здесь",
                                 command=lambda: self.extract_zips(sel, panel.current_dir))
            menu.add_command(label="Разблокировать (снять атрибуты)",
                             command=lambda: self.unblock(sel))
            menu.add_separator()
            menu.add_command(label="Удалить  (Delete)", command=self.delete_selected)
        else:
            menu.add_command(label="Создать папку", command=self.create_folder)
            menu.add_command(label="Создать файл", command=self.create_file)
            if self.clipboard:
                menu.add_separator()
                menu.add_command(label="Вставить  (Ctrl+V)", command=self.paste_to_active)
            menu.add_separator()
            menu.add_command(label="Открыть терминал здесь", command=self.open_in_terminal)
            menu.add_command(label="Добавить текущую в закладки",
                             command=lambda: self.add_bookmark(panel.current_dir))
            menu.add_command(label="Поиск...  (Ctrl+F)", command=self.open_search)
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def open_selected(self):
        p = self.active_panel
        if not p: return
        for iid in p.selected_paths():
            if os.path.isdir(iid):
                p.navigate_to(iid)
            elif is_archive(iid):
                self.open_archive_in_explorer(iid)
            else:
                try: os.startfile(iid)  # noqa
                except OSError as e: self.status("Не удалось открыть: " + str(e))

    def copy_paths_to_clip(self, paths):
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(paths))
        self.status("Пути скопированы")

    def copy_names_to_clip(self, paths):
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(os.path.basename(p) for p in paths))
        self.status("Имена скопированы")

    def open_with(self):
        p = self.active_panel
        if not p: return
        sel = p.selected_paths()
        if not sel: return
        if not open_with_dialog(sel[0]):
            messagebox.showwarning("Ошибка",
                                   "Не удалось вызвать «Открыть с помощью».",
                                   parent=self.root)

    def open_in_terminal(self):
        p = self.active_panel
        if not p:
            return
        sel = p.selected_paths()
        target = sel[0] if sel else p.current_dir
        if os.path.isfile(target):
            target = os.path.dirname(target)
        try:
            if os.name == "nt":
                # В WinPE классический cmd есть, start — тоже.
                subprocess.Popen(f'start "" cmd /K cd /d "{target}"', shell=True)
            else:
                subprocess.Popen(["x-terminal-emulator"], cwd=target)
        except Exception as e:
            messagebox.showwarning("Ошибка", str(e), parent=self.root)

    def open_in_editor(self):
        p = self.active_panel
        if not p: return
        sel = p.selected_paths()
        if not sel: return
        try: subprocess.Popen([self.editor_cmd, *sel])
        except Exception as e:
            messagebox.showwarning("Ошибка", str(e), parent=self.root)

    def show_properties(self):
        p = self.active_panel
        if not p: return
        for path in p.selected_paths():
            PropertiesDialog(self.root, path)

    def open_search(self):
        p = self.active_panel
        start = p.current_dir if p else os.path.expanduser("~")
        SearchDialog(self.root, start, self)

    # ---------- создание ----------
    def create_folder(self):
        p = self.active_panel
        if not p or not p.current_dir: return
        name = simpledialog.askstring("Новая папка", "Имя папки:", parent=self.root)
        if not name: return
        name = os.path.basename(name.replace("\\", "/").rstrip("/"))
        if not name: return
        try:
            newp = os.path.join(p.current_dir, name)
            os.mkdir(lp(newp)); p.select_after([newp]); p.refresh()
        except OSError as e:
            messagebox.showwarning("Ошибка", str(e), parent=self.root)

    def create_file(self, default_ext=None):
        p = self.active_panel
        if not p or not p.current_dir: return
        initial = f"новый.{default_ext}" if default_ext else "новый.txt"
        name = simpledialog.askstring("Новый файл",
                                      "Имя файла (с расширением):",
                                      initialvalue=initial, parent=self.root)
        if not name: return
        name = os.path.basename(name.replace("\\", "/").rstrip("/"))
        if not name or name in (".", ".."): return
        path = os.path.join(p.current_dir, name)
        if os.path.exists(lp(path)):
            messagebox.showwarning("Ошибка", "Уже существует", parent=self.root); return
        try:
            with open(lp(path), "x", encoding="utf-8"): pass
            p.select_after([path]); p.refresh(); self.status("Создан: " + path)
        except OSError as e:
            messagebox.showwarning("Ошибка", str(e), parent=self.root)

    def _rename_item(self, path):
        old_name = os.path.basename(path)
        new_name = simpledialog.askstring("Переименовать", "Новое имя:",
                                          initialvalue=old_name, parent=self.root)
        if new_name and new_name != old_name:
            new_path = os.path.join(os.path.dirname(path), new_name)
            try:
                os.rename(lp(path), lp(new_path))
                if self.active_panel:
                    self.active_panel.select_after([new_path])
                    self.active_panel.refresh()
            except OSError as e:
                messagebox.showwarning("Ошибка", str(e), parent=self.root)

    # ---------- закладки ----------
    def _apply_bookmarks(self):
        self.bm_list.delete(0, tk.END)
        for p in self.bookmarks:
            self.bm_list.insert(tk.END, p)

    def add_bookmark(self, path):
        if path and path not in self.bookmarks:
            self.bookmarks.append(path); self._apply_bookmarks()

    def _goto_bookmark(self):
        sel = self.bm_list.curselection()
        if sel:
            self.navigate_to(self.bm_list.get(sel[0]))

    def _bookmark_menu(self, event):
        idx = self.bm_list.nearest(event.y)
        if idx < 0 or idx >= len(self.bookmarks): return
        m = tk.Menu(self.root, tearoff=0, bg=DARK["input_bg"], fg=DARK["fg"],
                    activebackground=DARK["select_bg"],
                    activeforeground=DARK["select_fg"])
        m.add_command(label="Открыть",
                      command=lambda: self.navigate_to(self.bookmarks[idx]))
        m.add_command(label="Удалить", command=lambda: self._remove_bookmark(idx))
        try: m.tk_popup(event.x_root, event.y_root)
        finally: m.grab_release()

    def _remove_bookmark(self, idx):
        if 0 <= idx < len(self.bookmarks):
            del self.bookmarks[idx]; self._apply_bookmarks()

    def extract_zips(self, paths, target_dir):
        for p in paths:
            if not p.lower().endswith(".zip"): continue
            try:
                with zipfile.ZipFile(p, "r") as z:
                    z.extractall(target_dir)
                self.status("Распакован: " + os.path.basename(p))
            except Exception as e:
                messagebox.showwarning("Ошибка распаковки",
                                       f"{p}\n{e}", parent=self.root)
        for p in self.panels: p.refresh()

    def unblock(self, paths):
        def _unblock(path):
            try:
                os.chmod(lp(path), stat.S_IWRITE)
                if os.path.isdir(lp(path)):
                    for root, dirs, files in os.walk(lp(path)):
                        for f in files + dirs:
                            fp = os.path.join(root, f)
                            try: os.chmod(fp, stat.S_IWRITE)
                            except OSError: pass
            except OSError as e:
                messagebox.showwarning("Ошибка", str(e), parent=self.root)
        for p in paths: _unblock(p)
        for p in self.panels: p.refresh()

    # ---------- две панели ----------
    def _unfreeze_frame_widths(self):
        for p in self.panels:
            try:
                p.frame.configure(width=1)
            except tk.TclError:
                pass
        try:
            self.list_container.update_idletasks()
        except tk.TclError:
            pass

    def _reset_list_sash(self):
        try:
            self.list_container.update_idletasks()
            if len(self.list_container.panes()) < 2:
                return
            w = self.list_container.winfo_width()
            if w < 200:
                return
            self.list_container.sashpos(0, int(w * self._dual_ratio))
        except tk.TclError:
            pass

    # ---------- сохранение ----------
    def _restore_layout(self):
        geom = self.settings.get("geometry")
        try: self.root.geometry(geom if geom else f"{S(1200)}x{S(720)}")
        except tk.TclError: self.root.geometry(f"{S(1200)}x{S(720)}")
        for delay in (150, 400, 900):
            self.root.after(delay, self._restore_tree_width)

    def _restore_tree_width(self):
        try:
            tw = self.settings.get("tree_w")
            if tw:
                self.paned.sashpos(0, S(max(80, int(tw))))
        except (tk.TclError, TypeError, ValueError):
            pass

    def _on_close(self):
        if self._active_jobs:
            if not messagebox.askyesno(
                    "Операции выполняются",
                    "Файловые операции ещё не завершены.\nПрервать их и выйти?",
                    parent=self.root):
                return
            for j in list(self._jobs):
                j.cancel()
        self._closing = True
        try: self.settings["geometry"] = self.root.winfo_geometry()
        except tk.TclError: pass
        self.settings["show_hidden"] = self.show_hidden
        self.settings["dual"] = self.dual.get()
        self.settings["last_dir"] = self.active_panel.current_dir if self.active_panel else ""
        self.settings["address_history"] = self.address_history[-50:]
        self.settings["bookmarks"] = self.bookmarks
        self.settings["editor_cmd"] = self.editor_cmd
        self.settings["panels"] = [
            {"dir": p.current_dir, "sort_key": p.sort_key, "sort_dir": p.sort_dir}
            for p in self.panels]
        self.settings["col_widths"] = {
            k: int(round(v / UI_SCALE)) for k, v in self.col_widths.items()}
        try:
            self.settings["tree_w"] = int(round(self.paned.sashpos(0) / UI_SCALE))
        except tk.TclError:
            pass
        try:
            if self.dual.get():
                w = self.list_container.winfo_width()
                if w > 200:
                    self.settings["dual_ratio"] = round(
                        self.list_container.sashpos(0) / w, 3)
        except tk.TclError:
            pass
        save_settings(self.settings)
        for p in self.panels:
            p.dispose()
        self.root.destroy()

# ===========================================================================
# Запуск
# ===========================================================================
def run_selfcheck():
    """ExplorerPE.exe --check : что доступно в этой среде (удобно проверять в WinPE).
    Результат печатается и пишется в selfcheck.txt рядом с настройками — это
    работает и в оконной сборке, где консоли нет."""
    lines = []

    def out(label, value=""):
        lines.append(f"  {label:<20} {value}".rstrip())

    def yn(v):
        return "да" if v else "НЕТ"

    lines.append("Explorer-- self-check")
    out("Python:", sys.version.split()[0] + (" (64-bit)" if sys.maxsize > 2 ** 32 else " (32-bit)"))
    out("Windows / WinPE:", f"{yn(os.name == 'nt')} / {yn(WINPE)}")
    out("Администратор:", yn(is_admin()))
    out("psutil:", yn(HAS_PSUTIL) + ("" if HAS_PSUTIL else "  (работает запасной слой ctypes)"))
    try:
        import PIL  # noqa
        pil = True
    except ImportError:
        pil = False
    out("Pillow (иконки):", yn(pil))
    out("tkinterdnd2 (DnD):", yn(HAS_DND))
    out("Restart Manager:", yn(_RM is not None) + ("" if _RM is not None else "  (будет psutil/ctypes)"))
    out("Папка настроек:", APP_DIR)
    text = "\n".join(lines)
    print(text)
    try:
        with open(os.path.join(APP_DIR, "selfcheck.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    return 0


def main():
    if "--check" in sys.argv:
        sys.exit(run_selfcheck())
    enable_dpi_awareness()            # до создания окна Tk
    root = TkBase()
    init_dpi(root)
    setup_styles(root)
    if relaunch_as_admin():
        sys.exit(0)
    settings = load_settings()
    ExplorerWindow(root, settings)
    if not HAS_DND:
        root.after(500, lambda: messagebox.showinfo(
            "Drag & Drop",
            "Модуль tkinterdnd2 не найден — перетаскивание отключено.\n"
            "Установите: pip install tkinterdnd2"))
    root.mainloop()


if __name__ == "__main__":
    main()
