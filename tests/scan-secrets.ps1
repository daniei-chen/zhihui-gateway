# 凭据明文扫描包装（R3-L6 / CI 守卫）
#
# 与 `tests/gate.ps1` 的"敏感值扫描"互补：
#   - gate_checks.py --secrets 走历史模式表（wbk_ / sk-qtg- / OpenAI key / 私钥 / Bearer / AKIA）
#   - 本包装走 scan_secret_plaintext.py：9 类模式 + 熵检测 + 合成分类，
#     `--fail-on-risk` 只在 risk/known-s1（疑似真实凭据）> 0 时失败；
#     synthetic / needs-review 不阻断（测试/证据里的合成夹具值允许存在）。
#
# 用法：
#   .\tests\scan-secrets.ps1                        # 门禁模式：扫源码/配置/测试（排除 evidence）
#   .\tests\scan-secrets.ps1 -IncludeEvidence       # 审计模式：把 evidence/ 也纳入
#   .\tests\scan-secrets.ps1 -Roots C:\tmp\dir      # 定向/负向验证（可多个，逗号分隔）
#   .\tests\scan-secrets.ps1 -Out evidence\r3-l6\secret-scan-worktree.json
#
# 退出码：0=通过；2=发现 risk/known-s1（疑似真实凭据）；其它=脚本/IO 错误。
[CmdletBinding()]
param(
    # 默认排除 evidence/：门禁只扫源码/配置/模板/测试/设计，避免实验证据里的
    # fixture 值让 CI 抖动；审计时用 -IncludeEvidence 跑全量。
    [switch]$IncludeEvidence,
    [string[]]$Roots = @(),
    [string]$Out = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }

$Repo = Split-Path -Parent $PSScriptRoot
$Scanner = Join-Path $PSScriptRoot 'linux_lab\scan_secret_plaintext.py'
if (-not (Test-Path $Scanner)) {
    Write-Host "[FAIL] 找不到扫描器: $Scanner"
    exit 2
}

$scanArgs = @('-X', 'utf8', $Scanner, '--fail-on-risk')
if (-not $IncludeEvidence) { $scanArgs += '--exclude-evidence' }
if ($Roots.Count -gt 0) { $scanArgs += @('--roots', ($Roots -join ',')) }
if ($Out) { $scanArgs += @('--out', $Out) }

Push-Location $Repo
try {
    & python @scanArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
