#requires -Version 5.1

<#
.SYNOPSIS
    Creates a timestamped backup of the specified Git project's files.

.DESCRIPTION
    Collects tracked, untracked and ignored files from Git, filters the combined
    list through .snapignore, and copies the remaining files while preserving
    their directory structure.

    The three Git lists are saved as returned by Git. backup-files.txt contains
    the final, filtered list that was actually selected for copying.

.PARAMETER ProjectRoot
    Root directory of the Git project to back up. It must contain the .git
    directory and the .gitignore and .snapignore files. The two files may be
    empty.

.PARAMETER BackupRoot
    Root directory in which <project>\YYYY-MM-DD\HH-mm-ss\ will be created.

.EXAMPLE
    .\backup-git-project.ps1 -ProjectRoot 'D:\Projects\MyProject' -BackupRoot 'E:\Backups'
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string] $ProjectRoot,

    [Parameter(Mandatory = $true, Position = 1)]
    [ValidateNotNullOrEmpty()]
    [string] $BackupRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-GitText {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Arguments,

        [Parameter(Mandatory = $true)]
        [string] $WorkingDirectory
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'git.exe'
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $startInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo

    try {
        if (-not $process.Start()) {
            throw 'Git could not be started.'
        }

        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()

        if ($process.ExitCode -ne 0) {
            $message = $stderr.Trim()
            if ([string]::IsNullOrWhiteSpace($message)) {
                $message = "Git exited with code $($process.ExitCode)."
            }
            throw $message
        }

        return $stdout
    }
    finally {
        $process.Dispose()
    }
}

function Get-GitNullList {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Arguments,

        [Parameter(Mandatory = $true)]
        [string] $WorkingDirectory
    )

    $output = Invoke-GitText -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    if ([string]::IsNullOrEmpty($output)) {
        return @()
    }

    # -z makes file names unambiguous, including names containing spaces/newlines.
    return @($output -split "`0" | Where-Object { $_.Length -gt 0 })
}

function Convert-BackupPatternToRegex {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Pattern
    )

    $patternText = $Pattern.Replace('\', '/')
    $rootAnchored = $patternText.StartsWith('/')
    if ($rootAnchored) {
        $patternText = $patternText.Substring(1)
    }

    $directoryRule = $patternText.EndsWith('/')
    if ($directoryRule) {
        $patternText = $patternText.TrimEnd('/')
    }

    $containsSlash = $patternText.Contains('/')
    $escaped = [Regex]::Escape($patternText)
    $escaped = $escaped.Replace('\*\*', '.*')
    $escaped = $escaped.Replace('\*', '[^/]*')
    $escaped = $escaped.Replace('\?', '[^/]')

    if ($rootAnchored -or $containsSlash) {
        $prefix = '^'
    }
    else {
        # A pattern without a slash matches a name at any directory level.
        $prefix = '(^|.*/)'
    }

    if ($directoryRule) {
        return $prefix + $escaped + '(/.*)?$'
    }

    return $prefix + $escaped + '$'
}

function Get-SnapIgnoreRules {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path
    )

    $rules = New-Object System.Collections.Generic.List[object]

    foreach ($rawLine in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        $line = $rawLine.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) {
            continue
        }

        $include = $false
        if ($line.StartsWith('!')) {
            $include = $true
            $line = $line.Substring(1)
        }

        if ($line.Length -eq 0) {
            continue
        }

        $rules.Add([PSCustomObject]@{
            Include = $include
            Regex   = New-Object Regex(
                (Convert-BackupPatternToRegex -Pattern $line),
                [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
            )
        })
    }

    return $rules.ToArray()
}

function Test-BackupIncluded {
    param(
        [Parameter(Mandatory = $true)]
        [string] $RelativePath,

        [Parameter(Mandatory = $true)]
        [object[]] $Rules
    )

    $pathText = $RelativePath.Replace('\', '/').TrimStart('/')
    $included = $true

    # Later rules override earlier rules, including ! re-inclusion rules.
    foreach ($rule in $Rules) {
        if ($rule.Regex.IsMatch($pathText)) {
            $included = [bool] $rule.Include
        }
    }

    return $included
}

function Write-Utf8Lines {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,

        [AllowEmptyCollection()]
        [string[]] $Lines = @()
    )

    [System.IO.File]::WriteAllLines(
        $Path,
        [string[]] $Lines,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

try {
    $projectRootFull = [System.IO.Path]::GetFullPath(
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ProjectRoot)
    )

    if (-not (Test-Path -LiteralPath $projectRootFull -PathType Container)) {
        throw "ProjectRoot does not exist or is not a directory: $projectRootFull"
    }

    $requiredItems = @(
        [PSCustomObject]@{ Name = '.git';          PathType = 'Container' }
        [PSCustomObject]@{ Name = '.gitignore';    PathType = 'Leaf' }
        [PSCustomObject]@{ Name = '.snapignore';   PathType = 'Leaf' }
    )
    $missingItems = New-Object System.Collections.Generic.List[string]

    foreach ($requiredItem in $requiredItems) {
        $requiredPath = Join-Path $projectRootFull $requiredItem.Name
        if (-not (Test-Path -LiteralPath $requiredPath -PathType $requiredItem.PathType)) {
            if ($requiredItem.PathType -eq 'Container') {
                $expectedType = 'directory'
            }
            else {
                $expectedType = 'file (it may be empty)'
            }
            $missingItems.Add("$($requiredItem.Name) [$expectedType]")
        }
    }

    if ($missingItems.Count -gt 0) {
        throw "ProjectRoot is missing required items: $($missingItems -join ', '). Root: $projectRootFull"
    }

    try {
        $gitTopLevelText = Invoke-GitText -Arguments 'rev-parse --show-toplevel' -WorkingDirectory $projectRootFull
    }
    catch {
        throw "ProjectRoot is not a valid Git repository, or Git is unavailable. $($_.Exception.Message)"
    }

    $projectRoot = [System.IO.Path]::GetFullPath($gitTopLevelText.Trim())
    if (-not $projectRoot.Equals($projectRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "ProjectRoot must point to the repository root. Git reports: $projectRoot"
    }

    $projectName = Split-Path -Leaf $projectRoot.TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($projectName)) {
        throw "Could not determine the project name from '$projectRoot'."
    }

    $backupRootFull = [System.IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BackupRoot))
    $projectPrefix = $projectRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $backupPrefix = $backupRootFull.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $backupProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $backupRootFull $projectName))
    $backupProjectPrefix = $backupProjectRoot.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar

    if ($backupRootFull.Equals($projectRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $backupPrefix.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        $backupProjectRoot.Equals($projectRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $backupProjectPrefix.StartsWith($projectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'BackupRoot must be outside the Git project to prevent recursive backups.'
    }

    $snapIgnorePath = Join-Path $projectRoot '.snapignore'

    Write-Host "Project: $projectName"
    Write-Host "Root:    $projectRoot"
    Write-Host 'Reading file lists from Git...'

    $tracked = @(Get-GitNullList -Arguments '-c core.quotepath=false ls-files -z' -WorkingDirectory $projectRoot)
    $untracked = @(Get-GitNullList -Arguments '-c core.quotepath=false ls-files --others --exclude-standard -z' -WorkingDirectory $projectRoot)
    $ignored = @(Get-GitNullList -Arguments '-c core.quotepath=false ls-files --others --ignored --exclude-standard -z' -WorkingDirectory $projectRoot)

    $rules = @(Get-SnapIgnoreRules -Path $snapIgnorePath)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $allCandidates = New-Object System.Collections.Generic.List[string]

    foreach ($path in @($tracked) + @($untracked) + @($ignored)) {
        $normalized = $path.Replace('\', '/')
        if ($seen.Add($normalized)) {
            $allCandidates.Add($normalized)
        }
    }

    $backupFiles = @(
        $allCandidates |
            Where-Object { Test-BackupIncluded -RelativePath $_ -Rules $rules } |
            Sort-Object
    )
    $excludedCount = $allCandidates.Count - $backupFiles.Count

    $now = Get-Date
    $datePart = $now.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
    $timePart = $now.ToString('HH-mm-ss', [Globalization.CultureInfo]::InvariantCulture)
    $snapshotRoot = Join-Path (Join-Path (Join-Path $backupRootFull $projectName) $datePart) $timePart

    if (Test-Path -LiteralPath $snapshotRoot) {
        throw "Backup destination already exists: $snapshotRoot. Run the script again in a second."
    }

    $contentRoot = Join-Path $snapshotRoot 'backup'
    [System.IO.Directory]::CreateDirectory($contentRoot) | Out-Null

    Write-Utf8Lines -Path (Join-Path $snapshotRoot 'tracked.txt') -Lines $tracked
    Write-Utf8Lines -Path (Join-Path $snapshotRoot 'untracked.txt') -Lines $untracked
    Write-Utf8Lines -Path (Join-Path $snapshotRoot 'ignored.txt') -Lines $ignored
    Write-Utf8Lines -Path (Join-Path $snapshotRoot 'backup-files.txt') -Lines $backupFiles

    $copiedCount = 0
    $missingCount = 0
    $failedCount = 0
    $copyErrors = New-Object System.Collections.Generic.List[string]

    foreach ($relativePath in $backupFiles) {
        $systemRelativePath = $relativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
        $sourcePath = Join-Path $projectRoot $systemRelativePath
        $destinationPath = Join-Path $contentRoot $systemRelativePath

        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            $missingCount++
            $copyErrors.Add("MISSING: $relativePath")
            continue
        }

        try {
            $destinationDirectory = Split-Path -Parent $destinationPath
            [System.IO.Directory]::CreateDirectory($destinationDirectory) | Out-Null
            [System.IO.File]::Copy($sourcePath, $destinationPath, $false)
            $copiedCount++
        }
        catch {
            $failedCount++
            $copyErrors.Add("FAILED: $relativePath :: $($_.Exception.Message)")
        }
    }

    if ($copyErrors.Count -gt 0) {
        Write-Utf8Lines -Path (Join-Path $snapshotRoot 'copy-errors.txt') -Lines $copyErrors.ToArray()
    }

    Write-Host ''
    Write-Host 'Backup complete.' -ForegroundColor Green
    Write-Host "Destination: $snapshotRoot"
    Write-Host "Tracked:    $($tracked.Count)"
    Write-Host "Untracked:  $($untracked.Count)"
    Write-Host "Ignored:    $($ignored.Count)"
    Write-Host "Unique:     $($allCandidates.Count)"
    Write-Host "Excluded:   $excludedCount"
    Write-Host "Selected:   $($backupFiles.Count)"
    Write-Host "Copied:     $copiedCount"
    Write-Host "Missing:    $missingCount"
    Write-Host "Failed:     $failedCount"

    if ($missingCount -gt 0 -or $failedCount -gt 0) {
        Write-Warning "Some files were not copied. See: $(Join-Path $snapshotRoot 'copy-errors.txt')"
        exit 2
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
