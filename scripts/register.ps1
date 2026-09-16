[CmdletBinding()]
param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$repository = "git+https://github.com/blakestone-x/jev-mcp@v0.2.1"
$placeholderApiKey = "..."
$claudeCommand = "claude mcp add --scope user jev -e TYPESAFE_API_KEY=$placeholderApiKey -- uvx --from $repository jev-mcp"
$codexBlock = @"
[mcp_servers.jev]
command = "uvx"
args = ["--from", "$repository", "jev-mcp"]
env = { TYPESAFE_API_KEY = "$placeholderApiKey" }
"@
$genericBlock = @"
{
  "mcpServers": {
    "jev": {
      "command": "uvx",
      "args": ["--from", "$repository", "jev-mcp"],
      "env": { "TYPESAFE_API_KEY": "$placeholderApiKey" }
    }
  }
}
"@

if (-not $Apply) {
    Write-Output $claudeCommand
    Write-Output ""
    Write-Output "Codex configuration block:"
    Write-Output $codexBlock
    Write-Output "Generic MCP JSON block:"
    Write-Output $genericBlock
    return
}

$apiKey = if ($env:TYPESAFE_API_KEY) { $env:TYPESAFE_API_KEY.Trim() } else { "" }
if (-not $apiKey) {
    throw "TYPESAFE_API_KEY is not set; refusing to apply registration changes."
}
if ($apiKey -match '["\r\n\\]') {
    throw "TYPESAFE_API_KEY contains unsupported characters; refusing to apply registration changes."
}
if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "register.ps1 requires Windows for ACL-protected configuration writes."
}

function Set-ConfigFileSecurity {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $accessRule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $identity,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($accessRule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-ConfigFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Content
    )

    $temporary = "$Path.tmp.$([Guid]::NewGuid().ToString('N'))"
    try {
        [System.IO.File]::WriteAllText($temporary, $Content)
        Set-ConfigFileSecurity -Path $temporary
        if (Test-Path -LiteralPath $Path) {
            Copy-Item -LiteralPath $Path -Destination "$Path.bak" -Force
            Set-ConfigFileSecurity -Path "$Path.bak"
        }
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

$codexApplyBlock = @"
[mcp_servers.jev]
command = "uvx"
args = ["--from", "$repository", "jev-mcp"]
env = { TYPESAFE_API_KEY = "$apiKey" }
"@

$claudeListOutput = @()
$claudeListExitCode = 0
try {
    $claudeListOutput = & claude mcp list 2>&1
    $claudeListExitCode = $LASTEXITCODE
} catch {
    $claudeListExitCode = 1
}
$claudeListText = ($claudeListOutput -join [Environment]::NewLine)
$hasJev = $claudeListText -match '(?im)^\s*jev(?:\s|:|$)'

$claudeGetText = ""
if ($hasJev) {
    $claudeGetOutput = @()
    $claudeGetExitCode = 0
    try {
        $claudeGetOutput = & claude mcp get jev 2>&1
        $claudeGetExitCode = $LASTEXITCODE
    } catch {
        $claudeGetExitCode = 1
    }
    $claudeGetText = ($claudeGetOutput -join [Environment]::NewLine)
    if ($claudeGetText -match '(?i)TYPESAFE_API_KEY\s*(?:=|:)\s*["'']?\.\.\.') {
        $claudeRemoveOutput = @()
        $claudeRemoveExitCode = 0
        try {
            $claudeRemoveOutput = & claude mcp remove jev 2>&1
            $claudeRemoveExitCode = $LASTEXITCODE
        } catch {
            $claudeRemoveExitCode = 1
        }
        if ($claudeRemoveExitCode -ne 0) {
            throw "claude mcp remove failed with exit code $claudeRemoveExitCode"
        }
        $hasJev = $false
        Write-Output "Claude configuration contained a placeholder; replacing jev registration."
    }
}

if ($hasJev) {
    Write-Output "Claude configuration already contains jev; skipping add."
} else {
    $claudeAddOutput = @()
    $claudeAddExitCode = 0
    try {
        $claudeAddOutput = & claude mcp add --scope user jev -e ("TYPESAFE_API_KEY=" + $apiKey) -- uvx --from $repository jev-mcp 2>&1
        $claudeAddExitCode = $LASTEXITCODE
    } catch {
        $claudeAddExitCode = 1
    }
    $claudeAddText = ($claudeAddOutput -join [Environment]::NewLine)
    if ($claudeAddExitCode -ne 0 -and $claudeAddText -notmatch '(?i)already exists') {
        throw "claude mcp add failed with exit code $claudeAddExitCode"
    }
    if ($claudeAddExitCode -ne 0) {
        Write-Output "Claude mcp add reported that jev already exists; continuing."
    }
}

# A lookup is diagnostic only. Some CLI versions return non-zero here even
# after a successful add, so its exit code must never stop this script.
$claudeGetExitCode = 0
try {
    & claude mcp get jev *> $null
    $claudeGetExitCode = $LASTEXITCODE
} catch {
    $claudeGetExitCode = 1
}
Write-Verbose "claude mcp get exited with code $claudeGetExitCode"

$userHome = if ($env:HOME) { $env:HOME } else { $HOME }
$configPath = Join-Path $userHome ".codex\config.toml"
$configDirectory = Split-Path -Parent $configPath
if (-not (Test-Path -LiteralPath $configDirectory)) {
    New-Item -ItemType Directory -Path $configDirectory -Force | Out-Null
}

$existing = if (Test-Path -LiteralPath $configPath) {
    Get-Content -Raw -LiteralPath $configPath
} else {
    ""
}

$entryPattern = '(?ms)^\[mcp_servers\.jev\]\s*$.*?(?=^\[|\z)'
$entryMatch = [System.Text.RegularExpressions.Regex]::Match($existing, $entryPattern)
if ($entryMatch.Success -and $entryMatch.Value -match '(?i)TYPESAFE_API_KEY\s*=\s*"\.\.\."') {
    $evaluator = [System.Text.RegularExpressions.MatchEvaluator]{
        param($match)
        $match.Groups[1].Value + '"' + $apiKey + '"'
    }
    $updatedEntry = [System.Text.RegularExpressions.Regex]::Replace(
        $entryMatch.Value,
        '(TYPESAFE_API_KEY\s*=\s*)"\.\.\."',
        $evaluator
    )
    $existing = $existing.Substring(0, $entryMatch.Index) + $updatedEntry + $existing.Substring($entryMatch.Index + $entryMatch.Length)
    Write-ConfigFile -Path $configPath -Content $existing
    Write-Output "Codex configuration placeholder replaced: $configPath"
} elseif (-not $entryMatch.Success) {
    $prefix = if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) { "`r`n" } else { "" }
    Write-ConfigFile -Path $configPath -Content ($existing + $prefix + $codexApplyBlock.Trim() + "`r`n")
    Write-Output "Codex configuration updated: $configPath"
} else {
    Set-ConfigFileSecurity -Path $configPath
    Write-Output "Codex configuration already contains [mcp_servers.jev]: $configPath"
}
