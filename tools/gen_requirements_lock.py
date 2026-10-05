"""生成 requirements.lock（含哈希）——需联网。

对 Windows + CPython 3.10~3.13 各解析一次 requirements.txt，把各版本轮子的
sha256 合并成一份锁文件；这样同一份锁在不同 Python 版本下都能通过
`pip install --require-hashes` 的校验。

用法（项目根目录）：

    venv\\Scripts\\python.exe tools\\gen_requirements_lock.py

仅维护依赖时才需要运行；日常安装直接跑 install.bat。
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ = os.path.join(ROOT, "requirements.txt")
OUT = os.path.join(ROOT, "requirements.lock")
PYTHON = sys.executable
TARGET_VERSIONS = ("3.10", "3.11", "3.12", "3.13")

HEADER = """# 依赖锁定文件（含哈希）——可复现、可校验的安装。
#
# 用法：
#   venv\\Scripts\\python.exe -m pip install -r requirements.lock --require-hashes
#
# 本文件锁定 requirements.txt 解析出的完整依赖闭包（含间接依赖）。为让同一份锁
# 在 Windows + CPython 3.10~3.13 下都能通过 --require-hashes 校验，每个包列出
# 上述各 Python 版本对应的 win_amd64 轮子哈希（纯 Python 包则只有一个通用轮子）。
#
# 重新生成：venv\\Scripts\\python.exe tools\\gen_requirements_lock.py
"""


def _current_version():
    return "%d.%d" % sys.version_info[:2]


def _report_for(version, path):
    cmd = [PYTHON, "-m", "pip", "install", "--dry-run", "--ignore-installed",
           "--quiet", "--report", path]
    if version != _current_version():
        # 跨版本解析需要显式指定目标解释器与平台
        cmd += ["--only-binary=:all:", "--python-version", version,
                "--platform", "win_amd64"]
    cmd += ["-r", REQ]
    subprocess.run(cmd, check=True, cwd=ROOT)


def main():
    merged = {}
    with tempfile.TemporaryDirectory(prefix="pa-lock-") as tmp:
        for version in TARGET_VERSIONS:
            print("resolving for CPython %s ..." % version, flush=True)
            path = os.path.join(tmp, "report_%s.json" % version)
            _report_for(version, path)
            report = json.load(open(path, encoding="utf-8"))
            for entry in report["install"]:
                meta = entry["metadata"]
                key = meta["name"].lower().replace("_", "-")
                info = merged.setdefault(
                    key, {"name": meta["name"], "version": meta["version"], "hashes": set()})
                assert info["version"] == meta["version"], \
                    "版本在 Python %s 下不一致：%s" % (version, key)
                digest = ((entry.get("download_info") or {}).get("archive_info") or {}).get("hash", "")
                if digest.startswith("sha256="):
                    info["hashes"].add(digest.split("=", 1)[1])

    lines = [HEADER]
    for key in sorted(merged):
        info = merged[key]
        hashes = sorted(info["hashes"])
        if not hashes:
            continue
        lines.append("%s==%s \\" % (info["name"], info["version"]))
        for i, digest in enumerate(hashes):
            sep = "" if i == len(hashes) - 1 else " \\"
            lines.append("    --hash=sha256:%s%s" % (digest, sep))
        lines.append("")

    with open(OUT, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    print("wrote %s (%d packages)" % (os.path.relpath(OUT, ROOT), len(merged)))


if __name__ == "__main__":
    main()
