<#
    Windows 自动登录 + 防锁屏设置（挂机跑机器人必须做）

    为什么需要：
      机器人靠读 Windows 界面自动化接口工作，必须有一个【已登录的交互式桌面会话】。
      Windows 停在登录界面时（只有 LogonUI、没有 explorer），UI 自动化一律跑不了。
      机器重启后如果不自动登录，机器人就永远起不来。

    注意：AutoAdminLogon 会把密码【明文】存进注册表
          HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\DefaultPassword
          任何本机管理员都能读到。这台机器如果只跑机器人、装的是微信小号，可以接受；
          不接受就别开，改成每次手动登录。
          还原：跑 win_autologon_revert.ps1

    用法（在虚拟机里，右键 PowerShell -> 以管理员身份运行）：
        cd C:\Users\tao\Desktop\python
        powershell -ExecutionPolicy Bypass -File .\win_autologon.ps1

    密码是【运行时才输入】的，不写在这个文件里。
#>

$ErrorActionPreference = 'Stop'

# --- 必须管理员 ---
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "需要管理员权限。请右键 PowerShell -> 以管理员身份运行，再跑一次。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

$WinlogonKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$PwLessKey   = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'

Write-Host "=== 改前快照（还原时对照用）===" -ForegroundColor Cyan
$before = Get-ItemProperty $WinlogonKey -ErrorAction SilentlyContinue
Write-Host ("  AutoAdminLogon    = {0}" -f $before.AutoAdminLogon)
Write-Host ("  DefaultUserName   = {0}" -f $before.DefaultUserName)
Write-Host ("  DefaultDomainName = {0}" -f $before.DefaultDomainName)
Write-Host ("  DefaultPassword 已设 = {0}" -f ($null -ne $before.DefaultPassword))

$user = Read-Host "`n自动登录的用户名（直接回车用 tao）"
if ([string]::IsNullOrWhiteSpace($user)) { $user = 'tao' }

$sec = Read-Host "该账号的 Windows 密码（输入不回显）" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}
if ([string]::IsNullOrEmpty($plain)) {
    Write-Host "密码为空，中止。" -ForegroundColor Red; Read-Host "按回车退出"; exit 1
}

Write-Host "`n=== 写入 ===" -ForegroundColor Cyan

# Win11 必须先置 0，否则 AutoAdminLogon 会被 PIN / 无密码登录策略吃掉
New-Item -Path $PwLessKey -Force -ErrorAction SilentlyContinue | Out-Null
Set-ItemProperty -Path $PwLessKey -Name 'DevicePasswordLessBuildVersion' -Value 0 -Type DWord
Write-Host "  DevicePasswordLessBuildVersion -> 0"

Set-ItemProperty -Path $WinlogonKey -Name 'AutoAdminLogon'    -Value '1'   -Type String
Set-ItemProperty -Path $WinlogonKey -Name 'DefaultUserName'   -Value $user -Type String
Set-ItemProperty -Path $WinlogonKey -Name 'DefaultDomainName' -Value $env:COMPUTERNAME -Type String
Set-ItemProperty -Path $WinlogonKey -Name 'DefaultPassword'   -Value $plain -Type String
Remove-ItemProperty -Path $WinlogonKey -Name 'AutoLogonCount' -ErrorAction SilentlyContinue
$plain = $null
Write-Host "  AutoAdminLogon    -> 1"
Write-Host "  DefaultUserName   -> $user"
Write-Host "  DefaultDomainName -> $env:COMPUTERNAME"
Write-Host "  DefaultPassword   -> 已写入（明文）"

Write-Host "`n=== 防锁屏（不做的话挂久了会锁屏，界面不刷新机器人就废了）===" -ForegroundColor Cyan
powercfg /change monitor-timeout-ac 0   | Out-Null
powercfg /change standby-timeout-ac 0   | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null
Write-Host "  显示器 / 待机 / 休眠 超时 -> 从不"
Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name 'ScreenSaveActive'    -Value '0' -Type String -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name 'ScreenSaverIsSecure' -Value '0' -Type String -ErrorAction SilentlyContinue
Write-Host "  屏幕保护 -> 关"

Write-Host "`n=== 写后复核 ===" -ForegroundColor Cyan
$after = Get-ItemProperty $WinlogonKey
Write-Host ("  AutoAdminLogon    = {0}" -f $after.AutoAdminLogon)
Write-Host ("  DefaultUserName   = {0}" -f $after.DefaultUserName)
Write-Host ("  DefaultDomainName = {0}" -f $after.DefaultDomainName)
Write-Host ("  DefaultPassword 已设 = {0}" -f ($null -ne $after.DefaultPassword))
Write-Host ("  DevicePasswordLessBuildVersion = {0}" -f (Get-ItemProperty $PwLessKey).DevicePasswordLessBuildVersion)

Write-Host "`n完成。重启后会自动登录到桌面。" -ForegroundColor Green
Write-Host "不想要了就跑： powershell -ExecutionPolicy Bypass -File .\win_autologon_revert.ps1"
Read-Host "`n按回车退出"
