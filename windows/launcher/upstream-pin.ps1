# 上游源码包的来源校验（唯一真源：dev/upstream-source.lock）
#
# 为什么单独成一个文件：`build.ps1` 里原本自己写了一份摘要常量，而
# `.github/workflows/release.yml` 用的是 `dev/upstream-source.lock` —— 也就是
# **同一个东西有两份 pin**。2026-11-28 就是这么坏的：第三方载体上的同名文件被
# 换成另一份字节（992,664 B / sha256:72b9d63c…），`build.ps1` 里那份手写摘要立刻
# 变成假值，Windows 安装器的构建按设计被拒绝（拒绝是对的，错的是有第二份 pin）。
#
# 现在这里只做一件事：**只认 lock**，摘要与字节数两条都校验，缺一不可；
# lock 缺失或格式不对一律 fail closed —— 绝不允许「没有校验直接建」。
#
# 用法（build.ps1 里）：
#   . (Join-Path $scriptDir 'upstream-pin.ps1')
#   $pin = Read-UpstreamSrcPin -LockPath $upstreamSrcLock
#   Assert-UpstreamSrcArchive -Path $tarPath -Pin $pin

function Read-UpstreamSrcPin {
    param([Parameter(Mandatory = $true)][string]$LockPath)

    if (-not (Test-Path -LiteralPath $LockPath)) {
        throw ("上游源码锁文件不存在，拒绝构建：$LockPath`n" +
               '  锁文件是上游源码的唯一真源（sha256 + bytes）。缺了它，就没有任何东西能' +
               '证明要编译进安装包的源码来自哪里。不要跳过这一步。')
    }

    # 解析口径必须**与 `.github/workflows/release.yml` 的 bash 逐条一致**：
    #   bash 侧是 `sed -n 's/^sha256=//p'` + `grep -qE '^[0-9a-f]{64}$'` +
    #   `grep -qE '^[1-9][0-9]*$'`（bytes）。所以这里也：行首不许有空白、键名小写、
    #   等号两侧不许有空格、sha256 只收小写 hex、bytes 不许前导零、重复键直接拒。
    # 以前这里比 bash 宽松（会 trim、收大写 hex、收 `bytes=007435373`、重复键
    # last-wins），于是同一份 lock 能让 Linux 红、Windows 绿 —— 而「两边对同一份
    # pin 的判断不一致」正是 2026-11-28 那次发版失败的地方。
    $values = @{}
    $lineNo = 0
    foreach ($line in Get-Content -LiteralPath $LockPath) {
        $lineNo++
        if ($line -eq '' -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^([a-z0-9_.-]+)=(.*)$') {
            throw ("上游源码锁文件第 $lineNo 行格式不合法，拒绝构建：$LockPath`n" +
                   "  读到：'$line'`n" +
                   '  合法形式：key=value，行首与等号两侧都不许有空白。')
        }
        $key = $Matches[1]
        $value = $Matches[2]
        if ($values.ContainsKey($key)) {
            throw ("上游源码锁文件第 $lineNo 行重复了键 '$key'，拒绝构建：$LockPath`n" +
                   '  重复键会让读的一方拿到多行值（release.yml 因此直接失败），这里同样拒绝。')
        }
        $values[$key] = $value
    }

    $sha = [string]$values['sha256']
    $bytes = [string]$values['bytes']
    if ($sha -cnotmatch '^[0-9a-f]{64}$') {
        throw ("上游源码锁文件缺少合法 sha256，拒绝构建：$LockPath（读到 '$sha'）`n" +
               '  合法形式：sha256=<64 位小写十六进制>，与 release.yml 的 ^[0-9a-f]{64}$ 同口径')
    }
    if ($bytes -cnotmatch '^[1-9][0-9]*$') {
        throw ("上游源码锁文件缺少合法 bytes，拒绝构建：$LockPath（读到 '$bytes'）`n" +
               '  合法形式：bytes=<十进制字节数，不许前导零>，与 release.yml 的 ^[1-9][0-9]*$ 同口径')
    }

    return [pscustomobject]@{
        Sha256 = $sha
        Bytes  = [int64]$bytes
        Path   = $LockPath
    }
}

function Assert-UpstreamSrcArchive {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Pin
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "上游源码包不存在，拒绝构建：$Path"
    }

    $actualBytes = (Get-Item -LiteralPath $Path).Length
    $actualSha = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()

    if ($actualBytes -ne $Pin.Bytes) {
        throw ("上游源码大小不匹配，已拒绝构建：期望 $($Pin.Bytes) 字节，实际 $actualBytes 字节。`n" +
               "  锁文件：$($Pin.Path)`n" +
               '  载体上的源码包被替换过，或锁文件没跟着更新。在解包与编译之前停下，' +
               '避免把来源不明的源码打进安装包。')
    }
    if ($actualSha -ne $Pin.Sha256) {
        throw ("上游源码摘要不匹配，已拒绝构建：期望 $($Pin.Sha256)，实际 $actualSha。`n" +
               "  锁文件：$($Pin.Path)`n" +
               '  载体上的源码包被替换过，或锁文件没跟着更新。在解包与编译之前停下，' +
               '避免把来源不明的源码打进安装包。')
    }
    return $true
}
