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
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import psutil

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
INSERT_BATCH     = 99                     # сколько строк за один кадр
HOVER_THROTTLE   = 1.0 / FPS              # не чаще 60 раз в секунду
WHEEL_UNITS      = 3                      # строк за один "щелчок" колеса
ADDR_DEBOUNCE_MS = 120

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
        for root, _dirs, files in os.walk(path):
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
# Иконки
# ===========================================================================
if os.name == "nt":
    _shell32 = ctypes.windll.shell32
    _user32  = ctypes.windll.user32
    _gdi32   = ctypes.windll.gdi32

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
        hdc = _user32.GetDC(0)
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
            _user32.ReleaseDC(0, hdc)


class IconCache:
    _SPECIFIC = {".exe", ".lnk", ".ico", ".url", ".msi", ".scr", ".cpl"}

    def __init__(self):
        self._cache = {}
        self._available = False
        if os.name != "nt":
            return
        try:
            from PIL import Image, ImageTk  # noqa
            self._ImageTk = ImageTk
            self._available = True
        except ImportError:
            pass

    def for_file(self, path, is_dir=False, is_drive=False):
        if not self._available:
            return None
        if is_drive:
            return self._get("__drive__:" + path[:2], path, False, True)
        if is_dir:
            return self._get("__folder__", "folder", True, True)
        ext = os.path.splitext(path)[1].lower()
        if ext in self._SPECIFIC:
            return self._get(path, path, False, False)
        key = ext or "__noext__"
        query = ("file" + ext) if ext else "file"
        return self._get(key, query, True, False)

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
            return _hicon_to_pil(info.hIcon, 16)
        finally:
            _user32.DestroyIcon(info.hIcon)


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
                    bordercolor=DARK["border"], rowheight=22, borderwidth=0)
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
    """Асинхронно читает содержимое директории и возвращает готовый список записей."""
    def __init__(self, path, show_hidden, is_hidden_fn, callback):
        super().__init__(daemon=True)
        self.path = path
        self.show_hidden = show_hidden
        self.is_hidden = is_hidden_fn
        self.callback = callback

    def run(self):
        try:
            names = os.listdir(self.path)
        except PermissionError:
            self._done([], "Нет доступа: " + self.path)
            return
        except OSError as e:
            self._done([], "Ошибка: " + str(e))
            return

        entries = []
        for name in names:
            full = os.path.join(self.path, name)
            try:
                hidden = self.is_hidden(full)
            except Exception:
                hidden = False
            if not self.show_hidden and hidden:
                continue
            try:
                st = os.stat(full)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                is_dir = False
            entries.append({"name": name, "full": full, "is_dir": is_dir,
                            "size": size, "mtime": mtime, "hidden": hidden})
        self._done(entries, None)

    def _done(self, entries, err):
        try:
            self.callback(entries, err)
        except Exception:
            pass


class ProcessSearchWorker(threading.Thread):
    def __init__(self, filepath, callback):
        super().__init__(daemon=True)
        self.filepath = filepath
        self.callback = callback

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
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not exact:
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
                except (psutil.AccessDenied, psutil.NoSuchProcess):
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
    def __init__(self, folderpath, callback):
        super().__init__(daemon=True)
        self.folderpath = folderpath
        self.callback = callback

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
            except (psutil.NoSuchProcess, psutil.AccessDenied):
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
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue

        for proc in psutil.process_iter(["pid", "name"]):
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
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        seen, nm = set(), []
        for p in name_match:
            if p["pid"] not in seen and p["pid"] not in exact_pids:
                seen.add(p["pid"]); nm.append(p)
        return {"exact": exact, "name_match": nm}


class BusyScanWorker(threading.Thread):
    def __init__(self, dir_path, callback):
        super().__init__(daemon=True)
        self.dir_path = dir_path
        self.callback = callback

    def run(self):
        busy = {}
        try:
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
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
        except Exception as e:
            print("Ошибка сканирования занятых:", e)
        try:
            self.callback(self.dir_path, busy)
        except Exception:
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
            st = os.stat(path)
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


HEADER_HEIGHT = 22

# ===========================================================================
# Панель списка файлов
# ===========================================================================
class FilePanel:
    def __init__(self, parent, explorer):
        self.explorer = explorer
        self.current_dir = ""
        self.history = []
        self.history_index = -1
        self.sort_key = "name"
        self.sort_dir = 1
        self.busy = {}
        self._entries = []

        # --- плавность ---
        self._load_token = 0
        self._pending_render = None
        self._render_index = 0
        self._hover_item = None
        self._last_hover_ts = 0.0
        self._render_job = None

        # --- фиксация последней колонки ---
        # Все data-колонки жёстко фиксированы по ширине, тянуть их физически
        # нечем — у нашего заголовка нет разделителей.
        self._last_col_fixed = True

        self.frame = tk.Frame(parent, bg=DARK["bg"])

        # Полоска с текущим путём
        self.header = tk.Label(self.frame, text="", bg=DARK["input_bg"],
                               fg=DARK["fg"], anchor="w", padx=6)
        self.header.pack(fill="x")

        body = tk.Frame(self.frame, bg=DARK["bg"])
        body.pack(fill="both", expand=True)
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0, minsize=16)

        # --- КАСТОМНЫЙ заголовок колонок ---
        self._col_header = tk.Frame(body, bg=DARK["input_bg"],
                                    height=HEADER_HEIGHT)
        self._col_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._col_header.grid_propagate(False)
        self._header_labels = {}
        self._build_col_header()

        # --- Treeview БЕЗ штатного заголовка ---
        self.tree = ttk.Treeview(
            body, columns=("size", "type", "mtime"),
            show="tree", selectmode="extended")
        # #0 растягивается, а size/type/mtime — жёстко фиксированы
        self.tree.column("#0",    width=280, stretch=True,  minwidth=120)
        self.tree.column("size",  width=90,  stretch=False, minwidth=90)
        self.tree.column("type",  width=100, stretch=False, minwidth=100)
        self.tree.column("mtime", width=140, stretch=False, minwidth=140)

        self.tree.tag_configure("hidden", foreground=DARK["hidden_fg"])
        self.tree.tag_configure("busy",   foreground=DARK["busy_fg"])
        self.tree.tag_configure("hover",  background=DARK["hover"])

        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")

        # --- события ---
        self.tree.bind("<Double-Button-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_context)
        self.tree.bind("<Button-1>", self._on_click, add="+")
        self.tree.bind("<<TreeviewSelect>>",
                       lambda e: explorer._set_active_panel(self))
        self.tree.bind("<Motion>", self._on_motion, add="+")
        self.tree.bind("<Leave>",  self._on_leave,  add="+")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.tree.bind(seq, self._on_wheel, add="+")
        # синхронизация кастомного заголовка с колонками
        self.tree.bind("<Configure>", self._sync_header, add="+")

        if HAS_DND:
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._on_drop)

        # отрисовать заголовок после того, как окно получит геометрию
        self.frame.after_idle(self._sync_header)

    # ---------- кастомный заголовок ----------
    def _build_col_header(self):
        for w in self._col_header.winfo_children():
            w.destroy()
        self._header_labels = {}

        defs = [
            ("name",  "Имя",      280),
            ("size",  "Размер",   90),
            ("type",  "Тип",      100),
            ("mtime", "Изменён",  140),
        ]
        x = 0
        for key, text, w in defs:
            lbl = tk.Label(self._col_header, text=text,
                           bg=DARK["input_bg"], fg=DARK["fg"],
                           anchor="w", padx=6, cursor="hand2")
            lbl.place(x=x, y=0, width=w, height=HEADER_HEIGHT)
            lbl.bind("<Button-1>", lambda e, k=key: self._sort_by(k))
            self._header_labels[key] = lbl
            x += w

    def _sync_header(self, event=None):
        """Выставить позиции меток заголовка строго по фактическим колонкам Treeview."""
        try:
            w0 = self.tree.column("#0", "width")
            self._header_labels["name"].place_configure(x=0, width=w0)
            x = w0
            for key in ("size", "type", "mtime"):
                w = self.tree.column(key, "width")
                self._header_labels[key].place_configure(x=x, width=w)
                x += w
        except (tk.TclError, KeyError):
            pass

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

    # ---------- фиксация последней колонки ----------
    def set_fixed_last_column(self, fixed):
        """Оставлено для совместимости — теперь всегда True и без эффекта."""
        self._last_col_fixed = True

    # ---------- навигация ----------
    def navigate_to(self, path, add_history=True):
        if not os.path.isdir(path):
            return False
        self.current_dir = path
        if add_history:
            self.history = self.history[:self.history_index + 1]
            if not self.history or self.history[-1] != path:
                self.history.append(path)
                self.history_index = len(self.history) - 1
        self.refresh()
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
        parent = os.path.dirname(self.current_dir.rstrip("\\/"))
        if parent and os.path.isdir(parent):
            self.navigate_to(parent)

    # ---------- refresh ----------
    def refresh(self):
        self._load_token += 1
        token = self._load_token
        if self._render_job:
            try:
                self.frame.after_cancel(self._render_job)
            except Exception:
                pass
            self._render_job = None
        self._pending_render = None
        self._render_index = 0
        self._entries = []
        self.tree.delete(*self.tree.get_children())
        if not self.current_dir:
            return
        self.header.configure(text=self.current_dir + "   (загрузка…)")

        def _on_ready(entries, err):
            try:
                self.frame.after(0, self._on_loaded, token, entries, err)
            except tk.TclError:
                pass

        DirLoader(self.current_dir, self.explorer.show_hidden,
                  self.explorer.is_hidden, _on_ready).start()

    def _on_loaded(self, token, entries, err):
        if token != self._load_token:
            return
        if err:
            self.explorer.status(err)
            self.header.configure(text=self.current_dir)
            return
        self._entries = entries
        self._apply_sort()
        self._pending_render = list(self._entries)
        self._render_index = 0
        self._pump_render(token)

    def _pump_render(self, token):
        if token != self._load_token or self._pending_render is None:
            return
        icons = self.explorer.icons
        end = min(self._render_index + INSERT_BATCH, len(self._pending_render))
        for e in self._pending_render[self._render_index:end]:
            icon = icons.for_file(e["full"], is_dir=e["is_dir"])
            tags = []
            if e["hidden"]:
                tags.append("hidden")
            if os.path.normcase(e["full"]) in self.busy:
                tags.append("busy")
            try:
                self.tree.insert(
                    "", "end", iid=e["full"],
                    text=" " + e["name"],
                    image=icon if icon else "",
                    values=(("" if e["is_dir"] else human_size(e["size"])),
                            file_type_name(e["full"], e["is_dir"]),
                            fmt_mtime(e["mtime"])),
                    tags=tuple(tags))
            except tk.TclError:
                return
        self._render_index = end
        if self._render_index < len(self._pending_render):
            self._render_job = self.frame.after(FRAME_MS, self._pump_render, token)
        else:
            self._render_job = None
            self._pending_render = None
            n_dirs = sum(1 for e in self._entries if e["is_dir"])
            n_files = len(self._entries) - n_dirs
            self.header.configure(text=self.current_dir or "(пусто)")
            self.explorer.status(
                f"{self.current_dir}   |   папок: {n_dirs}, файлов: {n_files}")

    def _apply_sort(self):
        key = self.sort_key
        rev = self.sort_dir < 0

        def norm(e):
            if key == "name":  return e["name"].lower()
            if key == "size":  return e["size"] if not e["is_dir"] else -1
            if key == "type":  return file_type_name(e["full"], e["is_dir"]).lower()
            if key == "mtime": return e["mtime"]
            return e["name"].lower()

        dirs = [e for e in self._entries if e["is_dir"]]
        files = [e for e in self._entries if not e["is_dir"]]
        dirs.sort(key=norm, reverse=rev)
        files.sort(key=norm, reverse=rev)
        self._entries = dirs + files

    def _sort_by(self, key):
        if self.sort_key == key:
            self.sort_dir = -self.sort_dir
        else:
            self.sort_key = key
            self.sort_dir = 1
        self._update_sort_indicators()
        self._apply_sort()
        self._load_token += 1
        token = self._load_token
        if self._render_job:
            try:
                self.frame.after_cancel(self._render_job)
            except Exception:
                pass
            self._render_job = None
        self._pending_render = list(self._entries)
        self._render_index = 0
        self.tree.delete(*self.tree.get_children())
        self._pump_render(token)

    # ---------- hover ----------
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

    # ---------- плавное колесо ----------
    def _on_wheel(self, event):
        if event.num == 4 or (hasattr(event, "delta") and event.delta > 0):
            self.tree.yview_scroll(-WHEEL_UNITS, "units")
        elif event.num == 5 or (hasattr(event, "delta") and event.delta < 0):
            self.tree.yview_scroll(WHEEL_UNITS, "units")
        return "break"

    # ---------- клики ----------
    def _on_click(self, event):
        self.explorer._set_active_panel(self)

    def _on_double(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if os.path.isdir(iid):
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
        for src in srcs:
            if not src or not os.path.exists(src):
                continue
            if os.path.normcase(os.path.abspath(src)).startswith(
                    os.path.normcase(self.current_dir)):
                continue
            self.explorer.copy_with_conflict(src, self.current_dir, move=False)
        self.refresh()

    def selected_paths(self):
        return list(self.tree.selection())

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

        self.icons = IconCache()
        self._delete_queue = []
        self._delete_report = {"deleted": 0, "skipped": 0, "errors": 0, "killed": 0}
        self._delete_refresh_cb = None
        self._conflict_apply_all = None
        self._busy_scan_running = False
        self._addr_debounce_id = None

        self.panels = []
        self.active_panel = None
        self.dual = tk.BooleanVar(value=settings.get("dual", False))
        self.hidden_var = tk.BooleanVar(value=self.show_hidden)

        self._build_ui()
        self._populate_drives()
        self._apply_bookmarks()
        self._restore_layout()

        last = settings.get("last_dir") or os.path.expanduser("~")
        self.navigate_to(last if os.path.isdir(last) else os.path.expanduser("~"))
        if self.dual.get():
            self._toggle_dual()

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
                self.list_container.sashpos(0, w // 2)
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
            self.active_panel = panel
            if panel.current_dir:
                self.address_var.set(panel.current_dir)
            self._update_title_active()

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
    def _populate_drives(self):
        expanded = self._collect_expanded()
        self.tree.delete(*self.tree.get_children())
        if os.name == "nt":
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            drives = [f"{letter}:\\"
                      for i, letter in enumerate(string.ascii_uppercase)
                      if bitmask & (1 << i)]
        else:
            drives = ["/"]
        for d in drives:
            icon = self.icons.for_file(d, is_dir=True, is_drive=True)
            self.tree.insert("", "end", iid=d, text=" " + d,
                             image=icon if icon else "")
            self.tree.insert(d, "end", text="Загрузка...")
        self._restore_expanded(expanded)

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
        for p in paths:
            if not os.path.isdir(p) or not self.tree.exists(p):
                continue
            ch = self.tree.get_children(p)
            if len(ch) == 1 and self.tree.item(ch[0], "text") == "Загрузка...":
                self.tree.delete(ch[0])
                self._fill_tree_node(p, p)
            try: self.tree.item(p, open=True)
            except tk.TclError: pass

    def on_tree_expand(self, event=None):
        item = self.tree.focus()
        if not item:
            return
        ch = self.tree.get_children(item)
        if len(ch) == 1 and self.tree.item(ch[0], "text") == "Загрузка...":
            self.tree.delete(ch[0])
            self._fill_tree_node(item, item)

    def _fill_tree_node(self, parent, path):
        try:
            entries = sorted(os.listdir(path), key=str.lower)
        except (PermissionError, OSError):
            return
        folder_icon = self.icons.for_file("", is_dir=True)
        for name in entries:
            full = os.path.join(path, name)
            if not os.path.isdir(full):
                continue
            hidden = self.is_hidden(full)
            if not self.show_hidden and hidden:
                continue
            tags = ("hidden",) if hidden else ()
            self.tree.insert(parent, "end", iid=full, text=" " + full,
                             image=folder_icon if folder_icon else "", tags=tags)
            self.tree.insert(full, "end", text="Загрузка...")

    def on_tree_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and os.path.isdir(iid):
            self.navigate_to(iid)

    # ---------- навигация ----------
    def navigate_to(self, path, add_history=True):
        if not os.path.isdir(path):
            self.status("Не папка: " + path); return
        if self.active_panel:
            self.active_panel.navigate_to(path, add_history)
            self.address_var.set(path)
            self._remember_address(path)
            self._update_title_active()
            self._start_busy_scan(path)
        self._hide_suggest()

    def go_back(self):
        if self.active_panel:
            self.active_panel.go_back()
            self.address_var.set(self.active_panel.current_dir)

    def go_forward(self):
        if self.active_panel:
            self.active_panel.go_forward()
            self.address_var.set(self.active_panel.current_dir)

    def go_up(self):
        if self.active_panel:
            self.active_panel.go_up()
            self.address_var.set(self.active_panel.current_dir)

    def refresh(self):
        for p in self.panels:
            p.refresh()
        if self.active_panel:
            self._start_busy_scan(self.active_panel.current_dir)

    def open_path_from_bar(self):
        path = self.address_var.get().strip().strip('"')
        self._hide_suggest()
        if os.path.isdir(path):
            self.navigate_to(path)
        elif os.path.isfile(path):
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
        if self._busy_scan_running or not path:
            return
        self._busy_scan_running = True
        BusyScanWorker(path, self._on_busy_scan).start()

    def _on_busy_scan(self, path, busy):
        def _apply():
            self._busy_scan_running = False
            for p in self.panels:
                if os.path.normcase(p.current_dir) == os.path.normcase(path):
                    p.apply_busy_tags(busy)
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
            self.active_panel.tree.selection_set(
                self.active_panel.tree.get_children())

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
        self._delete_paths(sel)

    def _delete_paths(self, paths, refresh_cb=None):
        self._delete_report = {"deleted": 0, "skipped": 0, "errors": 0, "killed": 0}
        self._delete_queue = list(paths)
        self._delete_refresh_cb = refresh_cb
        self._process_next_delete()

    def copy_selected(self, cut=False):
        p = self.active_panel
        if not p: return
        sel = p.selected_paths()
        if not sel: return
        self.clipboard = (list(sel), "cut" if cut else "copy")
        self.status(f"{'Вырезано' if cut else 'Скопировано'}: {len(sel)}")

    def paste_to_active(self):
        p = self.active_panel
        if not p or not self.clipboard: return
        srcs, mode = self.clipboard
        try:
            for src in srcs:
                self.copy_with_conflict(src, p.current_dir, move=(mode == "cut"))
            if mode == "cut":
                self.clipboard = None
            p.refresh()
        finally:
            self._conflict_apply_all = None

    def copy_with_conflict(self, src, dst_dir, move=False):
        if not os.path.exists(src):
            return
        name = os.path.basename(src)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst) and os.path.normcase(src) != os.path.normcase(dst):
            if self._conflict_apply_all is not None:
                action = self._conflict_apply_all
            else:
                dlg = ConflictDialog(self.root, src, dst)
                self.root.wait_window(dlg)
                if not dlg.result: return
                action, apply_all = dlg.result
                if apply_all:
                    self._conflict_apply_all = action
            if action == "skip":
                self._delete_report["skipped"] += 1; return
            if action == "rename":
                base, ext = os.path.splitext(name)
                i = 1
                while True:
                    new_name = f"{base} ({i}){ext}"
                    ndst = os.path.join(dst_dir, new_name)
                    if not os.path.exists(ndst):
                        dst = ndst; break
                    i += 1
            elif action == "replace":
                try:
                    if os.path.isdir(dst): shutil.rmtree(dst)
                    else: os.remove(dst)
                except OSError: pass
        try:
            if move: shutil.move(src, dst)
            else:
                if os.path.isdir(src): shutil.copytree(src, dst)
                else: shutil.copy2(src, dst)
            self._delete_report["deleted"] += 1
        except OSError as e:
            self._delete_report["errors"] += 1
            messagebox.showwarning("Ошибка", str(e), parent=self.root)

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
            os.mkdir(os.path.join(p.current_dir, name)); p.refresh()
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
        if os.path.exists(path):
            messagebox.showwarning("Ошибка", "Уже существует", parent=self.root); return
        try:
            with open(path, "x", encoding="utf-8"): pass
            p.refresh(); self.status("Создан: " + path)
        except OSError as e:
            messagebox.showwarning("Ошибка", str(e), parent=self.root)

    def _rename_item(self, path):
        old_name = os.path.basename(path)
        new_name = simpledialog.askstring("Переименовать", "Новое имя:",
                                          initialvalue=old_name, parent=self.root)
        if new_name and new_name != old_name:
            new_path = os.path.join(os.path.dirname(path), new_name)
            try:
                os.rename(path, new_path)
                if self.active_panel: self.active_panel.refresh()
            except OSError as e:
                messagebox.showwarning("Ошибка", str(e), parent=self.root)

    # ---------- удаление ----------
    def _process_next_delete(self):
        if not self._delete_queue:
            self._finish_delete_report(); return
        path = self._delete_queue.pop(0)
        if not os.path.exists(path):
            self._process_next_delete(); return
        try:
            self._plain_delete(path)
            self._delete_report["deleted"] += 1
            self.status("Удалено: " + path)
            self._process_next_delete(); return
        except PermissionError:
            pass
        except OSError as e:
            self._delete_report["errors"] += 1
            messagebox.showwarning("Ошибка удаления", f"{path}\n{e}", parent=self.root)
            self._process_next_delete(); return
        is_folder = os.path.isdir(path)
        self.status(f"Поиск процесса для {'папки' if is_folder else 'файла'} "
                    f"{os.path.basename(path)}...")
        def _done(p, res):
            self.root.after(0, self._on_processes_found, p, res)
        if is_folder:
            FolderProcessSearchWorker(path, _done).start()
        else:
            ProcessSearchWorker(path, _done).start()

    def _on_processes_found(self, path, result):
        exact      = result.get("exact", [])
        name_match = result.get("name_match", [])
        if exact:
            names = ", ".join(f"{p['name']}({p['pid']})" for p in exact)
            self.status("Завершаю: " + names); self.root.update_idletasks()
            self._delete_report["killed"] += self._kill_processes(exact)
            if self._retry_delete(path, silent=True):
                self._delete_report["deleted"] += 1
                self._process_next_delete(); return
            if name_match:
                self._ask_about_name_matches(path, name_match)
            else:
                self._delete_report["errors"] += 1
                messagebox.showwarning("Не удалось удалить",
                                       f"{path}\n\nПроцессы завершены, но удалить не удалось.",
                                       parent=self.root)
                self._process_next_delete()
            return
        if not name_match:
            self._delete_report["errors"] += 1
            messagebox.showwarning("Процесс не найден",
                                   f"Не удалось определить процесс,\nдержащий: {path}",
                                   parent=self.root)
            self._process_next_delete(); return
        self._ask_about_name_matches(path, name_match)

    def _ask_about_name_matches(self, path, procs):
        if not procs:
            self._process_next_delete(); return
        if len(procs) == 1:
            p = procs[0]
            reply = messagebox.askyesno(
                "Завершить процесс?",
                f"Объект:\n{path}\n\nИмя совпадает, но путь другой:\n"
                f"  {p['name']} (PID {p['pid']})\n  {p['exe']}\n\nЗавершить его?",
                parent=self.root)
            if not reply:
                self._delete_report["skipped"] += 1
                self._process_next_delete(); return
            self._delete_report["killed"] += self._kill_processes([p])
            if self._retry_delete(path): self._delete_report["deleted"] += 1
            else: self._delete_report["errors"] += 1
            self._process_next_delete(); return
        dlg = ProcessChooserDialog(self.root, procs)
        self.root.wait_window(dlg)
        chosen = dlg.result
        if not chosen:
            self._delete_report["skipped"] += 1
            self._process_next_delete(); return
        self._delete_report["killed"] += self._kill_processes(chosen)
        if self._retry_delete(path): self._delete_report["deleted"] += 1
        else: self._delete_report["errors"] += 1
        self._process_next_delete()

    def _finish_delete_report(self):
        for p in self.panels: p.refresh()
        if self._delete_refresh_cb:
            try: self._delete_refresh_cb()
            except Exception: pass
            self._delete_refresh_cb = None
        r = self._delete_report
        self.status(f"Удалено: {r['deleted']}, пропущено: {r['skipped']}, "
                    f"ошибок: {r['errors']}, завершено процессов: {r['killed']}")

    def _plain_delete(self, path):
        if os.path.isdir(path):
            def on_rm_error(func, p, exc_info):
                try:
                    os.chmod(p, stat.S_IWRITE); func(p)
                except OSError: raise
            shutil.rmtree(path, onerror=on_rm_error)
        else:
            try: os.chmod(path, stat.S_IWRITE)
            except OSError: pass
            os.remove(path)

    def _retry_delete(self, path, silent=False):
        try:
            time.sleep(0.4)
            self._plain_delete(path)
            self.status("Удалено: " + path)
            return True
        except OSError as e:
            if not silent:
                messagebox.showwarning("Не получается удалить",
                                       f"{path}\n\n{e}", parent=self.root)
            return False

    def _kill_processes(self, procs):
        n = 0
        for p in procs:
            try:
                proc = psutil.Process(p["pid"])
                proc.terminate()
                try: proc.wait(timeout=3)
                except psutil.TimeoutExpired: proc.kill()
                self.status(f"Завершён: {p['name']} (PID {p['pid']})"); n += 1
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                messagebox.showwarning("Ошибка",
                                       f"Не удалось завершить {p['name']} ({p['pid']}).",
                                       parent=self.root)
        return n

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
                os.chmod(path, stat.S_IWRITE)
                if os.path.isdir(path):
                    for root, dirs, files in os.walk(path):
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
            self.list_container.sashpos(0, w // 2)
        except tk.TclError:
            pass

    # ---------- сохранение ----------
    def _restore_layout(self):
        geom = self.settings.get("geometry")
        try: self.root.geometry(geom if geom else "1200x720")
        except tk.TclError: self.root.geometry("1200x720")

    def _on_close(self):
        try: self.settings["geometry"] = self.root.winfo_geometry()
        except tk.TclError: pass
        self.settings["show_hidden"] = self.show_hidden
        self.settings["dual"] = self.dual.get()
        self.settings["last_dir"] = self.active_panel.current_dir if self.active_panel else ""
        self.settings["address_history"] = self.address_history[-50:]
        self.settings["bookmarks"] = self.bookmarks
        self.settings["editor_cmd"] = self.editor_cmd
        save_settings(self.settings)
        self.root.destroy()


# ===========================================================================
# Запуск
# ===========================================================================
def main():
    root = TkBase()
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