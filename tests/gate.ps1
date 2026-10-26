# 智惠三合一网关 · 本地质量门禁骨架（S0）
#
# 用途：在本地（无生产凭据、禁止外网）判断"这批改动能不能进下一步"。
# 检查项（任一项失败 -> 退出码非 0）：
#   1. Python 语法编译（ast.parse，不写 .pyc，不污染业务源码）
#   2. 敏感值扫描（wbk_ / sk-qtg- / OpenAI key / 私钥块 / 明文 Bearer）
#   3. 凭据明文扫描（9 类模式 + 熵检测 + 合成分类；risk/known-s1 阻断，synthetic 允许）
#   4. 假 provider 冒烟（成功/延迟/错误码/断流/多行 SSE/取消/usage 缺失/重复/大请求）
#   5. 测试可收集（隔离运行器下 Manager 全量 collect-only，0 收集错误）
#
# 用法：
#   .\tests\gate.ps1                  # 全量门禁
#   .\tests\gate.ps1 -Quick           # 跳过 8MB 大响应/20MB 大请求两项冒烟
#   .\tests\gate.ps1 -SkipCollect     # 跳过可收集检查（仅做快速静态门禁）
#
# 注意：本脚本只读业务源码；临时目录、DB、日志全部由运行器放进 %TEMP% 下的新目录。
[CmdletBinding()]
param(
    [switch]$SkipCollect,
    [switch]$Quick,
    [string]$EvidenceDir = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

$Repo = Split-Path -Parent $PSScriptRoot
$Python = 'python'
$GateChecks = Join-Path $PSScriptRoot 'zhihui_harness\gate_checks.py'
$Runner = Join-Path $PSScriptRoot 'zhihui_harness\run_tests.py'
$Smoke = Join-Path $PSScriptRoot 'fake_provider\smoke.py'
$ScanSecrets = Join-Path $PSScriptRoot 'linux_lab\scan_secret_plaintext.py'
$Evidence = if ($EvidenceDir) { $EvidenceDir } else { Join-Path $Repo 'evidence' }
if (-not (Test-Path $Evidence)) { New-Item -ItemType Directory -Force -Path $Evidence | Out-Null }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$gateLog = Join-Path $Evidence ("gate-" + $stamp + ".log")
$summary = New-Object System.Collections.Generic.List[object]
$failed = 0

function Invoke-Step {
    param([string]$Name, [scriptblock]$Body, [string]$LogHint)
    Write-Host ""
    Write-Host "----------------------------------------------------------------------"
    Write-Host ">> $Name"
    Write-Host "----------------------------------------------------------------------"
    $started = Get-Date
    $out = & $Body 2>&1
    $code = $LASTEXITCODE
    $out | ForEach-Object { Write-Host $_ }
    $elapsed = [Math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    $ok = ($code -eq 0)
    if (-not $ok) { $script:failed++ }
    $script:summary.Add([pscustomobject]@{
            Step = $Name; ExitCode = $code; Ok = $ok; Seconds = $elapsed; Log = $LogHint
        })
    Write-Host ("<< {0}：{1}（exit={2}，{3}s）" -f $Name, ($(if ($ok) { 'PASS' } else { 'FAIL' })), $code, $elapsed)
    return $ok
}

# ---------------------------------------------------------------------------
# 0. 前置
# ---------------------------------------------------------------------------
Write-Host "======================================================================"
Write-Host " 智惠网关本地质量门禁  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host " 仓库: $Repo"
Write-Host "======================================================================"

$repoFull = (Resolve-Path $Repo).Path
foreach ($marker in @('workbuddy-manager', 'workbuddy2api', 'zhihui-prod')) {
    if ($repoFull -like "*$marker*") {
        Write-Host "[FAIL] 拒绝在疑似生产目录运行: $repoFull"
        exit 2
    }
}
try {
    $pyVersion = & $Python --version 2>&1
    Write-Host " Python: $pyVersion"
} catch {
    Write-Host "[FAIL] 找不到 python"
    exit 2
}

# ---------------------------------------------------------------------------
# 1. Python 语法编译
# ---------------------------------------------------------------------------
Invoke-Step -Name '1/5 Python 语法编译' -LogHint $gateLog -Body {
    & $Python $GateChecks --syntax
} | Out-Null

# ---------------------------------------------------------------------------
# 2. 敏感值扫描
# ---------------------------------------------------------------------------
Invoke-Step -Name '2/5 敏感值扫描' -LogHint $gateLog -Body {
    & $Python $GateChecks --secrets
} | Out-Null

# ---------------------------------------------------------------------------
# 3. 凭据明文扫描（R3-L6）：源码/配置/测试（--exclude-evidence）；只阻断 risk/known-s1，
#    合成夹具值（synthetic）不阻断，避免 CI 每天因测试数据失败。
# ---------------------------------------------------------------------------
Invoke-Step -Name '3/5 凭据明文扫描（fail-on-risk）' -LogHint $gateLog -Body {
    & $Python $ScanSecrets --fail-on-risk --exclude-evidence --out (Join-Path $Evidence ("secret-scan-" + $stamp + ".json"))
} | Out-Null

# ---------------------------------------------------------------------------
# 4. 假 provider 冒烟
# ---------------------------------------------------------------------------
$smokeArgs = @($Smoke)
if ($Quick) { $smokeArgs += '--quick' }
Invoke-Step -Name '4/5 假 provider 冒烟' -LogHint $gateLog -Body {
    & $Python @smokeArgs
} | Out-Null

# ---------------------------------------------------------------------------
# 5. 测试可收集（隔离运行器，0 收集错误）
# ---------------------------------------------------------------------------
if ($SkipCollect) {
    Write-Host ""
    Write-Host "[SKIP] 5/5 测试可收集（-SkipCollect）"
    $summary.Add([pscustomobject]@{ Step = '5/5 测试可收集'; ExitCode = -1; Ok = $true; Seconds = 0; Log = 'skipped' })
} else {
    $collectLog = Join-Path $Evidence ("gate-collect-" + $stamp + ".log")
    Invoke-Step -Name '5/5 测试可收集（Manager collect-only）' -LogHint $collectLog -Body {
        & $Python $Runner --target manager --collect-only 2>&1 | Tee-Object -FilePath $collectLog
    } | Out-Null
}

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================================================"
Write-Host " 门禁汇总"
Write-Host "======================================================================"
foreach ($row in $summary) {
    $mark = if ($row.Ok) { 'PASS' } else { 'FAIL' }
    Write-Host (" [{0}] {1,-42} exit={2,-4} {3}s" -f $mark, $row.Step, $row.ExitCode, $row.Seconds)
}
$summary | ConvertTo-Json -Depth 4 | Out-File -FilePath (Join-Path $Evidence ("gate-summary-" + $stamp + ".json")) -Encoding utf8

# 把门禁过程也留档（文本摘要）
$summary | Format-Table -AutoSize | Out-String -Width 200 |
    Out-File -FilePath $gateLog -Encoding utf8 -Append

if ($failed -gt 0) {
    Write-Host ""
    Write-Host " 门禁结果：FAIL（$failed 项失败）"
    Write-Host "======================================================================"
    exit 1
}
Write-Host ""
Write-Host " 门禁结果：PASS"
Write-Host "======================================================================"
exit 0
