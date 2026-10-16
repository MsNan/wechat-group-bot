<#
    还原 win_autologon.ps1 做的改动。

    重点是【清掉注册表里的明文密码】。
    跑完之后重启就恢复成需要手动登录。

    用法（右键 PowerShell -> 以管理员身份运行）：
        cd C:\Users\tao\Desktop\python
        powershell -ExecutionPolicy Bypass -File .\win_autologon_revert.ps1
#>

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "需要管理员权限。请右键 PowerShell -> 以管理员身份运行。" -ForegroundColor Red
    Read-Host "按回车退出"; exit 1
}

$WinlogonKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'

Write-Host "=== 还原 ===" -ForegroundColor Cyan
Set-ItemProperty -Path $WinlogonKey -Name 'AutoAdminLogon' -Value '0' -Type String
Write-Host "  AutoAdminLogon -> 0"

foreach ($n in 'DefaultPassword', 'AutoLogonCount', 'DefaultDomainName') {
    if ($null -ne (Get-ItemProperty -Path $WinlogonKey -Name $n -ErrorAction SilentlyContinue)) {
        Remove-ItemProperty -Path $WinlogonKey -Name $n -ErrorAction SilentlyContinue
        Write-Host "  已删除 $n"
    }
}

Write-Host "`n=== 复核（DefaultPassword 必须是 False）===" -ForegroundColor Cyan
$a = Get-ItemProperty $WinlogonKey
Write-Host ("  AutoAdminLogon = {0}" -f $a.AutoAdminLogon)
Write-Host ("  DefaultPassword 已设 = {0}" -f ($null -ne $a.DefaultPassword))

Write-Host "`n注意：电源/屏保设置没有一起改回去（它们对挂机机器人有用，且不涉及密码）。" -ForegroundColor Yellow
Write-Host "想改回默认： powercfg /change monitor-timeout-ac 10"
Read-Host "`n按回车退出"
