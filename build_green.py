# -*- coding: utf-8 -*-
"""
打包【绿色免安装版】—— 推荐方案

产出一个文件夹（可压成 zip 发给别人）：
    WeChatBot/
        python/            <- 内嵌的 Python 运行时，不用装 Python
        settings.json      <- 配置，随便改，改完重启就生效
        启动机器人.bat      <- 双击这个跑
        环境自检.bat        <- 双击这个自检
        *.py               <- 源码，想改逻辑直接改，不用重新打包
        logs/

为什么不用 PyInstaller：
  1. uiautomation 底层的 comtypes 会在运行时动态生成 COM 包装代码并写磁盘缓存，
     PyInstaller 打包后那个目录不存在或只读，会报
     FileNotFoundError: ...comtypes\\gen\\_944DE083_..._0_1_0.py
  2. "自动操作微信"的 exe 被 360/火绒误报的概率明显高于普通程序，
     PyInstaller 的自解压行为更是杀软启发式检测的重灾区。
  3. 打包成 exe 后改个关键词都要重新打包；绿色版直接改 .py 存盘就行。
绿色版本质就是一个真实 Python 环境，上面三个问题都不存在。

用法（在 Windows 上跑，需要联网下载 Python 运行时）：
    python build_green.py
    python build_green.py --zip          # 打完顺便压成 zip
"""

import os
import sys
import shutil
import zipfile
import subprocess
import urllib.request

# 嵌入式运行时版本。改版本前先在虚拟机里把打包和启动完整跑一遍
PY_VERSION = "3.11.9"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

APP_NAME = "WeChatBot"
SRC_FILES = [
    "bot.py", "config.py", "check_env.py", "diag_receive.py",
    "ai_provider.py", "backend.py", "handlers.py",
    "settings.example.json", "README.md",
]
SRC_DIRS = ["wxui"]      # 适配层是个包，整个目录拷过去

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")
DIST = os.path.join(BUILD, APP_NAME)
PY_DIR = os.path.join(DIST, "python")


def log(msg):
    print(f"  {msg}")


def step(n, title):
    print(f"\n[{n}] {title}")


def download(url, dest):
    """
    下载文件。三重兜底。

    为什么不只用 urllib：实测过 urllib 会撞上
        SSLCertVerificationError: unable to get local issuer certificate
    这在没装根证书的 Python、或者公司代理环境里很常见。
    Windows 10 1803 以后自带 curl.exe，拿它兜底最省事。
    """
    log(f"下载 {url}")

    # 1) urllib 直连
    try:
        urllib.request.urlretrieve(url, dest)
        log(f"-> {os.path.basename(dest)}  ({os.path.getsize(dest) // 1024} KB)")
        return True
    except Exception as e:
        log(f"[!] urllib 失败: {type(e).__name__}: {e}")

    # 2) curl 兜底（Windows 10 1803+ 自带）
    log("改用 curl 重试...")
    try:
        r = subprocess.run(["curl", "-sL", "--max-time", "180", "-o", dest, url])
        if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            log(f"-> {os.path.basename(dest)}  ({os.path.getsize(dest) // 1024} KB)")
            return True
        log(f"[!] curl 失败（退出码 {r.returncode}）")
    except FileNotFoundError:
        log("[!] 系统里没有 curl")
    except Exception as e:
        log(f"[!] curl 异常: {e}")

    # 3) 让用户手动下
    print(f"\n  [X] 自动下载失败。请手动下载：")
    print(f"      {url}")
    print(f"      存到：{dest}")
    print(f"      然后重新运行本脚本（已存在的文件会直接复用，不会重复下载）")
    return False


def main():
    make_zip = "--zip" in sys.argv

    print("=" * 62)
    print(f"  打包绿色版  ({APP_NAME})")
    print(f"  Python {PY_VERSION} 内嵌运行时 + uiautomation")
    print("=" * 62)

    if sys.platform != "win32":
        print("\n[!] 你现在不在 Windows 上。")
        print("    这个脚本会下载 Windows 版的 Python 运行时并 pip 装 Windows 专属的")
        print("    uiautomation，只有在 Windows 上跑才有意义。")
        print("    请把整个文件夹拷到那台 Windows 电脑上再执行本脚本。")
        return 1

    # ---------- 1. 清理 ----------
    step(1, "清理旧的 build 目录")
    if os.path.exists(DIST):
        shutil.rmtree(DIST)
        log("已删除旧目录")
    os.makedirs(PY_DIR, exist_ok=True)

    # ---------- 2. 下载并解压 Python ----------
    step(2, f"下载 Python {PY_VERSION} 嵌入式运行时")
    zip_path = os.path.join(BUILD, f"python-{PY_VERSION}-embed.zip")
    if os.path.exists(zip_path):
        log("已有缓存，跳过下载")
    elif not download(PY_URL, zip_path):
        return 1

    log("解压中...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(PY_DIR)
    log(f"-> {PY_DIR}")

    # ---------- 3. 打开 site-packages 支持 ----------
    # 嵌入式 Python 默认把 import site 注释掉了，不改这行
    # pip 装的库根本加载不出来。这是最容易踩的一步。
    step(3, "启用 site-packages（改 ._pth）")
    pth = None
    for f in os.listdir(PY_DIR):
        if f.endswith("._pth"):
            pth = os.path.join(PY_DIR, f)
            break
    if not pth:
        print("  [X] 没找到 ._pth 文件，Python 包结构可能变了")
        return 1

    with open(pth, "r", encoding="utf-8") as f:
        content = f.read()
    if "#import site" in content:
        content = content.replace("#import site", "import site")
    elif "import site" not in content:
        content += "\nimport site\n"
    # 让它能 import 上一级目录里的 bot.py 等源码
    if "..\n" not in content and "\n..\n" not in content:
        content = content.rstrip() + "\n..\n"
    with open(pth, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"已修改 {os.path.basename(pth)}")

    # ---------- 4. 装 pip ----------
    step(4, "安装 pip")
    py_exe = os.path.join(PY_DIR, "python.exe")
    get_pip = os.path.join(BUILD, "get-pip.py")
    if not os.path.exists(get_pip) and not download(GET_PIP_URL, get_pip):
        return 1

    r = subprocess.run([py_exe, get_pip, "--no-warn-script-location"],
                       cwd=PY_DIR)
    if r.returncode != 0:
        print("  [X] pip 安装失败")
        return 1
    log("pip 装好了")

    # ---------- 5. 装依赖 ----------
    packages = ["uiautomation", "requests", "pymysql"]
    step(5, f"安装依赖 ({', '.join(packages)})")
    r = subprocess.run(
        [py_exe, "-m", "pip", "install", "-i", PYPI_MIRROR,
         "--no-warn-script-location"] + packages,
        cwd=PY_DIR,
    )
    if r.returncode != 0:
        print("  [X] 依赖安装失败。换官方源重试：")
        print(f"      {py_exe} -m pip install {' '.join(packages)}")
        return 1
    log("依赖装好了")

    # ---------- 6. 拷源码 ----------
    step(6, "拷贝源码")
    for name in SRC_FILES:
        src = os.path.join(HERE, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))
            log(f"+ {name}")
        else:
            log(f"- {name} (不存在，跳过)")
    for name in SRC_DIRS:
        src = os.path.join(HERE, name)
        if not os.path.isdir(src):
            print(f"  [X] 缺少 {name}/ 目录，打出来的包跑不起来")
            return 1
        shutil.copytree(src, os.path.join(DIST, name),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        log(f"+ {name}/")

    os.makedirs(os.path.join(DIST, "logs"), exist_ok=True)

    # settings.json：有就用现成的，没有就从模板复制一份
    settings_dst = os.path.join(DIST, "settings.json")
    settings_src = os.path.join(HERE, "settings.json")
    if os.path.exists(settings_src):
        shutil.copy2(settings_src, settings_dst)
        log("+ settings.json (用了你现有的配置)")
    else:
        shutil.copy2(os.path.join(HERE, "settings.example.json"), settings_dst)
        log("+ settings.json (从模板生成，记得改)")

    # ---------- 7. 写 bat ----------
    step(7, "生成启动脚本")
    bats = {
        "启动机器人.bat": (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "cd /d %~dp0\r\n"
            "title 微信群机器人\r\n"
            "echo ================================================\r\n"
            "echo   微信群机器人启动中...\r\n"
            "echo   配置在 settings.json，改完重启本程序生效\r\n"
            "echo   关闭这个窗口就是停止机器人\r\n"
            "echo ================================================\r\n"
            "echo.\r\n"
            "python\\python.exe bot.py\r\n"
            "echo.\r\n"
            "echo 程序已退出。\r\n"
            "pause\r\n"
        ),
        "环境自检.bat": (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "cd /d %~dp0\r\n"
            "title 环境自检\r\n"
            "python\\python.exe check_env.py --no-install\r\n"
            "pause\r\n"
        ),
        "测试大模型.bat": (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "cd /d %~dp0\r\n"
            "title 大模型对比\r\n"
            "python\\python.exe ai_provider.py\r\n"
            "pause\r\n"
        ),
        "测试后端连接.bat": (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "cd /d %~dp0\r\n"
            "title 后端连通性测试\r\n"
            "python\\python.exe backend.py\r\n"
            "pause\r\n"
        ),
    }
    for name, content in bats.items():
        # bat 用 GBK 存，中文才不会在 cmd 里乱码
        with open(os.path.join(DIST, name), "w", encoding="gbk", errors="replace") as f:
            f.write(content)
        log(f"+ {name}")

    # ---------- 8. 使用说明 ----------
    readme = (
        "微信群机器人 —— 绿色免安装版\r\n"
        "=" * 40 + "\r\n\r\n"
        "不用装 Python，解压就能跑。\r\n\r\n"
        "【第一次使用】\r\n"
        "1. 先在这台电脑上登录微信 PC 版，保持主窗口打开\r\n"
        "2. 双击「环境自检.bat」，确认能连上微信\r\n"
        "3. 从自检输出的会话列表里，复制你要监听的群名\r\n"
        "4. 用记事本打开 settings.json：\r\n"
        "     - LISTEN_GROUPS  填群名（一字不差）\r\n"
        "     - MY_NICKNAME    填你的微信昵称\r\n"
        "     - DRY_RUN        保持 true\r\n"
        "5. 双击「启动机器人.bat」，观察它读到的消息对不对\r\n"
        "6. 确认没问题后，把 settings.json 里 DRY_RUN 改成 false，重启\r\n\r\n"
        "【日常】\r\n"
        "  启动：双击「启动机器人.bat」\r\n"
        "  停止：关掉那个黑窗口\r\n"
        "  改配置：改 settings.json，然后重启\r\n"
        "  看日志：logs 文件夹\r\n\r\n"
        "【注意】\r\n"
        "  - 微信窗口要一直开着，别最小化\r\n"
        "  - 关掉微信的自动更新，微信一升级这个工具可能就失效了\r\n"
        "  - 用远程桌面连过来的话，断开会锁屏，锁屏后就读不到消息了。\r\n"
        "    请改用 ToDesk / 向日葵这类不锁屏的远程软件\r\n"
        "  - 杀毒软件可能报毒（因为它在自动操作微信），加白名单即可\r\n\r\n"
        "【改业务逻辑】\r\n"
        "  这个包里的 .py 文件就是源码，直接用记事本改、存盘、重启就生效，\r\n"
        "  不需要重新打包。\r\n"
        "    handlers.py    —— 查数据库/调后端接口的业务指令\r\n"
        "    settings.json  —— 群名、关键词、AI key、数据库配置\r\n"
    )
    with open(os.path.join(DIST, "使用说明.txt"), "w",
              encoding="gbk", errors="replace") as f:
        f.write(readme)
    log("+ 使用说明.txt")

    # ---------- 9. 压缩 ----------
    size_mb = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(DIST) for f in fs
    ) / 1024 / 1024

    if make_zip:
        step(9, "压缩成 zip")
        zip_out = os.path.join(BUILD, f"{APP_NAME}.zip")
        if os.path.exists(zip_out):
            os.remove(zip_out)
        with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, _, fs in os.walk(DIST):
                for f in fs:
                    full = os.path.join(dp, f)
                    z.write(full, os.path.join(
                        APP_NAME, os.path.relpath(full, DIST)))
        log(f"-> {zip_out}  ({os.path.getsize(zip_out) / 1024 / 1024:.1f} MB)")

    print("\n" + "=" * 62)
    print("  打包完成")
    print("=" * 62)
    print(f"\n输出目录: {DIST}")
    print(f"大小    : {size_mb:.1f} MB")
    print("\n下一步：")
    print(f"  1. 进 {DIST}")
    print("  2. 双击「环境自检.bat」确认能连上微信")
    print("  3. 改 settings.json")
    print("  4. 双击「启动机器人.bat」")
    print("\n整个文件夹可以直接拷到别的 Windows 电脑上跑，不用装 Python。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
