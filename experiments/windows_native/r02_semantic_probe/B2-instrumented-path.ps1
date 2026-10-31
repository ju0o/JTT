# Test-owned R02 semantic probe. Evidence directories are retained until explicit cleanup.
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{10}$')]
    [string]$Nonce
)
$ErrorActionPreference = 'Continue'
$nonce = $Nonce
$label = 'SCRIPT_PATH_CHANGE'
if ([string]::IsNullOrWhiteSpace($env:TEMP)) { throw 'TEMP_UNAVAILABLE' }
$tempRoot = [System.IO.Path]::GetFullPath($env:TEMP)
$tempAttrs = [System.IO.File]::GetAttributes($tempRoot)
if (($tempAttrs -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'TEMP_REPARSE_POINT' }
$root = Join-Path $tempRoot ('jtt-r02-semantic-' + $nonce + '-B2')
if (Test-Path -LiteralPath $root) { throw 'TEST_ROOT_ALREADY_EXISTS' }
[void][System.IO.Directory]::CreateDirectory($root)
$ownershipMarker = Join-Path $root ('.jtt-owned-' + $nonce + '.marker')
[System.IO.File]::WriteAllText($ownershipMarker, 'JTT_R02_TEST_OWNED_' + $nonce)
$work = Join-Path $root 'work'
$destination = Join-Path $work 'destination'
$destinationName = [System.IO.DirectoryInfo]::new($destination).Name
if ([string]::IsNullOrWhiteSpace($destinationName) -or $destinationName.Contains("'") -or $destinationName.Contains('\') -or $destinationName.Contains('/')) { throw 'DESTINATION_NAME_NOT_A_SAFE_RELATIVE_SEGMENT' }
$checkpointRoot = Join-Path $work '.jtt-r02-checkpoints'
[void][System.IO.Directory]::CreateDirectory($work)
[void][System.IO.Directory]::CreateDirectory($destination)
[void][System.IO.Directory]::CreateDirectory($checkpointRoot)
foreach ($ownedPath in @($root, $work, $destination, $checkpointRoot)) {
    $ownedAttrs = [System.IO.File]::GetAttributes($ownedPath)
    if (($ownedAttrs -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'TEST_OWNED_PATH_REPARSE_POINT' }
}
$expected = [System.IO.Path]::GetFullPath($destination)
$scriptPath = Join-Path $work ('path-change-' + $nonce + '.ps1')
$sideEffect = Join-Path $destination ('script-path-' + $nonce + '.marker')
$relativeLocationCommand = "Set-Location '$destinationName' -ErrorAction Stop"
$relativeCommandBytes = [System.Text.Encoding]::UTF8.GetBytes($relativeLocationCommand)
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try { $relativeCommandSha256 = [System.BitConverter]::ToString($sha256.ComputeHash($relativeCommandBytes)).Replace('-', '').ToLowerInvariant() } finally { $sha256.Dispose() }
$stdoutPath = Join-Path $root 'child.stdout.txt'
$stderrPath = Join-Path $root 'child.stderr.txt'
$summaryPath = Join-Path $root 'result.json'
$scriptBodyTemplate = @'
$ErrorActionPreference = 'Continue'
$nonce = '__NONCE__'
$label = 'SCRIPT_PATH_CHANGE'
$expected = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '__DESTINATION_NAME__'))
$checkpointRoot = Join-Path $PSScriptRoot '.jtt-r02-checkpoints'
$markerPaths = @{}
$markerValues = @{}
$stages = @('COMMAND_ENTRY', 'BEFORE_LOCATION', 'AFTER_LOCATION', 'AFTER_OUTPUT', 'ERROR')
foreach ($stage in $stages) {
    $markerPaths[$stage] = Join-Path $checkpointRoot ($label.ToLowerInvariant() + '-' + $stage.ToLowerInvariant() + '-' + $nonce + '.marker')
    $markerValues[$stage] = 'JTT_R02_' + $label + '_' + $stage + '_' + $nonce
}
function Write-Checkpoint([string]$Stage) {
    try {
        [System.IO.File]::WriteAllText($markerPaths[$Stage], $markerValues[$Stage])
    } catch {
        $tokenPrefix = 'JTT_R02_EVIDENCE_' + 'WRITE_ERROR_' + $label + '_' + $Stage + '_' + $nonce + '_'
        [System.Console]::Error.WriteLine($tokenPrefix + $_.Exception.GetType().FullName)
        throw
    }
}
try {
    $phase = 'COMMAND_ENTRY'
    Write-Checkpoint 'COMMAND_ENTRY'
    $phase = 'BEFORE_LOCATION'
    Write-Checkpoint 'BEFORE_LOCATION'
    $phase = 'SET_LOCATION'
    __RELATIVE_LOCATION_COMMAND__
    $phase = 'VERIFY_LOCATION'
    $actual = [System.IO.Path]::GetFullPath($PWD.Path)
    if (-not [System.String]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'JTT_EXPECTED_LOCATION_MISMATCH'
    }
    $phase = 'AFTER_LOCATION'
    Write-Checkpoint 'AFTER_LOCATION'
    $phase = 'SIDE_EFFECT_WRITE'
    $sideEffect = Join-Path $PWD.Path ('script-path-' + $nonce + '.marker')
    [System.IO.File]::WriteAllText($sideEffect, $actual)
    $phase = 'OUTPUT_CALL'
    [Console]::WriteLine('JTT_PATH_OUTPUT_' + $actual + '_' + $nonce + '_END')
    $phase = 'AFTER_OUTPUT'
    Write-Checkpoint 'AFTER_OUTPUT'
    $phase = 'EXECUTION_MARKER'
    [Console]::WriteLine('JTT_EXEC_' + $label + '_' + $nonce + '_END')
    exit 0
} catch {
    $caught = $_
    try {
        $errorContent = $markerValues['ERROR'] + '|' + $caught.Exception.GetType().FullName + '|' + $caught.FullyQualifiedErrorId + '|' + $phase
        [System.IO.File]::WriteAllText($markerPaths['ERROR'], $errorContent)
    } catch {
        $tokenPrefix = 'JTT_R02_EVIDENCE_' + 'WRITE_ERROR_' + $label + '_ERROR_' + $nonce + '_'
        [System.Console]::Error.WriteLine($tokenPrefix + $_.Exception.GetType().FullName)
    }
    Write-Error -ErrorRecord $caught
    exit 1
}
'@
$scriptBody = $scriptBodyTemplate.Replace('__NONCE__', $nonce).Replace('__RELATIVE_LOCATION_COMMAND__', $relativeLocationCommand).Replace('__DESTINATION_NAME__', $destinationName)
[System.IO.File]::WriteAllText($scriptPath, $scriptBody, [System.Text.Encoding]::UTF8)
$scriptBodySha256 = (Get-FileHash -LiteralPath $scriptPath -Algorithm SHA256).Hash
$scriptBodyBytes = (Get-Item -LiteralPath $scriptPath).Length
$ps = Join-Path $PSHOME 'powershell.exe'
$childArguments = '-NoLogo -NoProfile -NonInteractive -File <generated-script>'
$quotedScript = '"' + $scriptPath + '"'
$process = $null
$launchError = $null
try {
    $process = Start-Process -FilePath $ps -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $quotedScript) -WorkingDirectory $work -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -Wait -PassThru
} catch {
    $launchError = [ordered]@{ type = $_.Exception.GetType().FullName; message = $_.Exception.Message }
}
$childExitCode = $null
try {
    $stdout = if ([System.IO.File]::Exists($stdoutPath)) { [System.IO.File]::ReadAllText($stdoutPath) } else { '' }
    $stderr = if ([System.IO.File]::Exists($stderrPath)) { [System.IO.File]::ReadAllText($stderrPath) } else { '' }
    $childExitCode = if ($null -ne $process) { $process.ExitCode } else { $null }
} finally {
    if ($null -ne $process) { $process.Dispose() }
}
$markerStates = [ordered]@{}
foreach ($stage in @('COMMAND_ENTRY', 'BEFORE_LOCATION', 'AFTER_LOCATION', 'AFTER_OUTPUT', 'ERROR')) {
    $path = Join-Path $checkpointRoot ($label.ToLowerInvariant() + '-' + $stage.ToLowerInvariant() + '-' + $nonce + '.marker')
    $expectedContent = 'JTT_R02_' + $label + '_' + $stage + '_' + $nonce
    $stream = $null
    $reader = $null
    $content = $null
    $readError = $null
    $missing = $false
    try {
        $stream = [System.IO.File]::OpenRead($path)
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $true)
        $content = $reader.ReadToEnd().Trim()
    } catch [System.IO.FileNotFoundException] {
        $missing = $true
    } catch [System.IO.DirectoryNotFoundException] {
        $missing = $true
    } catch {
        $readError = $_.Exception.GetType().FullName
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
        elseif ($null -ne $stream) { $stream.Dispose() }
    }
    if ($readError) {
        $markerStates[$stage] = [ordered]@{ state = 'EVIDENCE_READ_ERROR'; path = $path; readErrorCategory = $readError; meaning = $stage }
        continue
    }
    if ($missing) {
        $writePrefix = 'JTT_R02_EVIDENCE_WRITE_ERROR_' + $label + '_' + $stage + '_' + $nonce + '_'
        $writeError = $stderr.Contains($writePrefix)
        $state = if ($writeError) { 'EVIDENCE_WRITE_ERROR' } else { 'UNKNOWN' }
        $markerStates[$stage] = [ordered]@{ state = $state; path = $path; meaning = $stage }
        continue
    }
    $matches = if ($stage -eq 'ERROR') {
        $parts = $content.Split('|')
        $parts.Count -eq 4 -and [System.String]::Equals($parts[0], $expectedContent, [System.StringComparison]::Ordinal) -and -not [string]::IsNullOrWhiteSpace($parts[1]) -and $parts[3] -in @('COMMAND_ENTRY', 'BEFORE_LOCATION', 'SET_LOCATION', 'VERIFY_LOCATION', 'AFTER_LOCATION', 'SIDE_EFFECT_WRITE', 'OUTPUT_CALL', 'AFTER_OUTPUT', 'EXECUTION_MARKER')
    } else { [System.String]::Equals($content, $expectedContent, [System.StringComparison]::Ordinal) }
    $state = if ($matches) { 'OBSERVED' } else { 'UNKNOWN' }
    $markerState = [ordered]@{ state = $state; path = $path; contentMatches = $matches; meaning = $stage }
    if ($stage -eq 'ERROR' -and $matches) {
        $markerState.errorType = $parts[1]
        $markerState.errorId = $parts[2]
        $markerState.failurePhase = $parts[3]
    }
    $markerStates[$stage] = $markerState
}
$sideEffectContent = if ([System.IO.File]::Exists($sideEffect)) { [System.IO.File]::ReadAllText($sideEffect) } else { $null }
$sideEffectMatches = $null -ne $sideEffectContent -and [System.String]::Equals($sideEffectContent, $expected, [System.StringComparison]::OrdinalIgnoreCase)
$result = [ordered]@{
    scenario = 'B2_INSTRUMENTED_SCRIPT_PATH_CHANGE'
    nonce = $nonce
    root = $root
    workingDirectory = $work
    destination = $expected
    destinationName = $destinationName
    relativeLocationCommand = $relativeLocationCommand
    relativeCommandSha256 = $relativeCommandSha256
    relativeCommandUtf8Bytes = $relativeCommandBytes.Length
    scriptPath = $scriptPath
    scriptBodySha256 = $scriptBodySha256
    scriptBodyBytes = $scriptBodyBytes
    childPowerShellPath = $ps
    childArguments = $childArguments
    sideEffectContent = $sideEffectContent
    sideEffectContentMatchesExpectedPath = $sideEffectMatches
    sideEffectPath = $sideEffect
    sideEffectObserved = [System.IO.File]::Exists($sideEffect)
    childExitCode = $childExitCode
    launchError = $launchError
    stdoutPath = $stdoutPath
    stderrPath = $stderrPath
    stdout = $stdout
    stderr = $stderr
    markerStates = $markerStates
}
$result | ConvertTo-Json -Compress -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
[Console]::Out.WriteLine([System.IO.File]::ReadAllText($summaryPath))
if ($null -eq $process -or $process.ExitCode -ne 0) { exit 1 }
exit 0
