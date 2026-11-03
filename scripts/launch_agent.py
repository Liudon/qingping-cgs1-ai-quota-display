#!/usr/bin/env python3
"""所有配置与日志保留在项目内，LaunchAgents 目录只建立入口软链接。"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LABEL = "local.ai-quota-display"
DOMAIN = "gui/" + str(os.getuid())
TARGET = DOMAIN + "/" + LABEL
PLIST = ROOT / "runtime" / (LABEL + ".plist")
LINK = Path.home() / "Library/LaunchAgents" / PLIST.name


def run(*args, check=True):
    return subprocess.run(["launchctl", *args], check=check)


def stop_and_wait():
    run("bootout", TARGET, check=False)
    # bootout 返回时任务可能仍在退出；等待注销，避免紧接 bootstrap 报错 5。
    deadline = time.monotonic() + 60
    while subprocess.run(["launchctl", "print", TARGET], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL).returncode == 0:
        if time.monotonic() >= deadline:
            raise SystemExit("服务仍在停止中，请稍后重试")
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description="额度服务后台管理")
    parser.add_argument("action", choices=["install", "start", "stop", "status", "uninstall"])
    args = parser.parse_args()
    if args.action == "status":
        return run("print", TARGET, check=False).returncode
    if args.action in ("stop", "uninstall"):
        stop_and_wait()
        if args.action == "uninstall" and LINK.is_symlink() and LINK.resolve() == PLIST:
            LINK.unlink()
        return 0
    if args.action == "install":
        if LINK.exists() or LINK.is_symlink():
            if not LINK.is_symlink() or LINK.resolve() != PLIST:
                raise SystemExit("已有同名 LaunchAgent，拒绝覆盖：" + str(LINK))
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        config = ROOT / "config.json"
        if not config.exists():
            config.write_text((ROOT / "config.example.json").read_text())
        json.loads(config.read_text())
        data = dict(Label=LABEL, ProgramArguments=[sys.executable, "-m", "service.main", "--config", str(config)],
                    WorkingDirectory=str(ROOT), RunAtLoad=True, KeepAlive=True, ThrottleInterval=30,
                    ExitTimeOut=65,
                    StandardOutPath=str(ROOT / "runtime/launchd.stdout.log"),
                    StandardErrorPath=str(ROOT / "runtime/launchd.stderr.log"),
                    EnvironmentVariables={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"})
        PLIST.write_bytes(plistlib.dumps(data))
        LINK.parent.mkdir(parents=True, exist_ok=True)
        if not LINK.is_symlink():
            LINK.symlink_to(PLIST)
        stop_and_wait()
    if not PLIST.exists():
        raise SystemExit("请先运行 install")
    run("enable", TARGET)
    run("bootstrap", DOMAIN, str(LINK))
    print("服务已启动：http://127.0.0.1:8766 （端口以 config.json 为准）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
