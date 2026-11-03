#!/usr/bin/env python3
"""安装/回滚外置 QML；不修改 SnowApp 二进制，不停止看门狗。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CA_CERT = ROOT / "certs" / "ISRG-Root-X1.pem"
CA_CERT_SHA256 = "22b557a27055b33606b6559f37703928d3e4ad79f110b407d04986e1843543d1"
WATCHDOG_SNIPPET = ROOT / "scripts" / "watchdog-ca.sh"
BASE = "/data/custom"
DEVICE_ROOT = BASE + "/ai-quota-display"
STAGING = DEVICE_ROOT + "/.staging"
BACKUP = DEVICE_ROOT + "/CustomView.qml.orig"
VIEW = BASE + "/CustomView.qml"
PATCHED_SHA = "7300addb7fda4714b93cc45d3eb1df00b6940bdd5265fff119c7cf12933746e6"
DEVICE_CA = DEVICE_ROOT + "/ISRG-Root-X1.pem"
QT_CA_DIR = DEVICE_ROOT + "/qt-certs"
QT_CA = QT_CA_DIR + "/ISRG-Root-X1.pem"
WATCHDOG = "/data/bin/watchdog.sh"
WATCHDOG_BACKUP = DEVICE_ROOT + "/watchdog.sh.orig"
WATCHDOG_BACKUP_SHA = WATCHDOG_BACKUP + ".sha256"


def validate_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query or parsed.path not in ("/api/v1/quotas", "/quota.json"):
        raise ValueError("URL 应为 http(s)://主机/api/v1/quotas 或 /quota.json，不含凭据或查询参数")
    if parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise ValueError("设备需要 Mac 的局域网地址，不能使用本机回环地址")
    return value


def configured_refresh_seconds():
    try:
        config = json.loads((ROOT / "config.json").read_text())
        value = config.get("device_refresh_seconds", 300)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("无法读取 config.json 中的设备刷新配置") from exc
    if type(value) is not int or not 60 <= value <= 86400:
        raise SystemExit("device_refresh_seconds 必须是 60 到 86400 之间的整数")
    return value


class Device:
    def __init__(self, serial=None):
        self.prefix = ["adb"]
        if not serial:
            lines = subprocess.check_output(["adb", "devices"], text=True).splitlines()[1:]
            devices = [line.split()[0] for line in lines if len(line.split()) == 2 and line.split()[1] == "device"]
            if len(devices) != 1:
                raise SystemExit("需要恰好一台已授权设备，或指定 --serial")
            serial = devices[0]
        self.prefix += ["-s", serial]

    def shell(self, command):
        return subprocess.check_output(self.prefix + ["shell", command], text=True, timeout=20).strip()

    def sha(self, path):
        return self.shell("sha256sum " + path).split()[0]

    def push(self, local, remote):
        data = Path(local).read_bytes()
        expected = hashlib.sha256(data).hexdigest()
        parent = remote.rsplit("/", 1)[0]
        self.shell("mkdir -p " + parent)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                upload = Path(tmp) / Path(remote).name
                upload.write_bytes(data)
                subprocess.run(self.prefix + ["push", str(upload), parent + "/"], check=True, timeout=30)
        except subprocess.CalledProcessError as exc:
            raise SystemExit("adb push 失败；当前页面未切换，请重新连接后重试") from exc
        if self.sha(remote) != expected:
            raise SystemExit("上传校验失败：" + remote)

    def install_ca_support(self):
        certificate = CA_CERT.read_bytes()
        if hashlib.sha256(certificate).hexdigest() != CA_CERT_SHA256:
            raise SystemExit("仓库中的 ISRG Root X1 文件指纹不匹配，停止安装")

        self.shell("mkdir -p " + DEVICE_ROOT + " " + QT_CA_DIR)
        for remote in (DEVICE_CA, QT_CA):
            exists = self.shell("test -f " + remote + " && echo yes || echo no") == "yes"
            if not exists or self.sha(remote) != CA_CERT_SHA256:
                self.push(CA_CERT, remote)
        if self.sha(DEVICE_CA) != CA_CERT_SHA256 or self.sha(QT_CA) != CA_CERT_SHA256:
            raise SystemExit("设备 CA 文件校验失败，停止安装")

        with tempfile.TemporaryDirectory() as tmp:
            watchdog_file = Path(tmp) / "watchdog.sh"
            subprocess.run(self.prefix + ["pull", WATCHDOG, str(watchdog_file)], check=True, timeout=30)
            original_text = watchdog_file.read_text()
            backup_exists = self.shell("test -f " + WATCHDOG_BACKUP + " && echo yes || echo no") == "yes"
            snippet = WATCHDOG_SNIPPET.read_text().rstrip("\n")
            snippet_lines = snippet.splitlines()
            already_patched = all(line in original_text.splitlines() for line in snippet_lines)

            if not backup_exists:
                if already_patched:
                    raise SystemExit("看门狗已被修改但缺少原始备份；拒绝覆盖或伪造备份")
                backup_sha = hashlib.sha256(watchdog_file.read_bytes()).hexdigest()
                self.push(watchdog_file, WATCHDOG_BACKUP + ".new")
                self.shell("mv -f " + WATCHDOG_BACKUP + ".new " + WATCHDOG_BACKUP + " && echo " + backup_sha + " > " + WATCHDOG_BACKUP_SHA + " && sync")
            else:
                backup_sha = self.sha(WATCHDOG_BACKUP)
                has_backup_sha = self.shell("test -f " + WATCHDOG_BACKUP_SHA + " && echo yes || echo no") == "yes"
                if has_backup_sha and self.shell("cat " + WATCHDOG_BACKUP_SHA).strip() != backup_sha:
                    raise SystemExit("看门狗原始备份校验失败，拒绝修改")
                if not has_backup_sha:
                    subprocess.run(self.prefix + ["shell", "echo " + backup_sha + " > " + WATCHDOG_BACKUP_SHA], check=True, timeout=20)
                with tempfile.TemporaryDirectory() as backup_tmp:
                    saved = Path(backup_tmp) / "watchdog.sh.orig"
                    subprocess.run(self.prefix + ["pull", WATCHDOG_BACKUP, str(saved)], check=True, timeout=30)
                    saved_text = saved.read_text()
                    if any(line in saved_text.splitlines() for line in snippet_lines):
                        raise SystemExit("看门狗备份已包含 CA 补丁，拒绝将其视为原始版本")

            if not already_patched:
                anchor = "sleepTime=5\n"
                if anchor not in original_text:
                    raise SystemExit("看门狗脚本结构未知，未修改")
                missing_lines = [line for line in snippet_lines if line not in original_text.splitlines()]
                updated = original_text.replace(anchor, anchor + "\n".join(missing_lines) + "\n", 1)
                if all(line in updated.splitlines() for line in snippet_lines):
                    current_sha = self.sha(WATCHDOG)
                    with tempfile.TemporaryDirectory() as patched_tmp:
                        patched = Path(patched_tmp) / "watchdog.sh.new"
                        patched.write_text(updated)
                        self.push(patched, WATCHDOG + ".new")
                    if self.sha(WATCHDOG) != current_sha:
                        self.shell("rm -f " + WATCHDOG + ".new")
                        raise SystemExit("看门狗文件在操作中发生变化，拒绝覆盖")
                    self.shell("chmod 755 " + WATCHDOG + ".new && mv -f " + WATCHDOG + ".new " + WATCHDOG + " && sync")

        mount_info = self.shell("grep ' /etc/ssl/certs ' /proc/self/mountinfo || true")
        if not mount_info:
            self.shell("mount -o bind " + QT_CA_DIR + " /etc/ssl/certs")
            mount_info = self.shell("grep ' /etc/ssl/certs ' /proc/self/mountinfo || true")
        mount_root = "/custom/ai-quota-display/qt-certs /etc/ssl/certs "
        if mount_root not in mount_info:
            raise SystemExit("/etc/ssl/certs 已挂载到其他位置，停止自动修改")
        if self.sha("/etc/ssl/certs/ISRG-Root-X1.pem") != CA_CERT_SHA256:
            raise SystemExit("Qt 系统 CA 目录未读取到 ISRG Root X1")

    def ready(self):
        if self.sha("/data/bin/SnowApp") != PATCHED_SHA:
            raise SystemExit("SnowApp 不是已验证的自定义页版本，停止安装；请先核实页面入口")
        self.shell("test -f " + VIEW + " && ps | grep -q '[w]atchdog.sh'")

    def restart(self):
        self.shell("ps | grep -q '[w]atchdog.sh' && pidof SnowApp >/dev/null && kill -9 $(pidof SnowApp)")


def main():
    parser = argparse.ArgumentParser(description="青萍额度页面部署与回滚（固定目录和空间上限）")
    parser.add_argument("action", choices=["install", "restore"])
    parser.add_argument("--url")
    parser.add_argument("--serial")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()
    if args.action == "install":
        try:
            url = validate_url(args.url or "")
        except ValueError as exc:
            parser.error(str(exc))
    device = Device(args.serial)
    device.ready()
    current_sha = device.sha(VIEW)
    if args.action == "restore":
        backup_sha = device.sha(BACKUP + ".sha256")
        if device.sha(BACKUP) != backup_sha:
            raise SystemExit("固定备份校验失败，拒绝覆盖")
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "CustomView.qml"
            subprocess.run(device.prefix + ["pull", BACKUP, str(original)], check=True, timeout=30)
            if hashlib.sha256(original.read_bytes()).hexdigest() != backup_sha:
                raise SystemExit("备份下载校验失败")
            device.push(original, VIEW + ".new")
            device.shell("mv -f " + VIEW + ".new " + VIEW + " && sync")
        print("已恢复首次安装前的页面（原始备份保留）")
    else:
        refresh_seconds = configured_refresh_seconds()
        device.install_ca_support()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            settings = tmp / "Settings.js"
            settings.write_text(".pragma library\nvar serverUrl = " + json.dumps(url) + "\nvar refreshSeconds = " + str(refresh_seconds) + "\n")

            # 识别当前槽位，始终写入非活动槽位，容量有固定上限。
            active = device.shell("if grep -Fq '" + DEVICE_ROOT + "/slot-a/CustomView.qml' " + VIEW + "; then echo a; elif grep -Fq '" + DEVICE_ROOT + "/slot-b/CustomView.qml' " + VIEW + "; then echo b; else echo none; fi")
            slot = "slot-b" if active == "a" else "slot-a"
            target = DEVICE_ROOT + "/" + slot
            device.shell("mkdir -p " + DEVICE_ROOT + " && rm -rf " + STAGING + " && mkdir -p " + STAGING)
            try:
                # 保存首次安装前页面，不覆盖已有备份。
                if device.shell("test -f " + BACKUP + " && echo yes || echo no") == "no":
                    with tempfile.TemporaryDirectory() as backup_tmp:
                        original = Path(backup_tmp) / "CustomView.qml"
                        subprocess.run(device.prefix + ["pull", VIEW, str(original)], check=True, timeout=30)
                        if hashlib.sha256(original.read_bytes()).hexdigest() != current_sha:
                            raise SystemExit("备份源文件校验失败")
                        device.push(original, BACKUP + ".new")
                        sha_file = Path(backup_tmp) / "CustomView.qml.sha256"
                        sha_file.write_text(current_sha + "\n")
                        device.push(sha_file, BACKUP + ".sha256.new")
                        device.shell("mv -f " + BACKUP + ".new " + BACKUP + " && mv -f " + BACKUP + ".sha256.new " + BACKUP + ".sha256 && sync")

                loader = tmp / "CustomView.qml"
                loader.write_text('import QtQuick 2.9\nLoader {\n    source: "file://' + target + '/CustomView.qml"\n    function restore() { if (item && item.restore) item.restore() }\n}\n')
                for name, local in (("CustomView.qml", STAGING + "/CustomView.qml"),
                                    ("Quota.js", STAGING + "/Quota.js"),
                                    ("Settings.js", STAGING + "/Settings.js")):
                    device.push(ROOT / "qml" / name if name != "Settings.js" else settings, local)
                device.shell("test -s " + STAGING + "/CustomView.qml && test -s " + STAGING + "/Quota.js && test -s " + STAGING + "/Settings.js && rm -rf " + target + " && mv " + STAGING + " " + target)
                device.push(loader, VIEW + ".new")
                if device.sha(VIEW) != current_sha:
                    raise SystemExit("当前页面在部署期间被修改，拒绝覆盖")
                device.shell("mv -f " + VIEW + ".new " + VIEW + " && sync")
            finally:
                try:
                    device.shell("rm -rf " + STAGING + " " + VIEW + ".new")
                except (OSError, subprocess.SubprocessError):
                    pass
        print("页面已安装到固定目录 " + target)
        print("已校验 ISRG Root X1，并配置 Qt CA 目录与看门狗启动项")
        print("设备额度刷新间隔：" + str(refresh_seconds) + " 秒")
        print("原始页面备份：" + BACKUP + "；回滚命令：python3 scripts/device.py restore")
    if not args.no_restart:
        device.restart()
        print("已重启 SnowApp，等待看门狗重新拉起；请检查滑动与 Wi-Fi")
    else:
        print("页面已写入，重启 SnowApp 或设备后生效")


if __name__ == "__main__":
    main()
