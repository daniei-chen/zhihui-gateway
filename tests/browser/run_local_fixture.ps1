[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$ExpectedSource
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $repo
$lab = 'D:\AI\codex\codex1\zhihui-linux-lab'
$sshArgs = @('-F','NUL','-p','22224','-i',"$lab\lab_ed25519",'-o','BatchMode=yes',
    '-o','StrictHostKeyChecking=yes','-o',"UserKnownHostsFile=$lab\known_hosts",
    '-o','ConnectTimeout=10','-o','ServerAliveInterval=10','-o','ServerAliveCountMax=2',
    'gateway-lab@127.0.0.1')
$ready = $null
for ($attempt=0; $attempt -lt 36; $attempt++) {
    $raw = & ssh.exe @sshArgs 'cat /home/gateway-lab/lab/browser-current.json'
    if ($LASTEXITCODE -eq 0) {
        $candidate = ($raw -join "`n") | ConvertFrom-Json
        if ($candidate.source_sha256 -eq $ExpectedSource -and $candidate.production_connected -eq $false) {
            $ready = $candidate
            break
        }
    }
    Start-Sleep -Seconds 10
}
if ($null -eq $ready) { throw 'The requested frozen local browser fixture did not become ready' }
$uri = [Uri]$ready.host_url
if ($uri.Scheme -ne 'https' -or $uri.Host -ne '127.0.0.1' -or $uri.Port -ne 18443) {
    throw 'Refuse a non-local browser target'
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$fixturePath = Join-Path $repo "evidence\browser-ready-$stamp.json"
[IO.File]::WriteAllText($fixturePath,($ready | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
$results = @()
foreach ($name in @('keys_crud','viewer_permissions','page_inventory')) {
    $log = Join-Path $repo "evidence\browser-$name-$stamp.log"
    $env:BROWSER_UI_IDS = ''
    $env:BROWSER_CASE_LIMIT = '10000'
    & node "tests/browser/$name.mjs" $fixturePath > $log 2>&1
    $code = $LASTEXITCODE
    $results += [pscustomobject]@{suite=$name;exit_code=$code;log=$log}
    Write-Output "$name exit=$code log=$log"
}
$summary = [pscustomobject]@{source_sha256=$ExpectedSource;production_connected=$false;
    fixture=$fixturePath;results=$results;scope='Browser suites only; full gateway release acceptance remains separate'}
$summaryPath = Join-Path $repo "evidence\browser-batch-$stamp.json"
[IO.File]::WriteAllText($summaryPath,($summary | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
Write-Output "BROWSER_BATCH $summaryPath"
if (@($results | Where-Object exit_code -ne 0).Count -gt 0) { exit 1 }
exit 0
