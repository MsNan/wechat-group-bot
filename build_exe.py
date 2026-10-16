# -*- coding: utf-8 -*-
"""
打包成 exe（PyInstaller）—— 备选方案

先说清楚：**这个场景我更推荐 build_green.py（绿色版）**，原因见下面。
你如果就是想要一个 exe 图标，用这个脚本。

用法（在 Windows 上跑）：
    pip install pyinstaller
    python build_exe.py
    python build_exe.py --console     # 保留黑窗口看日志（调试期建议加）


═══ 为什么 exe 方案更麻烦 ═══

1. comtypes 的动态缓存问题
   wxui 底层是 uiautomation -> comtypes。comtypes 会在【首次运行时】
   动态生成 COM 接口的 Python 包装代码，缓存到 site-packages/comtypes/gen/。
   PyInstaller 静态分析扫不到这些"运行时才存在"的模块，打包后会报：
       FileNotFoundError: ...comtypes\\gen\\_944DE083_8FB8_45CF_..._0_1_0.py
       ModuleNotFoundError: No module named 'comtypes.stream'
   本脚本已经做了两处修复：
       a) 打包前先跑一次 import，让 comtypes 把 gen 目录生成出来，
          再用 --add-data 整个带进去
       b) bot.py 启动时设 comtypes.client.gen_dir = None，
          让它只在内存里生成，完全不碰文件系统

2. 杀毒软件误报
   一个"自动操作微信客户端 UI + 发网络请求 + 读写本地文件"的 exe，
   行为特征和微信盗号木马高度相似，360/火绒/Defender 报毒的概率
   明显高于普通程序。所以本脚本：
       - 强制用 --onedir 而不是 --onefile
         （onefile 每次启动要自解压到临时目录，这个行为本身就是
           杀软启发式检测的重灾区，误报率显著更高）
       - 关掉 UPX 压缩（--noupx，压缩壳也是常见恶意软件特征）
   即便如此还是可能被拦，到时候加白名单就行。

3. 改配置 / 改逻辑
   exe 里的代码改不了。所以配置必须外置成 settings.json 放在 exe 旁边
   （config.py 已经支持了）。但业务逻辑（handlers.py 里查什么表）
   要改的话，还是得重新打包。绿色版就没这个问题。
"""

import os
import sys
import shutil
import subprocess

APP_NAME = "WeChatBot"
HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build_pyinstaller")


def main():
    keep_console = "--console" in sys.argv

    print("=" * 62)
    print(f"  PyInstaller 打包  ({APP_NAME})")
    print("=" * 62)

    if sys.platform != "win32":
        print("\n[!] 不在 Windows 上，打不出能用的包。")
        print("    请把整个文件夹拷到那台 Windows 电脑上再跑。")
        return 1

    # ---------- 检查 pyinstaller ----------
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("\n[X] 没装 PyInstaller。先跑：")
        print("    pip install pyinstaller")
        return 1

    # ---------- 关键一步：预生成 comtypes 缓存 ----------
    print("\n[1] 预生成 comtypes COM 包装缓存")
    print("    （不做这步，打出来的 exe 会报 FileNotFoundError）")
    gen_dir = None
    try:
        import comtypes.client
        import comtypes.gen
        # 触发 UIAutomation 的 typelib 生成
        try:
            __import__("uiautomation")
        except ImportError:
            print("    [X] 没装 uiautomation，先跑 check_env.py --install")
            return 1
        gen_dir = os.path.dirname(comtypes.gen.__file__)
        files = [f for f in os.listdir(gen_dir) if f.endswith(".py")]
        print(f"    gen 目录: {gen_dir}")
        print(f"    已生成 {len(files)} 个包装模块")
        if len(files) <= 1:
            print("    [!] 只有 __init__.py，说明包装代码还没生成。")
            print("        建议先在这台机器上正常跑一次 check_env.py，")
            print("        让 comtypes 把缓存生成出来，再回来打包。")
    except ImportError as e:
        print(f"    [X] comtypes 相关导入失败: {e}")
        return 1

    # ---------- 清理 ----------
    print("\n[2] 清理旧产物")
    for d in (DIST, BUILD):
        if os.path.exists(d):
            shutil.rmtree(d)
            print(f"    已删 {os.path.basename(d)}")
    spec = os.path.join(HERE, f"{APP_NAME}.spec")
    if os.path.exists(spec):
        os.remove(spec)

    # ---------- 组命令 ----------
    print("\n[3] 执行 PyInstaller")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,

        # onedir 而不是 onefile：启动快、杀软误报低、配置文件好放
        "--onedir",

        # 不用 UPX 压缩，压缩壳是杀软重点关注特征
        "--noupx",

        "--distpath", DIST,
        "--workpath", BUILD,
        "--specpath", HERE,
        "--noconfirm",

        # comtypes 相关：官方 hook 只覆盖了一部分，这几个要手动加
        "--hidden-import", "comtypes.stream",
        "--hidden-import", "comtypes.persist",
        "--hidden-import", "comtypes.typeinfo",
        "--hidden-import", "comtypes.automation",
        "--hidden-import", "comtypes.gen",
        "--hidden-import", "comtypes.gen.UIAutomationClient",

        # 把整个包收进来，避免动态 import 漏掉
        "--collect-submodules", "comtypes",
        "--collect-submodules", "uiautomation",
        "--collect-submodules", "wxui",

        "--hidden-import", "requests",
        "--hidden-import", "pymysql",

        # 本项目自己的模块
        "--hidden-import", "config",
        "--hidden-import", "handlers",
        "--hidden-import", "ai_provider",
        "--hidden-import", "backend",
    ]

    # 把预生成的 comtypes gen 目录整个带上
    if gen_dir and os.path.isdir(gen_dir):
        cmd += ["--add-data", f"{gen_dir};comtypes/gen"]

    icon = os.path.join(HERE, "app.ico")
    if os.path.exists(icon):
        cmd += ["--icon", icon]

    if not keep_console:
        cmd.append("--noconsole")
    else:
        print("    保留控制台窗口（--console）")

    cmd.append(os.path.join(HERE, "bot.py"))

    print(f"    {' '.join(cmd[:8])} ...")
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print(f"\n[X] 打包失败（退出码 {r.returncode}）")
        return 1

    # ---------- 放配置和说明 ----------
    print("\n[4] 放配置文件")
    out_dir = os.path.join(DIST, APP_NAME)

    settings_dst = os.path.join(out_dir, "settings.json")
    settings_src = os.path.join(HERE, "settings.json")
    template = os.path.join(HERE, "settings.example.json")
    if os.path.exists(settings_src):
        shutil.copy2(settings_src, settings_dst)
        print("    + settings.json (用了你现有的配置)")
    elif os.path.exists(template):
        shutil.copy2(template, settings_dst)
        print("    + settings.json (从模板生成，记得改)")
    if os.path.exists(template):
        shutil.copy2(template, os.path.join(out_dir, "settings.example.json"))

    os.makedirs(os.path.join(out_dir, "logs"), exist_ok=True)

    with open(os.path.join(out_dir, "使用说明.txt"), "w",
              encoding="gbk", errors="replace") as f:
        f.write(
            "微信群机器人\r\n" + "=" * 40 + "\r\n\r\n"
            f"双击 {APP_NAME}.exe 启动。\r\n\r\n"
            "配置改 settings.json（记事本打开就行），改完重启程序生效。\r\n"
            "  LISTEN_GROUPS  要监听的群名，必须一字不差\r\n"
            "  MY_NICKNAME    你的微信昵称\r\n"
            "  DRY_RUN        true=只看不发（第一次务必保持 true）\r\n\r\n"
            "日志在 logs 文件夹。\r\n\r\n"
            "注意：\r\n"
            "  - 微信要登录着、主窗口开着，别最小化\r\n"
            "  - 杀毒软件可能报毒，加白名单即可\r\n"
            "  - 关掉微信自动更新，微信升级可能导致本工具失效\r\n"
        )
    print("    + 使用说明.txt")

    print("\n" + "=" * 62)
    print("  打包完成")
    print("=" * 62)
    print(f"\n输出: {out_dir}")
    print(f"入口: {os.path.join(out_dir, APP_NAME + '.exe')}")
    print("\n[重要] 打完必须实测这几个场景，comtypes 的坑就藏在这里：")
    print("  1. 双击 exe 能不能正常启动（第一次运行最容易暴露问题）")
    print("  2. 能不能读到群消息")
    print("  3. 关掉再开一次，第二次还能不能跑")
    print("  4. 改 settings.json 后重启，配置有没有生效")
    print("\n如果报 comtypes / UIAutomationClient 相关的错，")
    print("说明这条路走不通，请改用 build_green.py 的绿色版方案。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
