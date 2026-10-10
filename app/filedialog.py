"""本机原生「文件 / 文件夹选择」对话框（Windows 资源管理器样式）。

用 ctypes 直接调用系统 API：
- 文件夹：`shell32.SHBrowseForFolderW`
- 文件：`comdlg32.GetOpenFileNameW`

刻意不依赖 tkinter（本项目 venv 里没有）、也不起 PowerShell / 子进程：纯标准库、
无额外进程、不弹控制台窗口，打包成 exe 后同样可用。

对话框是**模态**的：调用线程会一直阻塞到用户选完或取消，因此调用方必须放到线程池里
执行（见 main.py 的 `run_in_threadpool`），**不能**直接在事件循环线程上调用。

非 Windows 平台一律抛 `FileDialogError`，由接口层转成结构化错误、前端退化为手动输入。
"""
import ctypes
import os
import sys
import threading

IS_WINDOWS = sys.platform.startswith("win")

# 同一时刻只允许一个对话框：多开会让用户困惑，也容易把线程池线程占满
_DIALOG_LOCK = threading.Lock()

# SHBrowseForFolderW 的 ulFlags
_BIF_RETURNONLYFSDIRS = 0x0001
_BIF_EDITBOX = 0x0010
_BIF_NEWDIALOGSTYLE = 0x0040
_BIF_USENEWUI = _BIF_EDITBOX | _BIF_NEWDIALOGSTYLE

# GetOpenFileNameW 的 Flags
_OFN_NOCHANGEDIR = 0x00000008
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_EXPLORER = 0x00080000

_COINIT_APARTMENTTHREADED = 0x2
# RPC_E_CHANGED_MODE：该线程已按别的套间初始化过，忽略即可（不影响对话框）
_RPC_E_CHANGED_MODE = -2147417850

# 文件选择对话框的默认过滤器
DEFAULT_FILE_FILTERS = (("数据包 (*.zip)", "*.zip"), ("所有文件 (*.*)", "*.*"))


class FileDialogError(Exception):
    """带结构化信息的对话框错误，供接口层转成 {code, message, hint}。"""

    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def is_supported():
    return IS_WINDOWS


if IS_WINDOWS:
    from ctypes import wintypes

    class _BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    class _OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    def _co_initialize():
        """把当前线程初始化为 STA 套间。

        `BIF_NEWDIALOGSTYLE` 要求调用方先 CoInitialize；线程池线程会被复用，因此这里
        只初始化、不 CoUninitialize，避免每次弹窗都重复初始化/反初始化。
        """
        try:
            ole32 = ctypes.WinDLL("ole32", use_last_error=True)
            ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            ole32.CoInitializeEx.restype = ctypes.c_long
            hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            if hr not in (0, 1) and hr != _RPC_E_CHANGED_MODE:
                return
        except Exception:
            # 初始化失败也让对话框照常尝试：多数情况下仍能工作
            pass

    def _pidl_from_path(shell32, path):
        """把本地路径转成 PIDL（供文件夹对话框定位初始目录）；失败返回 None。"""
        if not path or not os.path.isdir(path):
            return None
        try:
            shell32.SHParseDisplayName.argtypes = [
                wintypes.LPCWSTR, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p), wintypes.ULONG,
                ctypes.POINTER(wintypes.ULONG),
            ]
            shell32.SHParseDisplayName.restype = ctypes.c_long
            pidl = ctypes.c_void_p()
            attrs = wintypes.ULONG()
            hr = shell32.SHParseDisplayName(
                path, None, ctypes.byref(pidl), 0, ctypes.byref(attrs))
            if hr != 0 or not pidl.value:
                return None
            return pidl
        except Exception:
            return None

    def _pick_folder_windows(initial_dir, title):
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)

        shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(_BROWSEINFOW)]
        shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None

        root_pidl = _pidl_from_path(shell32, initial_dir)
        display = ctypes.create_unicode_buffer(260)
        info = _BROWSEINFOW()
        info.hwndOwner = None
        info.pidlRoot = root_pidl
        # LPWSTR 字段需要显式 cast：直接赋 c_wchar_Array 会抛 TypeError
        info.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
        info.lpszTitle = title
        info.ulFlags = _BIF_RETURNONLYFSDIRS | _BIF_USENEWUI
        info.lpfn = None
        info.lParam = 0
        info.iImage = 0

        try:
            pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
            if not pidl:
                return None  # 用户取消
            try:
                path_buf = ctypes.create_unicode_buffer(1024)
                if not shell32.SHGetPathFromIDListW(pidl, path_buf):
                    raise FileDialogError("PICKER_FAILED", "无法解析所选文件夹的路径",
                                          "请重试或手动填写路径")
                return path_buf.value
            finally:
                ole32.CoTaskMemFree(pidl)
        finally:
            if root_pidl:
                ole32.CoTaskMemFree(root_pidl)

    def _pick_file_windows(initial_dir, title, filters, default_ext):
        comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(_OPENFILENAMEW)]
        comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
        comdlg32.CommDlgExtendedError.restype = wintypes.DWORD

        # 过滤器是「显示名\0通配符\0…\0\0」的双 NUL 结尾字符串
        parts = []
        for label, pattern in filters:
            parts.append(label)
            parts.append(pattern)
        filter_str = "\0".join(parts) + "\0\0"

        buffer = ctypes.create_unicode_buffer(4096)
        ofn = _OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
        ofn.hwndOwner = None
        ofn.hInstance = None
        ofn.lpstrFilter = filter_str
        ofn.lpstrCustomFilter = None
        ofn.nMaxCustFilter = 0
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        ofn.nMaxFile = len(buffer)
        ofn.lpstrFileTitle = None
        ofn.nMaxFileTitle = 0
        ofn.lpstrInitialDir = initial_dir if (initial_dir and os.path.isdir(initial_dir)) else None
        ofn.lpstrTitle = title
        ofn.Flags = _OFN_EXPLORER | _OFN_FILEMUSTEXIST | _OFN_PATHMUSTEXIST | _OFN_NOCHANGEDIR
        ofn.nFileOffset = 0
        ofn.nFileExtension = 0
        ofn.lpstrDefExt = default_ext
        ofn.lCustData = 0
        ofn.lpfnHook = None
        ofn.lpTemplateName = None
        ofn.pvReserved = None
        ofn.dwReserved = 0
        ofn.FlagsEx = 0

        if not comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            # 返回 FALSE 时用 CommDlgExtendedError 区分「用户取消」(0) 与真实错误
            ext_error = comdlg32.CommDlgExtendedError()
            if ext_error:
                raise FileDialogError("PICKER_FAILED", f"打开文件选择窗口失败（错误码 {ext_error}）",
                                      "请重试或手动填写路径")
            return None
        return buffer.value


def pick_folder(initial_dir=None, title=None):
    """弹出文件夹选择对话框；返回所选路径，用户取消返回 None。"""
    if not IS_WINDOWS:
        raise FileDialogError("UNSUPPORTED", "当前系统不支持原生文件夹选择",
                              "请手动填写路径")
    with _DIALOG_LOCK:
        _co_initialize()
        return _pick_folder_windows(initial_dir, title or "选择文件夹")


def pick_file(initial_dir=None, title=None, filters=None, default_ext=None):
    """弹出文件选择对话框；返回所选文件路径，用户取消返回 None。"""
    if not IS_WINDOWS:
        raise FileDialogError("UNSUPPORTED", "当前系统不支持原生文件选择",
                              "请手动填写路径")
    with _DIALOG_LOCK:
        _co_initialize()
        return _pick_file_windows(initial_dir, title or "选择文件",
                                  filters or DEFAULT_FILE_FILTERS, default_ext)
