"""原生「文件 / 文件夹选择」：接口层三条返回路径 + 平台与结构体自检。

对话框本身需要交互式桌面，测试里**不真正弹窗**（会一直阻塞），改为把 filedialog 的
弹窗函数打桩，验证接口层的 成功 / 取消 / 不可用 三种返回与前端约定一致。
"""
import asyncio
import ctypes
import json
import sys

import harness as h

h.bootstrap()
from app import filedialog  # noqa: E402
from app import main as app_main  # noqa: E402

TITLE = "原生文件/文件夹选择（接口层 + 平台自检）"
NEEDS_SERVER = False


def _post(body):
    """构造一个带 JSON 请求体的 POST Request。"""
    from starlette.requests import Request

    payload = json.dumps(body).encode("utf-8")
    state = {"sent": False}

    async def receive():
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")],
        "path": "/",
        "query_string": b"",
        "scheme": "http",
    }
    return Request(scope, receive)


def _call(fn, body):
    return asyncio.run(fn(_post(body)))


def _json(resp):
    """成功时端点返回普通 dict，错误时返回 JSONResponse——两种都归一成 dict。"""
    if isinstance(resp, dict):
        return resp
    return json.loads(resp.body.decode("utf-8"))


class _Patched:
    """临时替换 filedialog 里的弹窗函数。"""

    def __init__(self, **stubs):
        self._stubs = stubs
        self._saved = {}

    def __enter__(self):
        for name, fn in self._stubs.items():
            self._saved[name] = getattr(filedialog, name)
            setattr(filedialog, name, fn)
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(filedialog, name, fn)
        return False


def _platform(t):
    t.equal("is_supported 与平台一致", filedialog.is_supported(),
            sys.platform.startswith("win"))
    t.equal("默认过滤器首个是 zip", filedialog.DEFAULT_FILE_FILTERS[0][1], "*.zip")

    if sys.platform.startswith("win") and ctypes.sizeof(ctypes.c_void_p) == 8:
        # 结构体大小必须与 Windows x64 ABI 完全一致，否则系统调用会读错字段
        t.equal("OPENFILENAMEW 结构体大小（x64）",
                ctypes.sizeof(filedialog._OPENFILENAMEW), 152)
        t.equal("BROWSEINFOW 结构体大小（x64）",
                ctypes.sizeof(filedialog._BROWSEINFOW), 64)

    # 非 Windows 平台应抛结构化错误，而不是崩溃
    saved = filedialog.IS_WINDOWS
    filedialog.IS_WINDOWS = False
    try:
        raised = None
        try:
            filedialog.pick_folder()
        except filedialog.FileDialogError as exc:
            raised = exc.code
        t.equal("非 Windows 平台抛 UNSUPPORTED", raised, "UNSUPPORTED")
    finally:
        filedialog.IS_WINDOWS = saved


def _api(t):
    # 1) 用户选中了路径
    with _Patched(pick_folder=lambda *a, **k: r"D:\export\here"):
        resp = _call(app_main.api_pick_folder, {"initial_dir": ""})
    t.equal("pick-folder 返回选中路径", _json(resp).get("path"), r"D:\export\here")

    # 2) 用户取消
    with _Patched(pick_folder=lambda *a, **k: None):
        resp = _call(app_main.api_pick_folder, {"initial_dir": ""})
    t.equal("pick-folder 取消返回 cancelled", _json(resp).get("cancelled"), True)

    # 3) 对话框不可用 → 结构化错误（前端据此退化为手动输入）
    def _boom(*a, **k):
        raise filedialog.FileDialogError("UNSUPPORTED", "当前系统不支持原生选择",
                                         "请手动填写路径")

    with _Patched(pick_folder=_boom):
        resp = _call(app_main.api_pick_folder, {"initial_dir": ""})
    body = _json(resp)
    t.equal("pick-folder 不可用返回结构化错误码", body["error"]["code"], "UNSUPPORTED")
    t.equal("pick-folder 不可用返回 400", resp.status_code, 400)
    t.check("pick-folder 错误带修复提示", bool(body["error"].get("hint")))

    # 文件选择：选中 / 取消
    with _Patched(pick_file=lambda *a, **k: r"D:\pkg\data.zip"):
        resp = _call(app_main.api_pick_file, {"initial_dir": ""})
    t.equal("pick-file 返回选中路径", _json(resp).get("path"), r"D:\pkg\data.zip")

    with _Patched(pick_file=lambda *a, **k: None):
        resp = _call(app_main.api_pick_file, {"initial_dir": ""})
    t.equal("pick-file 取消返回 cancelled", _json(resp).get("cancelled"), True)

    # 传入的初始目录应被透传给弹窗函数（用于「在当前位置打开」）
    seen = {}

    def _record(initial_dir=None, title=None):
        seen["initial_dir"] = initial_dir
        return None

    with _Patched(pick_folder=_record):
        _call(app_main.api_pick_folder, {"initial_dir": r"D:\already\there"})
    t.equal("初始目录透传给文件夹弹窗", seen.get("initial_dir"), r"D:\already\there")


def run(t):
    _platform(t)
    _api(t)
