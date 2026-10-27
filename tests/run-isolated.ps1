# 智惠三合一网关 · 隔离测试运行器（PowerShell 入口）
#
# 审计包 T02 要求：每次运行创建全新临时目录，DB/users/config/logs/locks/临时文件
# 全部落在其中；默认禁调度/禁自愈/禁真实账号刷新/禁外网；绝不在 manager/、qtg/
# 业务源码里写入任何字节。
#
# 真正干活的是 tests/zhihui_harness/run_tests.py（Python 入口，跨平台）；
# 本脚本负责：参数校验、日志留档到 evidence/、退出码透传。
#
# 用法：
#   .\tests\run-isolated.ps1                             # Manager 全量（部署副本原样）
#   .\tests\run-isolated.ps1 -Target manager-assets      # 补 web/dev/docs 资产的诊断视图
#   .\tests\run-isolated.ps1 -Target qtg                 # QTG 控制面
#   .\tests\run-isolated.ps1 -Target all                 # 全部（manager + qtg）
#   .\tests\run-isolated.ps1 -CollectOnly                # 只验证可收集
#   .\tests\run-isolated.ps1 -Keep                       # 保留临时根用于诊断
#   .\tests\run-isolated.ps1 -PytestArg '-x','-k','gateway'
#
[CmdletBinding()]
param(
    [ValidateSet('manager', 'manager-assets', 'src', 'qtg', 'all')]
    [string]$Target = 'manager',

    [switch]$Keep,

    [ValidateSet('block', 'warn', 'off')]
    [string]$NetPolicy = 'block',

    [switch]$CollectOnly,

    [string]$BaseDir = '',

    [string[]]$PytestArg = @(),

    [string]$FakeMode = 'normal'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

$Repo = Split-Path -Parent $PSScriptRoot
$Python = 'python'
$Runner = Join-Path $PSScriptRoot 'zhihui_harness\run_tests.py'
$Evidence = Join-Path $Repo 'evidence'

# ---------------------------------------------------------------------------
# 0. 前置安全校验：只在执行树里跑，拒绝生产路径
# ---------------------------------------------------------------------------
if (-not (Test-Path $Runner)) {
    Write-Error "找不到运行器: $Runner"
    exit 2
}
$repoFull = (Resolve-Path $Repo).Path
foreach ($marker in @('workbuddy-manager', 'workbuddy2api', 'zhihui-prod')) {
    if ($repoFull -like "*$marker*") {
        Write-Error "拒绝在疑似生产目录运行: $repoFull"
        exit 2
    }
}
# 残留的 WB_* 环境变量若指向 /opt 或真实路径，必须先明确报错而不是静默继承
$dangerous = @('WB_DATA_DIR', 'WB_DB', 'WB_USERS_FILE', 'WB_AUTH_DIR',
               'WB_UPSTREAM_CONFIG', 'WB_UPSTREAM_DIR', 'WB_STATIC_DIR')
$bad = @()
foreach ($name in $dangerous) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value -and ($value -match '^/opt/' -or $value -match '^[A-Za-z]:[\\/]opt[\\/]')) {
        $bad += "$name=$value"
    }
}
if ($bad.Count -gt 0) {
    Write-Error ("当前 shell 里有指向 /opt 的 WB_* 变量，运行器会覆盖它们；" +
                 "为免误写请先清理: " + ($bad -join '; '))
    exit 2
}

if (-not (Test-Path $Evidence)) {
    New-Item -ItemType Directory -Force -Path $Evidence | Out-Null
}

# ---------------------------------------------------------------------------
# 1. 组装参数
# ---------------------------------------------------------------------------
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$safeTarget = ($Target -replace '[^A-Za-z0-9_-]', '_')
$logPath = Join-Path $Evidence ("run-" + $safeTarget + "-" + $stamp + ".log")

$args = @($Runner, '--target', $Target, '--net-policy', $NetPolicy, '--fake-mode', $FakeMode)
if ($Keep) { $args += '--keep' }
if ($CollectOnly) { $args += '--collect-only' }
if ($BaseDir) { $args += @('--base-dir', $BaseDir) }
foreach ($pa in $PytestArg) {
    # 必须用 --opt=value 形式，否则以 - 开头的值（如 -k）会被 argparse 当成选项
    if ($pa) { $args += "--pytest-arg=$pa" }
}

Write-Host "======================================================================"
Write-Host " 智惠网关隔离测试  target=$Target  net=$NetPolicy  collectOnly=$CollectOnly"
Write-Host " 仓库: $repoFull"
Write-Host " 日志: $logPath"
Write-Host "======================================================================"

# ---------------------------------------------------------------------------
# 2. 执行并留档（tee 到 evidence）
# ---------------------------------------------------------------------------
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$started = Get-Date
# -X utf8 必须显式传：Windows 下 subprocess 的 text 模式默认按 GBK 解码子进程输出，
# 会让依赖中文输出的测试误报（详见 tests/README.md「已知限制」）。
& $Python -X utf8 @args 2>&1 | Tee-Object -FilePath $logPath
$code = $LASTEXITCODE
$elapsed = [Math]::Round(((Get-Date) - $started).TotalSeconds, 1)
"# EXIT_CODE=$code  ELAPSED_S=$elapsed  TARGET=$Target" |
    Add-Content -Path $logPath -Encoding utf8

Write-Host ""
Write-Host "======================================================================"
Write-Host " 退出码: $code   用时: ${elapsed}s"
Write-Host " 完整日志: $logPath"
Write-Host "======================================================================"
exit $code
