# Test-owned R02 semantic probe. Evidence directories are retained until explicit cleanup.
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{10}$')]
    [string]$Nonce
)
$ErrorActionPreference = 'Continue'
$nonce = $Nonce
if ([string]::IsNullOrWhiteSpace($env:TEMP)) { throw 'TEMP_UNAVAILABLE' }
$tempRoot = [System.IO.Path]::GetFullPath($env:TEMP)
$tempAttrs = [System.IO.File]::GetAttributes($tempRoot)
if (($tempAttrs -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'TEMP_REPARSE_POINT' }
$root = Join-Path $tempRoot ('jtt-r02-semantic-' + $nonce + '-B1')
if (Test-Path -LiteralPath $root) { throw 'TEST_ROOT_ALREADY_EXISTS' }
[void][System.IO.Directory]::CreateDirectory($root)
$ownershipMarker = Join-Path $root ('.jtt-owned-' + $nonce + '.marker')
[System.IO.File]::WriteAllText($ownershipMarker, 'JTT_R02_TEST_OWNED_' + $nonce)
$work = Join-Path $root 'work'
$destination = Join-Path $work 'destination'
$destinationName = [System.IO.DirectoryInfo]::new($destination).Name
if ([string]::IsNullOrWhiteSpace($destinationName) -or $destinationName.Contains("'") -or $destinationName.Contains('\') -or $destinationName.Contains('/')) { throw 'DESTINATION_NAME_NOT_A_SAFE_RELATIVE_SEGMENT' }
[void][System.IO.Directory]::CreateDirectory($work)
[void][System.IO.Directory]::CreateDirectory($destination)
foreach ($ownedPath in @($root, $work, $destination)) {
    $ownedAttrs = [System.IO.File]::GetAttributes($ownedPath)
    if (($ownedAttrs -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'TEST_OWNED_PATH_REPARSE_POINT' }
}
$expected = [System.IO.Path]::GetFullPath($destination)
$relativeLocationCommand = "Set-Location '$destinationName' -ErrorAction Stop"
$relativeCommandBytes = [System.Text.Encoding]::UTF8.GetBytes($relativeLocationCommand)
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try { $relativeCommandSha256 = [System.BitConverter]::ToString($sha256.ComputeHash($relativeCommandBytes)).Replace('-', '').ToLowerInvariant() } finally { $sha256.Dispose() }
$sideEffect = Join-Path $destination ('b1-side-effect-' + $nonce + '.marker')
$outputMarker = 'JTT_B1_PATH_OUTPUT_' + $nonce + '_END'
$exitCode = 0
$errorInfo = $null
$actual = $null
$phase = 'SET_WORKING_DIRECTORY'
try {
    Set-Location -LiteralPath $work
    $phase = 'SET_LOCATION'
    Invoke-Expression $relativeLocationCommand
    $phase = 'VERIFY_LOCATION'
    $actual = [System.IO.Path]::GetFullPath($PWD.Path)
    if (-not [System.String]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'JTT_EXPECTED_LOCATION_MISMATCH'
    }
    $phase = 'SIDE_EFFECT_WRITE'
    [System.IO.File]::WriteAllText($sideEffect, $actual)
    $phase = 'OUTPUT_CALL'
    [Console]::WriteLine($outputMarker)
} catch {
    $exitCode = 1
    $errorInfo = [ordered]@{
        type = $_.Exception.GetType().FullName
        fullyQualifiedErrorId = $_.FullyQualifiedErrorId
        failurePhase = $phase
    }
    Write-Error -ErrorRecord $_
}
$effectObserved = [System.IO.File]::Exists($sideEffect)
$effectContent = if ($effectObserved) { [System.IO.File]::ReadAllText($sideEffect) } else { $null }
$effectMatches = $null -ne $effectContent -and [System.String]::Equals($effectContent, $expected, [System.StringComparison]::OrdinalIgnoreCase)
$summary = [ordered]@{
    scenario = 'B1_MINIMAL_PATH_CHANGE'
    nonce = $nonce
    root = $root
    workingDirectory = $work
    destination = $expected
    destinationName = $destinationName
    relativeLocationCommand = $relativeLocationCommand
    relativeCommandSha256 = $relativeCommandSha256
    relativeCommandUtf8Bytes = $relativeCommandBytes.Length
    expectedPath = $expected
    actualPath = $actual
    failurePhase = if ($null -ne $errorInfo) { $errorInfo.failurePhase } else { $null }
    expectedLocationVerified = ($null -ne $actual -and [System.String]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase))
    sideEffectObserved = $effectObserved
    sideEffectPath = $sideEffect
    sideEffectContentMatchesExpectedPath = $effectMatches
    outputMarker = $outputMarker
    checkpointMarkers = 'NOT_INSTRUMENTED_MINIMAL_CONTROL'
    error = $errorInfo
    exitCode = $exitCode
}
[Console]::Out.WriteLine(($summary | ConvertTo-Json -Compress -Depth 4))
exit $exitCode
