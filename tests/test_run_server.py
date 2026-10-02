"""run.py 启动链路：hypercorn 配置、端口占用提示、启动失败返回非零码。

不起真实服务；真实双实例与端到端行为见 `test_http_smoke.py`。
"""
import asyncio
import contextlib
import io
import socket

import harness as h

h.bootstrap()
import run as run_mod  # noqa: E402

TITLE = "启动链路：hypercorn 配置 / 端口占用 / 失败退码"
NEEDS_SERVER = False


def run(t):
    config = run_mod.build_config("127.0.0.1", 7901)
    t.equal("配置绑定到指定地址与端口", config.bind, ["127.0.0.1:7901"])
    t.check("只监听 127.0.0.1（不开放局域网）",
            all(bind.startswith("127.0.0.1:") for bind in config.bind))
    t.check("请求日志默认关闭（控制台不被刷屏）", config.accesslog is None)
    t.equal("访问地址拼接正确", run_mod.server_url("127.0.0.1", 7901), "http://127.0.0.1:7901/")
    t.check("run.py 已改用 hypercorn",
            "hypercorn" in open(run_mod.__file__, encoding="utf-8").read())

    # 端口被占用：绑定阶段必须失败，而不是静默装死
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]

    def attempt():
        async def inner():
            try:
                await asyncio.wait_for(run_mod._serve("127.0.0.1", busy_port), timeout=30)
            except OSError as exc:
                return exc
            except asyncio.TimeoutError:
                return "TIMEOUT"
            return None

        return asyncio.run(inner())

    with contextlib.redirect_stdout(io.StringIO()):
        outcome = attempt()
    sock.close()
    t.check("端口被占用时启动失败（抛 OSError）", isinstance(outcome, OSError), repr(outcome))

    # 启动失败要给可读提示并返回非零码（而不是抛一串 traceback）
    original = (run_mod._serve, run_mod.acquire_single_instance, run_mod.release_single_instance)

    async def boom(*_args, **_kwargs):
        raise OSError("模拟端口占用")

    run_mod._serve = boom
    run_mod.acquire_single_instance = lambda: "test-lock"
    run_mod.release_single_instance = lambda _lock: None
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = run_mod.main()
    finally:
        run_mod._serve, run_mod.acquire_single_instance, run_mod.release_single_instance = original
    t.equal("启动失败返回非零码", code, 1)
    t.check("启动失败打印可读提示",
            "启动失败" in buffer.getvalue() and "端口" in buffer.getvalue(),
            buffer.getvalue().strip()[:100])

    # 已有实例时返回非零码且不启动
    run_mod.acquire_single_instance = lambda: None
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = run_mod.main()
    finally:
        run_mod.acquire_single_instance = original[1]
    t.equal("已有实例时返回非零码", code, 1)
    t.check("已有实例时提示不要重复启动", "已在运行中" in buffer.getvalue())