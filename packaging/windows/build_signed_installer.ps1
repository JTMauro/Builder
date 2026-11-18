<#
  Builder: build, self-test, sign, package and verify on the Cydcor build PC.
  Started by "BUILD BUILDER.cmd" in the repository root. Follows the Cydcor build standard
  (OneDrive SHARED/CODE/CertsandSisgns/SKILL.md), Path B: everything on the build PC.

  Output: packaging\windows\installer\Output\
    Builder-Setup-<ver>-PerUser.exe   Builder-Setup-<ver>.exe   Builder-<ver>-Portable.zip
    Builder-<ver>-release.zip   SHA256SUMS.txt   SIGNATURES.txt   DEPLOY.md   Builder-publisher.cer

  Options:
    -ListCertificates        show code-signing certificates on this PC and stop
    -TimestampUrl <url>      use another timestamp server (if IT blocks DigiCert's)
    -SignOne <file>          sign one file with the Cydcor certificate and stop
    -SkipUpdateCheck         don't run winget/pip upgrades first (offline builds)
    -Unsigned                build and package without signing (for testing on a PC without the certificate)
#>
[CmdletBinding()]
param(
    [switch]$ListCertificates,
    [string]$TimestampUrl = "",
    [string]$SignOne = "",
    [switch]$SkipUpdateCheck,
    [switch]$Unsigned,
    [ValidateSet("all", "sign")][string]$Phase = "all"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path        # packaging\windows
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path         # repository root
$Dist = Join-Path $Here "dist\Builder"
$Out = Join-Path $Here "installer\Output"
$Cache = Join-Path $Here "cache"
$Versions = [ordered]@{}

function Say($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  !!  $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host "`n  FAILED: $text" -ForegroundColor Red
    Write-Host "  See INSTRUCTIONS.txt (Common failures) in packaging\windows."
    Read-Host "Press Enter to close" | Out-Null
    exit 1
}
function Run($exe, [string[]]$argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { Fail "$exe $($argList -join ' ') exited with $LASTEXITCODE" }
}

# ------------------------------------------------------------ version
$init = Get-Content (Join-Path $Root "media_backend\__init__.py") -Raw
if ($init -notmatch '__version__\s*=\s*"([0-9.]+)"') { Fail "no __version__ in media_backend\__init__.py" }
$Version = $Matches[1]
$vi = Get-Content (Join-Path $Here "installer\version_info.txt") -Raw
if ($vi -notmatch "StringStruct\('ProductVersion', '$([regex]::Escape($Version))'\)") {
    Fail "installer\version_info.txt doesn't say $Version. Update both version lines to match media_backend\__init__.py."
}

# ------------------------------------------------------------ 1. signing settings and certificate
$signing = Get-Content (Join-Path $Here "signing.json") -Raw | ConvertFrom-Json
if (-not $TimestampUrl) { $TimestampUrl = $signing.timestamp_url }

function Find-Certificate {
    $all = @(Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert -ErrorAction SilentlyContinue |
             Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) })
    $c = $all | Where-Object { $_.Thumbprint -eq $signing.local.thumbprint } | Select-Object -First 1
    if (-not $c) {
        $c = $all | Where-Object { $_.Subject -like "*$($signing.local.subject_fallback)*" } |
             Sort-Object NotAfter -Descending | Select-Object -First 1
        if ($c) { Warn "Thumbprint $($signing.local.thumbprint) not found; using the newest Cydcor certificate instead." }
    }
    return $c
}

if ($ListCertificates) {
    Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert |
        Select-Object Subject, Thumbprint, NotAfter, HasPrivateKey, PSParentPath | Format-List
    exit 0
}

function Find-SignTool {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

$Cert = $null
$SignTool = $null
$MachineStore = $false
if (-not $Unsigned) {
    $Cert = Find-Certificate
    if (-not $Cert) { Fail "No Cydcor code-signing certificate on this PC. Plug in the token, or run with -ListCertificates." }
    $MachineStore = $Cert.PSParentPath -like "*LocalMachine*"
    Write-Host "Signing mode: $($signing.mode)"
    Write-Host "Using: $($Cert.Subject)  [$($Cert.Thumbprint)]  expires $($Cert.NotAfter.ToString('yyyy-MM-dd'))"
    $days = ($Cert.NotAfter - (Get-Date)).Days
    if ($days -lt 90) { Warn "The certificate expires in $days days. Ask IT to renew it." }
}

function Sign-Files([string[]]$files) {
    if ($Unsigned -or $files.Count -eq 0) { return }
    if (-not $SignTool) { $script:SignTool = Find-SignTool }
    if (-not $SignTool) { Fail "signtool.exe not found (Windows SDK signing tools)." }
    $base = @("sign", "/sha1", $Cert.Thumbprint, "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256", "/d", "Builder")
    if ($MachineStore) { $base += "/sm" }
    for ($i = 0; $i -lt $files.Count; $i += 40) {
        $chunk = $files[$i..([Math]::Min($i + 39, $files.Count - 1))]
        & $SignTool @base @chunk | Out-Host
        if ($LASTEXITCODE -ne 0) { Fail "signtool could not use the certificate (UAC declined, token unplugged, or timestamp unreachable: try -TimestampUrl)." }
    }
}

if ($SignOne) {
    Sign-Files @((Resolve-Path $SignOne).Path)
    & (Find-SignTool) verify /pa /v $SignOne
    exit $LASTEXITCODE
}

if ($Phase -eq "all") {
    # ------------------------------------------------------------ 0 + 2. update check and tools
    Say "Update check and tools"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    $tools = @(
        @{ id = "Python.Python.3.13"; test = { Get-Command py -ErrorAction SilentlyContinue } },
        @{ id = "JRSoftware.InnoSetup"; test = { Test-Path "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" } },
        @{ id = "Microsoft.WindowsSDK.10.0.26100"; test = { Find-SignTool } }
    )
    foreach ($t in $tools) {
        if (-not (& $t.test)) {
            if (-not $winget) { Fail "$($t.id) is missing and winget isn't available to install it." }
            Write-Host "Installing $($t.id) ..."
            winget install --id $t.id -e --accept-package-agreements --accept-source-agreements --silent | Out-Host
        } elseif ($winget -and -not $SkipUpdateCheck) {
            winget upgrade --id $t.id -e --accept-package-agreements --accept-source-agreements --silent | Out-Null
        }
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $ISCC = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $ISCC)) { $ISCC = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" }
    if (-not (Test-Path $ISCC)) { Fail "Inno Setup 6 (ISCC.exe) not found after installing it. Close this window and run BUILD BUILDER.cmd again." }

    # ------------------------------------------------------------ 3. clean
    Say "Clean"
    foreach ($d in @((Join-Path $Here "build"), (Join-Path $Here "dist"), $Out)) {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
    New-Item -ItemType Directory -Force -Path $Out, $Cache | Out-Null

    # ------------------------------------------------------------ 4. compile
    Say "Compile (Python 3.13 + PyInstaller)"
    $venv = Join-Path $Here ".venv"
    if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { Run py @("-3.13", "-m", "venv", $venv) }
    $py = Join-Path $venv "Scripts\python.exe"
    Run $py @("-m", "pip", "install", "-q", "-U", "pip")
    Run $py @("-m", "pip", "install", "-q", "-U", $Root, "-r", (Join-Path $Here "requirements-windows.txt"))
    if (-not $SkipUpdateCheck) {
        Write-Host "Outdated packages (updated on the next build unless pinned):"
        & $py -m pip list --outdated --disable-pip-version-check | Out-Host
    }
    $Versions["Python"] = (& $py --version) -replace "Python ", ""
    $Versions["PyInstaller"] = (& $py -m PyInstaller --version)
    $Versions["Inno Setup"] = (Get-Item $ISCC).VersionInfo.ProductVersion
    Push-Location $Here
    try { Run $py @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", "dist", "--workpath", "build", "builder.spec") }
    finally { Pop-Location }
    foreach ($exe in @("Builder.exe", "builder-cli.exe")) {
        if (-not (Test-Path (Join-Path $Dist $exe))) { Fail "PyInstaller didn't produce $exe" }
    }

    # ------------------------------------------------------------ 5. bundle ffmpeg and the Craft engine
    Say "Bundle ffmpeg and the Craft engine"
    $ffZip = Join-Path $Cache "ffmpeg-release-essentials.zip"
    $ffUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $expected = (Invoke-WebRequest "$ffUrl.sha256" -UseBasicParsing).Content.Trim().Split(" ")[0].ToLower()
    $have = ""
    if (Test-Path $ffZip) { $have = (Get-FileHash $ffZip -Algorithm SHA256).Hash.ToLower() }
    if ($have -ne $expected) {
        Write-Host "Downloading ffmpeg (gyan.dev release essentials) ..."
        Invoke-WebRequest $ffUrl -OutFile $ffZip -UseBasicParsing
        $have = (Get-FileHash $ffZip -Algorithm SHA256).Hash.ToLower()
        if ($have -ne $expected) { Fail "ffmpeg download checksum mismatch ($have vs $expected)" }
    }
    $ffTmp = Join-Path $Cache "ffmpeg"
    if (Test-Path $ffTmp) { Remove-Item $ffTmp -Recurse -Force }
    Expand-Archive $ffZip $ffTmp
    $ffBin = Get-ChildItem $ffTmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    $ffDest = Join-Path $Dist "ffmpeg"
    New-Item -ItemType Directory -Force -Path $ffDest | Out-Null
    Copy-Item (Join-Path $ffBin.DirectoryName "ffmpeg.exe"), (Join-Path $ffBin.DirectoryName "ffprobe.exe") $ffDest
    Get-ChildItem $ffTmp -Recurse -Include LICENSE*, README* | Select-Object -First 2 | Copy-Item -Destination $ffDest
    $Versions["ffmpeg"] = ((& (Join-Path $ffDest "ffmpeg.exe") -version) | Select-Object -First 1) -replace "ffmpeg version ", "" -replace " Copyright.*", ""

    $engDest = Join-Path $Dist "engine"
    New-Item -ItemType Directory -Force -Path $engDest | Out-Null
    $prebuilt = Join-Path $Here "prebuilt\craft-anything-cli.exe"
    $cargo = Get-Command cargo -ErrorAction SilentlyContinue
    if (Test-Path $prebuilt) {
        # Built from this same engine\ source in the Builder chat (Windows target); signed below with everything else.
        Write-Host "Using the prebuilt Craft engine (packaging\windows\prebuilt)."
        Copy-Item $prebuilt $engDest
    } elseif ($cargo) {
        Write-Host "Building the Craft engine with Rust (cargo) ..."
        Push-Location (Join-Path $Root "engine")
        try { Run cargo @("build", "-p", "craft-anything-cli", "--release") } finally { Pop-Location }
        Copy-Item (Join-Path $Root "engine\target\release\craft-anything-cli.exe") $engDest
        $Versions["Rust"] = (& rustc --version)
    } else {
        Fail "No Craft engine: put craft-anything-cli.exe in packaging\windows\prebuilt, or install Rust (winget install Rustlang.Rustup)."
    }
    $Versions["Craft engine"] = (& (Join-Path $engDest "craft-anything-cli.exe") --version)

    # ------------------------------------------------------------ 6. self-test
    Say "Self-test"
    # Windows PowerShell turns redirected stderr lines into errors, so read them as plain text here.
    $ErrorActionPreference = "Continue"
    $selftest = & (Join-Path $Dist "builder-cli.exe") app --selftest 2>&1 | ForEach-Object { "$_" }
    $ErrorActionPreference = "Stop"
    $selftest | Out-Host
    if (($selftest -join "`n") -notmatch "SELFTEST PASSED") { Fail "SELFTEST FAILED" }

    # ------------------------------------------------------------ 7. elevate for a LocalMachine certificate
    if ($MachineStore -and -not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Say "The certificate is in the machine store: signing in an Administrator window (one UAC prompt)"
        $Versions | ConvertTo-Json | Set-Content (Join-Path $Here "build\versions.json") -Encoding UTF8
        $argsList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$($MyInvocation.MyCommand.Path)`"", "-Phase", "sign")
        if ($TimestampUrl) { $argsList += @("-TimestampUrl", $TimestampUrl) }
        $p = Start-Process powershell -Verb RunAs -ArgumentList $argsList -Wait -PassThru
        exit $p.ExitCode
    }
} else {
    $vf = Join-Path $Here "build\versions.json"
    if (Test-Path $vf) { (Get-Content $vf -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $Versions[$_.Name] = $_.Value } }
    $ISCC = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $ISCC)) { $ISCC = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" }
}

# ------------------------------------------------------------ 8. sign the app files
Say "Sign"
$toSign = Get-ChildItem $Dist -Recurse -Include *.exe, *.dll, *.pyd | Where-Object {
    (Get-AuthenticodeSignature $_.FullName).Status -ne "Valid"     # skip files Microsoft or a vendor already signed
} | ForEach-Object { $_.FullName }
Write-Host "$($toSign.Count) files to sign (already-signed vendor files are left as they are)"
Sign-Files @($toSign)

# ------------------------------------------------------------ 9. package
Say "Package"
$iss = Join-Path $Here "installer\Builder.iss"
$common = @("/Q", "/DAppVersion=$Version", "/DSourceDir=$Dist", "/DOutDir=$Out")
if (-not $Unsigned) {
    $st = Find-SignTool
    $sm = ""
    if ($MachineStore) { $sm = " /sm" }
    # Inno replaces $q with a quote and $f with the quoted file name (signtool's path has spaces).
    $common += @("/DSign", "/Ssigntool=`$q$st`$q sign /sha1 $($Cert.Thumbprint)$sm /fd SHA256 /tr $TimestampUrl /td SHA256 /d Builder `$f")
}
Run $ISCC (@("/DPerUser") + $common + @($iss))
Run $ISCC ($common + @($iss))
$portable = Join-Path $Out "Builder-$Version-Portable.zip"
Compress-Archive -Path $Dist -DestinationPath $portable -Force

# ------------------------------------------------------------ 10. verify
Say "Verify"
$check = @(Get-ChildItem $Out -Filter *.exe) + @(Get-Item (Join-Path $Dist "Builder.exe"), (Join-Path $Dist "builder-cli.exe"),
          (Join-Path $Dist "engine\craft-anything-cli.exe"), (Join-Path $Dist "ffmpeg\ffmpeg.exe"))
$lines = @("Builder $Version signatures, $(Get-Date -Format 'yyyy-MM-dd HH:mm')", "")
foreach ($k in $Versions.Keys) { $lines += ("Built with {0}: {1}" -f $k, $Versions[$k]) }
$lines += ""
foreach ($f in $check) {
    $sig = Get-AuthenticodeSignature $f.FullName
    $ts = [bool]$sig.TimeStamperCertificate
    $state = if ($Unsigned) { "UNSIGNED (test build)" } else { "$($sig.Status) timestamped: $ts  $($sig.SignerCertificate.Subject)" }
    $lines += ("{0,-40} {1}" -f $f.Name, $state)
    if (-not $Unsigned) {
        & (Find-SignTool) verify /pa /q $f.FullName | Out-Null
        if ($LASTEXITCODE -ne 0 -or -not $ts) { Fail "$($f.Name) didn't verify (signtool verify /pa)" }
        Ok "$($f.Name) Valid, timestamped"
    }
}
$lines | Set-Content (Join-Path $Out "SIGNATURES.txt") -Encoding UTF8

# ------------------------------------------------------------ 11. release files
Say "Release files"
(Get-Content (Join-Path $Here "installer\DEPLOY.md") -Raw).Replace("{VERSION}", $Version) |
    Set-Content (Join-Path $Out "DEPLOY.md") -Encoding UTF8
if ($Cert) { Export-Certificate -Cert $Cert -FilePath (Join-Path $Out "Builder-publisher.cer") -Type CERT | Out-Null }
$sums = Get-ChildItem $Out -File | Where-Object { $_.Name -notin @("SHA256SUMS.txt") } | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Name
}
$sums | Set-Content (Join-Path $Out "SHA256SUMS.txt") -Encoding ASCII
$release = Join-Path $Out "Builder-$Version-release.zip"
$inRelease = Get-ChildItem $Out -File | Where-Object { $_.Name -like "*.exe" -or $_.Name -in @("SHA256SUMS.txt", "DEPLOY.md", "Builder-publisher.cer", "SIGNATURES.txt") }
Compress-Archive -Path ($inRelease | ForEach-Object { $_.FullName }) -DestinationPath $release -Force

# ------------------------------------------------------------ 12. summary
Say "Done"
if ($Unsigned) { Write-Host "Publisher : (unsigned test build)" -ForegroundColor Yellow }
else { Write-Host "Publisher : Cydcor LLC (signed with: $($signing.mode))" -ForegroundColor Green }
Write-Host "Version   : $Version"
Get-ChildItem $Out -File | ForEach-Object { Write-Host ("  {0,-44} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB)) }
Start-Process explorer.exe $Out
Read-Host "Press Enter to close" | Out-Null
