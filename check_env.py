# -*- coding: utf-8 -*-
"""
微信群机器人 —— 环境自检 + 自动装依赖（第一步先跑这个）

它会：
  1. 检查 Python 与操作系统
  2. 检查微信客户端版本（必须是 4.1.8.107）
  3. 检测缺哪些依赖，并【自动装上】
  4. 连接微信，列出会话列表 -> 拿到群名的精确写法（差一个字就监听不到）
  5. 检查配置

用法：
    python check_env.py              自检 + 问你要不要装依赖
    python check_env.py --install    自检 + 直接装，不问
    python check_env.py --no-install 只检查，不装

这个脚本不会发任何消息、不改微信任何设置。
"""

import sys
import os
import subprocess

SEP = "=" * 62

# 实测唯一可用的微信版本，与 bot.py 的 SUPPORTED_CEILING 保持一致
REQUIRED_WECHAT = (4, 1, 8, 107)
REQUIRED_STR = ".".join(str(x) for x in REQUIRED_WECHAT)


def section(title):
    print(f"\n{SEP}\n  {title}\n{SEP}")


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:4])
    except Exception:
        return ()


# --------------------------------------------------------------------
# 1. Python 环境
# --------------------------------------------------------------------
def check_python():
    section("1. Python 环境")
    print(f"Python 版本: {sys.version.split()[0]}")
    print(f"解释器路径: {sys.executable}")
    print(f"操作系统  : {sys.platform}")

    if sys.platform != "win32":
        print("\n[X] 致命问题：当前不是 Windows。")
        print("    机器人靠 Windows 的界面自动化接口读写微信，Mac/Linux 上跑不了。")
        print("    请把整个文件夹拷到那台闲置 Windows 电脑上再跑。")
        return False

    major, minor = sys.version_info[:2]
    if major == 3 and 9 <= minor <= 12:
        print(f"\n[OK] Python {major}.{minor} 在支持范围内 (3.9 ~ 3.12)")
    else:
        print(f"\n[!] 警告：支持范围是 Python 3.9 ~ 3.12，你是 {major}.{minor}")
        print("    出问题请装 3.12。")
    return True


# --------------------------------------------------------------------
# 2. 微信客户端版本
# --------------------------------------------------------------------
def check_wechat_version():
    section(f"2. 微信客户端版本（必须是 {REQUIRED_STR}）")

    version = None
    install_path = None

    try:
        import winreg
        for hive, path in [
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\WeChat"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\WeChat"),
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin"),
        ]:
            try:
                with winreg.OpenKey(hive, path) as key:
                    install_path = winreg.QueryValueEx(key, "InstallPath")[0]
                    print(f"安装路径: {install_path}")
                    break
            except OSError:
                continue
    except Exception as e:
        print(f"[!] 读注册表失败: {e}")

    # 3.9.x 的安装目录下有 [3.9.12.17] 这种版本号文件夹
    if install_path and os.path.isdir(install_path):
        try:
            for name in sorted(os.listdir(install_path), reverse=True):
                if name.startswith("[") and name.endswith("]"):
                    version = name.strip("[]")
                    print(f"检测到版本目录: {name}")
                    break
        except Exception:
            pass

    # 直接读 exe 的文件版本号
    if not version and install_path:
        for exe_name in ("WeChat.exe", "Weixin.exe"):
            exe = os.path.join(install_path, exe_name)
            if os.path.exists(exe):
                version = _read_exe_version(exe)
                if version:
                    print(f"{exe_name} 文件版本: {version}")
                    break

    print()
    if not version:
        print("[?] 没能自动读出微信版本。请手动查：微信 -> 左下角三条杠 -> 关于微信")
        print(f"    必须是 {REQUIRED_STR}")
        return None

    v = _ver_tuple(version)
    if v == REQUIRED_WECHAT:
        print(f"[OK] 微信 {version}，是实测可用的版本")
    elif v and v[0] == 3:
        print(f"[X] 微信 {version} 是 3.9.x，登录时会被腾讯服务端以「版本过低」拒绝。")
        print(f"    请装 {REQUIRED_STR}，并关掉自动更新。")
    elif v > REQUIRED_WECHAT:
        print(f"[X] 微信 {version} 高于 {REQUIRED_STR}。")
        print("    4.1.13 及以上没有实现无障碍接口，界面自动化读不到任何内容。")
        print(f"    请降级到 {REQUIRED_STR}，并关掉自动更新（不关会被静默升回去）。")
    else:
        print(f"[!] 微信 {version} 没有实测过，实测可用的是 {REQUIRED_STR}。")
    return version


def _read_exe_version(path):
    """用 ctypes 读 exe 的文件版本号，不需要装 pywin32。"""
    try:
        import ctypes
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf)

        r = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
            buf, "\\", ctypes.byref(r), ctypes.byref(length)
        ):
            return None

        # VS_FIXEDFILEINFO: dwFileVersionMS 偏移 8，dwFileVersionLS 偏移 12
        data = ctypes.string_at(r.value, length.value)
        ms = int.from_bytes(data[8:12], "little")
        ls = int.from_bytes(data[12:16], "little")
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


# --------------------------------------------------------------------
# 3. 依赖检查 + 自动安装
# --------------------------------------------------------------------
def _installed(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def check_deps():
    section("3. 依赖检查")
    missing = []
    for mod, why in [
        ("uiautomation", "读写微信界面，必需"),
        ("requests", "AI 接口 + 后端 HTTP 接口，必需"),
        ("pymysql", "直连 MySQL 才需要，不连库可以不装"),
    ]:
        ok = _installed(mod)
        print(f"  {mod:13s}: {'已装' if ok else '未装'}   （{why}）")
        if not ok:
            missing.append(mod)      # 可选的也一起装了省事，装了不用也没坏处
    return missing


def install(packages):
    """调用当前解释器的 pip 装包。"""
    if not packages:
        return True

    section(f"正在安装: {', '.join(packages)}")

    # 用 sys.executable 而不是裸 pip，确保装到当前这个 Python 里
    # （多版本 Python 共存时，裸 pip 经常装错地方）
    cmd = [sys.executable, "-m", "pip", "install", "-i",
           "https://pypi.tuna.tsinghua.edu.cn/simple"] + packages
    print(f"执行: {' '.join(cmd)}\n")

    try:
        result = subprocess.run(cmd)
        if result.returncode == 0:
            print("\n[OK] 安装完成")
            return True
        print(f"\n[X] 安装失败（退出码 {result.returncode}）")
        print("    换官方源试试：")
        print(f"    {sys.executable} -m pip install {' '.join(packages)}")
        return False
    except Exception as e:
        print(f"\n[X] 安装出错: {e}")
        return False


# --------------------------------------------------------------------
# 4. 连微信 + 列会话
# --------------------------------------------------------------------
def check_connection():
    section("4. 连接微信客户端")

    try:
        import wxui
    except Exception as e:
        print(f"[X] 导入 wxui 失败: {type(e).__name__}: {e}")
        print("    多半是依赖没装全，按上面第 3 步装好后重跑。")
        return None

    try:
        wx = wxui.WeChat()
    except wxui.AccessibilityUnavailable as e:
        print(f"[X] {e}")
        print(f"    这是微信版本问题，不是配置问题。请降级到 {REQUIRED_STR} 并关掉自动更新。")
        return None
    except Exception as e:
        print(f"[X] 连不上微信: {type(e).__name__}: {e}")
        print("\n排查：")
        print("  1. 微信 PC 客户端【已登录】了吗？")
        print("  2. 微信主窗口是打开的吗（别缩在托盘里）？")
        print("  3. Windows 是不是停在锁屏 / 登录界面？")
        print("  4. 试试用【管理员权限】运行")
        return None

    print(f"[OK] 已连上微信，当前账号: {getattr(wx, 'nickname', '(取不到)')}")

    section("5. 当前会话列表（从这里复制群名的精确写法）")
    try:
        sessions = wx.GetSession()
        if not sessions:
            print("[!] 没读到会话。把微信窗口切到前台再跑一次。")
        else:
            print(f"共 {len(sessions)} 个会话：\n")
            for i, s in enumerate(sessions, 1):
                print(f"  {i:2d}. {getattr(s, 'name', None) or str(s)!r}")
            print("\n[重要] 上面是带引号的原样输出。把群名【一字不差】复制到")
            print("       config.py 的 LISTEN_GROUPS（或 settings.json）里。")
            print("       空格、emoji、特殊符号都必须完全一致。")
    except Exception as e:
        print(f"[!] 读会话列表失败: {e}")
        print("    不影响使用，手动抄群名也行。")

    return wx


# --------------------------------------------------------------------
# 5. 配置检查
# --------------------------------------------------------------------
def check_config():
    section("6. 配置检查")
    try:
        import config
    except Exception as e:
        print(f"[X] 加载 config.py 失败: {e}")
        return

    print(f"配置来源  : {'settings.json + config.py' if config.LOADED_FROM_JSON else 'config.py（没有 settings.json）'}")
    print(f"演习模式  : {'开（不会真发消息）' if config.DRY_RUN else '关（会真发消息！）'}")
    print(f"监听群    : {config.LISTEN_GROUPS or '[空！还没配]'}")
    print(f"我的昵称  : {config.MY_NICKNAME or '(空，无法判断 @我)'}")
    print(f"关键词规则: {len(config.RULES)} 条")

    ai = config.AI
    if ai.get("enabled"):
        keys = [p for p in ai.get("providers", []) if p.get("api_key")]
        print(f"AI 回复   : 开启，已配 {len(keys)} 个模型")
        if not keys:
            print("            [!] enabled=true 但一个 api_key 都没填，AI 实际不会生效")
    else:
        print("AI 回复   : 关闭")

    b = config.BACKEND
    print(f"后端接口  : {'开启 ' + b['http'].get('base_url', '') if b.get('http', {}).get('enabled') else '关闭'}")
    print(f"数据库    : {'开启' if b.get('mysql', {}).get('enabled') else '关闭'}")
    print(f"消息入库  : {'开启' if b.get('save_messages', {}).get('enabled') else '关闭'}")



# --------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    auto_install = "--install" in args
    no_install = "--no-install" in args

    print("\n" + SEP)
    print("  微信群机器人 —— 环境自检")
    print("  只读检查，不会发送任何消息")
    print(SEP)

    if not check_python():
        return

    check_wechat_version()
    missing = check_deps()

    if missing:
        print(f"\n缺少依赖: {', '.join(missing)}")
        do_install = auto_install
        if not auto_install and not no_install:
            try:
                ans = input("\n要现在自动安装吗？[Y/n] ").strip().lower()
                do_install = ans in ("", "y", "yes")
            except (EOFError, KeyboardInterrupt):
                do_install = False

        if do_install:
            install(missing)
        else:
            print(f"\n跳过安装。手动装： pip install {' '.join(missing)}")
    else:
        print("\n[OK] 依赖齐全")

    wx = check_connection()
    check_config()

    section("自检结束")
    if wx is not None:
        print("[OK] 环境通了，下一步：")
        print("  1. 改 config.py（或 settings.json）里的 LISTEN_GROUPS 和 MY_NICKNAME")
        print("  2. 保持 DRY_RUN=True 跑一次 bot.py —— 只看不发，验证能读到消息")
        print("  3. 确认读得对了，再把 DRY_RUN 改成 False")
        # print("\n  可选：")  # [FREE] 付费版工具
        # print("    python ai_provider.py     看各家大模型对比和拿 key 的地址")  # [FREE] 付费版工具
        # print("    python backend.py         测后端/数据库连通性")  # [FREE] 付费版工具
        # print("    python handlers.py        看已注册的业务指令")  # [FREE] 付费版工具
    else:
        print("[X] 环境还没通，按上面提示解决后重跑本脚本。")
    print()


if __name__ == "__main__":
    main()
    if sys.platform == "win32":
        try:
            input("按回车退出...")
        except (EOFError, KeyboardInterrupt):
            pass
