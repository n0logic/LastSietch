# ============================================================
# Last Sietch Windows Game Server Prep
# Hardens a fresh Win 11 Pro VM for game server hosting.
# Idempotent + reversible. Safe to re-run.
# Designed to be run via `qm guest exec` from the a hypervisor host
# OR interactively in an elevated PowerShell on the VM.
# ============================================================

[CmdletBinding()]
param(
    [string]$ComputerName = '',
    [switch]$Apply
)

$ErrorActionPreference = 'Continue'
$null = New-Item -Path C:\LASTSIETCH -ItemType Directory -Force
Start-Transcript -Path C:\LASTSIETCH\hardening.log -Append | Out-Null

# Services we will NEVER touch even if a future pass adds more disables.
$ProtectedServices = @(
    'WinRM','RpcSs','RpcEptMapper','NlaSvc','Dnscache','Dhcp',
    'NetSetupSvc','LanmanServer','LanmanWorkstation','mpssvc','BFE','nsi',
    'QEMU-GA'
)

function Snapshot-NetState {
    param([string]$Tag)
    $f = "C:\LASTSIETCH\hardening.$Tag.log"
    $sb = New-Object System.Text.StringBuilder
    $null = $sb.AppendLine("=== $Tag @ $(Get-Date -Format o) ===")
    $null = $sb.AppendLine((Get-NetIPAddress -InterfaceAlias Ethernet -AddressFamily IPv4 -ErrorAction SilentlyContinue | Format-List | Out-String))
    $null = $sb.AppendLine((Get-NetRoute -InterfaceAlias Ethernet -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Format-List | Out-String))
    $null = $sb.AppendLine((Get-DnsClientServerAddress -InterfaceAlias Ethernet -AddressFamily IPv4 -ErrorAction SilentlyContinue | Format-List | Out-String))
    $null = $sb.AppendLine((Get-NetConnectionProfile -InterfaceAlias Ethernet -ErrorAction SilentlyContinue | Format-List | Out-String))
    $null = $sb.AppendLine((Get-NetFirewallProfile | Format-Table Name, Enabled, DefaultInboundAction, DefaultOutboundAction -AutoSize | Out-String))
    Set-Content -Path $f -Value $sb.ToString() -Encoding UTF8
    "  [SNAP] $Tag captured to $f"
}

function Set-RegValue {
    param([string]$Path, [string]$Name, $Value, [string]$Type = 'DWord')
    if (-not (Test-Path $Path)) {
        if ($Apply) { New-Item -Path $Path -Force | Out-Null }
    }
    $current = (Get-ItemProperty -Path $Path -Name $Name -ErrorAction SilentlyContinue).$Name
    if ($current -ceq $Value) { "  [SKIP] $Path :: $Name already = $Value"; return }
    if ($Apply) {
        Set-ItemProperty -Path $Path -Name $Name -Value $Value -Type $Type -Force
        "  [APPLY] $Path :: $Name = $Value (was: $current)"
    } else {
        "  [DRY] $Path :: $Name $current -> $Value"
    }
}

function Disable-Svc {
    param([string]$Name)
    if ($ProtectedServices -contains $Name) {
        "  [GUARD] refusing to touch protected service $Name"
        return
    }
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $svc) { "  [MISS] $Name (not present)"; return }
    if ($svc.StartType -eq 'Disabled' -and $svc.Status -eq 'Stopped') {
        "  [SKIP] $Name already disabled+stopped"; return
    }
    if ($Apply) {
        try { Stop-Service $Name -Force -ErrorAction Stop } catch { "  [WARN] stop $Name failed: $_" }
        try { Set-Service $Name -StartupType Disabled -ErrorAction Stop } catch { "  [WARN] disable $Name failed: $_" }
        "  [APPLY] $Name -> Disabled/Stopped (was: $($svc.StartType)/$($svc.Status))"
    } else {
        "  [DRY] $Name $($svc.StartType)/$($svc.Status) -> Disabled/Stopped"
    }
}

function Disable-SchTask {
    param([string]$Path, [string]$Name)
    $task = Get-ScheduledTask -TaskPath $Path -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { "  [MISS] $Path$Name"; return }
    if ($task.State -eq 'Disabled') { "  [SKIP] $Path$Name already disabled"; return }
    if ($Apply) { Disable-ScheduledTask -InputObject $task | Out-Null; "  [APPLY] $Path$Name disabled" }
    else { "  [DRY] $Path$Name -> Disabled" }
}

function Remove-AppxFull {
    param([string]$Name)
    $pkg  = Get-AppxPackage -Name $Name -AllUsers -ErrorAction SilentlyContinue
    $prov = Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | Where-Object DisplayName -eq $Name
    if (-not $pkg -and -not $prov) { "  [SKIP] $Name absent"; return }
    if ($Apply) {
        if ($pkg)  { try { Remove-AppxPackage -Package $pkg.PackageFullName -AllUsers -ErrorAction Stop } catch { "  [WARN] remove $Name installed: $_" } }
        if ($prov) { try { Remove-AppxProvisionedPackage -Online -PackageName $prov.PackageName -ErrorAction Stop | Out-Null } catch { "  [WARN] deprovision ${Name}: $_" } }
        "  [APPLY] $Name removed"
    } else {
        "  [DRY] $Name -> removed"
    }
}

# ------------------------------------------------------------
"`n#### Last Sietch Windows Game Server Prep ####"
"Mode: $(if ($Apply) {'APPLY'} else {'DRY-RUN'})"
"Time: $(Get-Date -Format o)"
"Computer: $((Get-CimInstance Win32_ComputerSystem).Name)"
"Build: $((Get-CimInstance Win32_OperatingSystem).Caption) $((Get-CimInstance Win32_OperatingSystem).Version)"

# ---- Pre-flight network snapshot ----
"`n=== [0] Pre-flight ==="
Snapshot-NetState -Tag 'pre'
# Guarantee QEMU-GA is healthy
$qga = Get-Service -Name 'QEMU-GA' -ErrorAction SilentlyContinue
if ($qga) {
    "  [QGA] $($qga.Status) / $($qga.StartType)"
    if ($Apply -and ($qga.StartType -ne 'Automatic')) { Set-Service QEMU-GA -StartupType Automatic; "  [APPLY] QGA -> Automatic" }
    if ($Apply -and ($qga.Status -ne 'Running'))      { Start-Service QEMU-GA; "  [APPLY] QGA -> Started" }
} else {
    "  [WARN] QEMU-GA service missing -- verify virtio-win-guest-tools is installed"
}

# ---- 1. Privacy / telemetry ----
"`n=== [1] Privacy / telemetry ==="
Disable-Svc 'DiagTrack'
Disable-Svc 'dmwappushservice'
Set-RegValue 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection' 'AllowTelemetry' 1
Set-RegValue 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\AdvertisingInfo' 'Enabled' 0
Set-RegValue 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Search' 'BingSearchEnabled' 0
Set-RegValue 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Search' 'CortanaConsent' 0
Set-RegValue 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search' 'AllowCortana' 0
Set-RegValue 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' 'EnableActivityFeed' 0
Set-RegValue 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' 'PublishUserActivities' 0
Set-RegValue 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location' 'Value' 'Deny' 'String'
Disable-SchTask '\Microsoft\Windows\Customer Experience Improvement Program\' 'Consolidator'
Disable-SchTask '\Microsoft\Windows\Customer Experience Improvement Program\' 'UsbCeip'
Disable-SchTask '\Microsoft\Windows\Application Experience\' 'Microsoft Compatibility Appraiser'
Disable-SchTask '\Microsoft\Windows\Application Experience\' 'ProgramDataUpdater'
Disable-SchTask '\Microsoft\Windows\Application Experience\' 'StartupAppTask'

# ---- 2. Performance ----
"`n=== [2] Performance ==="
$hp = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
$cur = (powercfg /getactivescheme)
if ($cur -match $hp) { "  [SKIP] High Performance already active" }
elseif ($Apply) { powercfg /setactive $hp | Out-Null; "  [APPLY] High Performance plan activated" }
else { "  [DRY] Power plan -> High Performance" }
Set-RegValue 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects' 'VisualFXSetting' 2
Set-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\PriorityControl' 'Win32PrioritySeparation' 24
Disable-Svc 'SysMain'
Disable-Svc 'WSearch'
Disable-Svc 'Spooler'

# ---- 3. Update policy ----
"`n=== [3] Update policy ==="
Set-RegValue 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' 'ActiveHoursStart' 6
Set-RegValue 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' 'ActiveHoursEnd' 23
Set-RegValue 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' 'NoAutoRebootWithLoggedOnUsers' 1

# ---- 4. Network ----
"`n=== [4] Network ==="
$netcat = (Get-NetConnectionProfile -InterfaceAlias Ethernet -ErrorAction SilentlyContinue).NetworkCategory
if ($netcat -eq 'Private') { "  [SKIP] Ethernet already Private" }
elseif ($Apply) { Set-NetConnectionProfile -InterfaceAlias Ethernet -NetworkCategory Private; "  [APPLY] Ethernet -> Private" }
else { "  [DRY] Ethernet $netcat -> Private" }
Set-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' 'fDenyTSConnections' 0
$rdpRules = Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
$rdpDisabled = $rdpRules | Where-Object Enabled -eq $false
if ($rdpDisabled) {
    if ($Apply) { Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'; "  [APPLY] RDP firewall rules enabled" }
    else { "  [DRY] RDP firewall rules ($($rdpDisabled.Count)) -> Enabled" }
} else { "  [SKIP] RDP firewall rules already enabled" }

# ---- 5. Bloat removal ----
"`n=== [5] Bloat removal ==="
$removeNames = @(
    'Microsoft.XboxGameOverlay','Microsoft.XboxGamingOverlay','Microsoft.XboxApp',
    'Microsoft.WindowsMaps','Microsoft.MicrosoftSolitaireCollection','Microsoft.Getstarted',
    'Microsoft.GetHelp','Microsoft.Microsoft3DViewer','Microsoft.MixedReality.Portal',
    'Microsoft.SkypeApp','Microsoft.YourPhone','Microsoft.ZuneMusic','Microsoft.ZuneVideo',
    'Microsoft.BingNews','Microsoft.BingWeather','Microsoft.MicrosoftOfficeHub',
    'Microsoft.WindowsFeedbackHub','Microsoft.People','Microsoft.Wallet',
    'MicrosoftTeams','Clipchamp.Clipchamp'
)
foreach ($n in $removeNames) { Remove-AppxFull -Name $n }
Set-RegValue 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OneDrive' 'DisableFileSyncNGSC' 1

# ---- 6. Computer name ----
"`n=== [6] Computer name ==="
$current = (Get-CimInstance Win32_ComputerSystem).Name
if (-not $ComputerName) {
    "  [SKIP] no -ComputerName provided (current: $current)"
} elseif ($current -eq $ComputerName) {
    "  [SKIP] already named $ComputerName"
} else {
    if ($Apply) {
        try { Rename-Computer -NewName $ComputerName -Force -ErrorAction Stop; "  [APPLY] $current -> $ComputerName (reboot required to take effect)" }
        catch { "  [WARN] rename failed: $_" }
    } else {
        "  [DRY] $current -> $ComputerName"
    }
}

# ---- Post-flight verify ----
"`n=== [7] Post-flight ==="
Snapshot-NetState -Tag 'post'
$ip = (Get-NetIPAddress -InterfaceAlias Ethernet -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress
$gw = (Get-NetRoute -InterfaceAlias Ethernet -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue).NextHop
"  [VERIFY] IPv4=$ip Gateway=$gw"
if (-not $ip -or -not $gw) {
    "  [ALERT] !!! NETWORK STATE LOST -- investigate via console !!!"
} else {
    "  [OK] network healthy"
}
$qgaPost = Get-Service -Name 'QEMU-GA' -ErrorAction SilentlyContinue
if ($qgaPost) { "  [QGA] post: $($qgaPost.Status) / $($qgaPost.StartType)" }

"`n#### Done ($(if ($Apply) {'APPLY'} else {'DRY-RUN'})) ####"
"Reboot recommended after applying changes (especially rename + service trim)."
Stop-Transcript | Out-Null
