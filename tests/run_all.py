"""Pixiv Archive 回归测试入口（无第三方依赖）。

用法（在项目根目录）：

    venv\\Scripts\\python.exe tests\\run_all.py              # 全部：离线 + 真实服务
    venv\\Scripts\\python.exe tests\\run_all.py --offline     # 只跑不需要服务的用例
    venv\\Scripts\\python.exe tests\\run_all.py --only envfile
    venv\\Scripts\\python.exe tests\\run_all.py --port 7802

退出码：全部通过为 0，有失败为 1。真实服务只在本机 127.0.0.1 上起，端口可换。
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: E402


def run_module(module, server):
    title = getattr(module, "TITLE", module.__name__)
    report = harness.Report(title)
    print("\n=== %s ===" % title, flush=True)
    try:
        module.run(report, server) if _wants_server(module) else module.run(report)
    except Exception as exc:  # 模块自身抛错也算失败，不能让整套测试静默中断
        import traceback

        report.check("模块执行未抛异常", False, "%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
    for status, name, detail in report.rows:
        print("  [%s] %s%s" % (status, name, ("   " + detail) if detail else ""), flush=True)
    return report


def _wants_server(module):
    import inspect

    return len(inspect.signature(module.run).parameters) >= 2


def skipped_module(module, reason):
    report = harness.Report(getattr(module, "TITLE", module.__name__))
    report.skip("跳过：" + reason)
    return report


def run_js_tests():
    """如果有 tests/js/*.test.js 且本机有 node，就跑一遍前端纯逻辑单测。"""
    js_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")
    if not os.path.isdir(js_dir):
        return []
    tests = sorted(f for f in os.listdir(js_dir) if f.endswith(".test.js"))
    if not tests:
        return []
    node = None
    for candidate in ("node", r"C:\Program Files\nodejs\node.exe"):
        try:
            subprocess.run([candidate, "--version"], capture_output=True, timeout=20, check=True)
            node = candidate
            break
        except Exception:
            continue
    reports = []
    for name in tests:
        report = harness.Report("JS 单元测试 %s" % name)
        print("\n=== %s ===" % report.title, flush=True)
        if node is None:
            report.skip("本机没有 node，跳过")
        else:
            proc = subprocess.run([node, os.path.join(js_dir, name)],
                                  capture_output=True, timeout=120)
            out = proc.stdout.decode("utf-8", "replace").strip()
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("[OK]"):
                    report.check(line[4:].strip(), True)
                elif line.startswith("[FAIL]"):
                    report.check(line[6:].strip(), False)
                elif line:
                    report.skip(line)
            if proc.returncode != 0 and not report.failed:
                report.check("node 退出码为 0", False,
                             proc.stderr.decode("utf-8", "replace")[-300:])
        for status, name_, detail in report.rows:
            print("  [%s] %s%s" % (status, name_, ("   " + detail) if detail else ""), flush=True)
        reports.append(report)
    return reports


def main():
    try:  # 让中文输出不受控制台代码页影响（管道里也是 UTF-8）
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Pixiv Archive 回归测试")
    parser.add_argument("--offline", action="store_true", help="跳过需要真实服务的用例")
    parser.add_argument("--only", default="", help="只跑文件名/标题包含该关键字的模块")
    parser.add_argument("--port", type=int, default=harness.DEFAULT_PORT)
    parser.add_argument("--no-js", action="store_true", help="跳过 node 前端单测")
    args = parser.parse_args()

    key = args.only.strip().lower()
    modules = []
    for module in harness.discover():
        blob = (os.path.basename(module.__file__) + " " + getattr(module, "TITLE", "")).lower()
        if key and key not in blob:
            continue
        modules.append(module)

    if not modules:
        print("没有匹配的测试模块：--only %r" % args.only)
        return 1

    reports = []
    for module in modules:
        if getattr(module, "NEEDS_SERVER", False):
            continue
        reports.append(run_module(module, None))

    server_modules = [m for m in modules if getattr(m, "NEEDS_SERVER", False)]
    if server_modules:
        if args.offline:
            for module in server_modules:
                reports.append(skipped_module(module, "--offline"))
        else:
            server = harness.Server(args.port)
            ok, message = server.start()
            print("\n" + message, flush=True)
            try:
                for module in server_modules:
                    if ok:
                        reports.append(run_module(module, server))
                    else:
                        reports.append(skipped_module(module, message))
            finally:
                server.stop()

    if not args.no_js and not key:
        reports.extend(run_js_tests())

    total = sum(r.total for r in reports)
    passed = sum(r.passed for r in reports)
    failed = [name for r in reports for name in r.failed]
    skipped = sum(1 for r in reports for row in r.rows if row[0] == "SKIP")

    print("\n" + "=" * 66)
    for report in reports:
        flag = "通过" if not report.failed else "失败 %d" % len(report.failed)
        print("  %-52s %2d 项  %s" % (report.title[:52], report.total, flag))
    print("=" * 66)
    print("合计：%d 项检查，通过 %d，失败 %d，跳过 %d" % (total, passed, len(failed), skipped))
    for name in failed:
        print("  失败：%s" % name)
    print("结论：%s" % ("全部通过" if not failed else "存在失败项"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())