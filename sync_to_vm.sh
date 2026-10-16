#!/bin/bash
# 把 Mac 上的代码同步进 Windows 虚拟机，并【逐个文件比对 SHA256】确认虚拟机里跑的
# 就是这一份 —— 之前几轮的坑就是"Mac 上改完了，虚拟机里跑的还是旧的"。
#
# 用法：
#   ./sync_to_vm.sh                 # 同步 + 校验
#   ./sync_to_vm.sh --check         # 只校验，不覆盖
#
# 需要环境变量 WXBOT_VMPW（虚拟机加密密码 / guest 密码）：
#   export WXBOT_VMPW='...'         # 别写死在文件里
set -u

VR="/Applications/VMware Fusion.app/Contents/Public/vmrun"
VMX="/Users/guotao/Virtual Machines.localized/Windows 11 x64.vmwarevm/Windows 11 x64.vmx"
SRC="$(cd "$(dirname "$0")" && pwd)"
DST='C:\Users\tao\Desktop\python'
GUSER="tao"
FILES=(bot.py config.py check_env.py handlers.py ai_provider.py backend.py diag_receive.py README.md)

if [ -z "${WXBOT_VMPW:-}" ]; then
  echo "请先设置密码环境变量：export WXBOT_VMPW='虚拟机密码'"
  exit 1
fi
PW="$WXBOT_VMPW"
B=( -T fusion -vp "$PW" -gu "$GUSER" -gp "$PW" )

if ! "$VR" list 2>/dev/null | grep -q "Windows 11 x64.vmx"; then
  echo "虚拟机没在跑。先启动："
  echo "  open -a \"VMware Fusion\" \"$(dirname "$VMX")\""
  exit 1
fi

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

if [ "$CHECK_ONLY" = "0" ]; then
  echo "=== 同步 ==="
  for f in "${FILES[@]}"; do
    [ -f "$SRC/$f" ] || { echo "  跳过（本地没有） $f"; continue; }
    if "$VR" "${B[@]}" copyFileFromHostToGuest "$VMX" "$SRC/$f" "$DST\\$f" >/dev/null 2>&1; then
      echo "  -> $f"
    else
      echo "  !! $f 同步失败"
    fi
  done
fi

echo
echo "=== 校验（Mac sha256 vs 虚拟机 sha256）==="
TMP=$(mktemp -d)
cat > "$TMP/h.ps1" <<'PSEOF'
param([string]$Out)
$d='C:\Users\tao\Desktop\python'
$L=@()
foreach($f in (Get-ChildItem $d -File -Include *.py,*.md -Recurse -Depth 0 -EA SilentlyContinue)){
  $L += ("{0} {1}" -f (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower(), $f.Name)
}
($L -join "`r`n") | Set-Content -Path $Out -Encoding UTF8
PSEOF
printf '\xef\xbb\xbf' > "$TMP/h.bom.ps1"; cat "$TMP/h.ps1" >> "$TMP/h.bom.ps1"
"$VR" "${B[@]}" copyFileFromHostToGuest "$VMX" "$TMP/h.bom.ps1" 'C:\Users\tao\_h.ps1' >/dev/null 2>&1
"$VR" "${B[@]}" deleteFileInGuest "$VMX" 'C:\Users\tao\_h.txt' >/dev/null 2>&1
"$VR" "${B[@]}" runProgramInGuest "$VMX" \
  'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' \
  '-NoProfile' '-ExecutionPolicy' 'Bypass' '-File' 'C:\Users\tao\_h.ps1' 'C:\Users\tao\_h.txt' >/dev/null 2>&1
"$VR" "${B[@]}" copyFileFromGuestToHost "$VMX" 'C:\Users\tao\_h.txt' "$TMP/h.txt" >/dev/null 2>&1

BAD=0
for f in "${FILES[@]}"; do
  [ -f "$SRC/$f" ] || continue
  MINE=$(shasum -a 256 "$SRC/$f" | cut -d' ' -f1)
  # 注意：PowerShell 写出来的文件带 UTF-8 BOM，不剥掉会粘在第一行的哈希上造成假不一致
  THEIRS=$(sed $'1s/^\xef\xbb\xbf//' "$TMP/h.txt" 2>/dev/null | tr -d '\r' \
           | awk -v n="$f" '$2==n{print $1}')
  if [ -z "$THEIRS" ]; then
    echo "  ?  $f  虚拟机里没找到"; BAD=1
  elif [ "$MINE" = "$THEIRS" ]; then
    echo "  OK $f  ${MINE:0:16}"
  else
    echo "  != $f  Mac=${MINE:0:16}  VM=${THEIRS:0:16}"; BAD=1
  fi
done
rm -rf "$TMP"

echo
if [ "$BAD" = "0" ]; then
  echo "全部一致。虚拟机里跑的就是这一份代码。"
  echo
  echo "在虚拟机里启动（PowerShell / cmd，必须是已登录的交互式桌面）："
  echo "  cd C:\\Users\\tao\\Desktop\\python"
  echo "  python diag_receive.py \"测试群\"     # 先分段自检，不发消息"
  echo "  python bot.py                        # DRY_RUN=True 时不会真发"
  echo
  echo "日志： C:\\Users\\tao\\Desktop\\python\\logs\\bot.log      运行/决策/发送结果"
  echo "       C:\\Users\\tao\\Desktop\\python\\logs\\messages.log 收到的每条消息"
else
  echo "有文件不一致，别急着测 —— 先把上面标 != 的重新同步。"
  exit 1
fi
