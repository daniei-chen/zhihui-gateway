[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskLab = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..\zhihui-linux-lab'))
$taskConfig = Get-Content -LiteralPath (Join-Path $taskLab 'lab-config.json') -Raw | ConvertFrom-Json
$taskStateFile = Join-Path $taskLab 'processes.json'
$taskState = @{}
if (Test-Path -LiteralPath $taskStateFile) {
    $taskSaved = Get-Content -LiteralPath $taskStateFile -Raw | ConvertFrom-Json
    foreach ($taskProperty in $taskSaved.PSObject.Properties) { $taskState[$taskProperty.Name] = $taskProperty.Value }
}
$taskExistingVm = Get-CimInstance Win32_Process -Filter "Name='qemu-system-x86_64.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($taskConfig.disk) }
if ($taskExistingVm) {
    if (@($taskExistingVm).Count -ne 1) { throw 'Multiple matching VMs; refusing another launch' }
    $taskState.qemu_pid = $taskExistingVm.ProcessId
    $taskState | ConvertTo-Json | Set-Content -LiteralPath $taskStateFile -Encoding utf8
    Write-Output 'Verified existing local VM; no duplicate launched.'
    exit 0
}
$taskListeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
foreach ($taskPort in @(22224, 18086, 18443, 18446)) {
    if ($taskListeners.Port -contains $taskPort) { throw "Port $taskPort is occupied; refusing to replace its owner" }
}
$taskMetadataProcess = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($taskConfig.seed_directory) -and $_.CommandLine.Contains('http.server') }
if (-not $taskMetadataProcess) {
    if ($taskListeners.Port -contains 18181) { throw 'Metadata port occupied by another process' }
    $taskPython = (Get-Command python.exe).Source
    $taskMetadataProcess = Start-Process -FilePath $taskPython -ArgumentList @('-m', 'http.server', '18181', '--bind', '127.0.0.1', '--directory', $taskConfig.seed_directory) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLab 'metadata.stdout.log') -RedirectStandardError (Join-Path $taskLab 'metadata.stderr.log')
}
$taskState.metadata_pid = $taskMetadataProcess.ProcessId
if (-not $taskState.metadata_pid) { $taskState.metadata_pid = $taskMetadataProcess.Id }
$taskState | ConvertTo-Json | Set-Content -LiteralPath $taskStateFile -Encoding utf8
$taskVmProcess = Start-Process -FilePath $taskConfig.qemu -ArgumentList $taskConfig.arguments -WindowStyle Hidden -PassThru -WorkingDirectory (Split-Path $taskConfig.qemu -Parent) -RedirectStandardOutput (Join-Path $taskLab 'qemu.stdout.log') -RedirectStandardError (Join-Path $taskLab 'qemu.stderr.log')
$taskState.qemu_pid = $taskVmProcess.Id
$taskState.started_at = [DateTimeOffset]::Now.ToString('o')
$taskState | ConvertTo-Json | Set-Content -LiteralPath $taskStateFile -Encoding utf8
Start-Sleep -Milliseconds 500
$taskVmProcess.Refresh()
if ($taskVmProcess.HasExited) { Get-Content -LiteralPath (Join-Path $taskLab 'qemu.stderr.log'); throw 'Local VM exited during startup' }
Write-Output ($taskState | ConvertTo-Json -Compress)
