param(
    [string]$Org = "",
    [string]$Tag = "py310",
    [string]$RegistryServer = "",
    [string]$RegistryUsername = "",
    [string]$RegistryPassword = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Normalize-RegistryServer {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }

    $candidate = $Value.Trim()
    if ($candidate -match "^[a-zA-Z][a-zA-Z0-9+.-]*://") {
        try {
            $uri = [Uri]$candidate
            if (-not [string]::IsNullOrWhiteSpace($uri.Host)) {
                if ($uri.IsDefaultPort) {
                    return $uri.Host
                }
                return ("{0}:{1}" -f $uri.Host, $uri.Port)
            }
        }
        catch {
        }
    }

    $candidate = ($candidate -replace "^[a-zA-Z][a-zA-Z0-9+.-]*://", "")
    return ($candidate -split "/", 2)[0].Trim()
}

function Test-ValidRegistryServer {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[A-Za-z0-9.-]+(:[0-9]+)?$")
}

function Import-DotEnv {
    param([string]$Path = ".env")
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            return
        }

        $idx = $line.IndexOf("=")
        if ($idx -lt 1) {
            return
        }

        $key = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()

        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        Set-Item -Path ("Env:{0}" -f $key) -Value $value
    }
}

Import-DotEnv

if ([string]::IsNullOrWhiteSpace($Org)) {
    $Org = if ($env:TU_ORG) { $env:TU_ORG } else { "" }
}
if ([string]::IsNullOrWhiteSpace($RegistryServer)) {
    $RegistryServer = if ($env:REGISTRY_SERVER) { $env:REGISTRY_SERVER } else { "" }
}
$RegistryUsername = if ([string]::IsNullOrWhiteSpace($RegistryUsername)) {
    if ($env:REGISTRY_USERNAME) { $env:REGISTRY_USERNAME } else { "" }
}
else {
    $RegistryUsername
}
$RegistryPassword = if ([string]::IsNullOrWhiteSpace($RegistryPassword)) {
    if ($env:REGISTRY_PASSWORD) { $env:REGISTRY_PASSWORD } else { "" }
}
else {
    $RegistryPassword
}
$RegistryServer = Normalize-RegistryServer -Value $RegistryServer

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Error: docker is required and is not in PATH."
}

if ([string]::IsNullOrWhiteSpace($Org)) {
    throw "Error: missing org. Pass -Org or set TU_ORG."
}
if ([string]::IsNullOrWhiteSpace($RegistryServer)) {
    throw "Error: missing RegistryServer. Pass -RegistryServer or set REGISTRY_SERVER (no default assumed)."
}
if (-not (Test-ValidRegistryServer -Value $RegistryServer)) {
    throw "Error: RegistryServer must be a registry host (example: registry.example.com or registry.example.com:5000)."
}
if (
    [string]::IsNullOrWhiteSpace($RegistryUsername) -xor
    [string]::IsNullOrWhiteSpace($RegistryPassword)
) {
    throw "Error: provide both RegistryUsername and RegistryPassword, or neither."
}

$baseImage = "$RegistryServer/$Org/model-service-base:$Tag"

& docker build -f model_service/Dockerfile.base -t $baseImage model_service
if ($LASTEXITCODE -ne 0) {
    throw "Error building base image."
}

if (-not [string]::IsNullOrWhiteSpace($RegistryUsername)) {
    $RegistryPassword | & docker login $RegistryServer --username $RegistryUsername --password-stdin
    if ($LASTEXITCODE -ne 0) {
        throw "Error logging into registry '$RegistryServer'. Check credentials/token and registry URL."
    }
}

& docker push $baseImage
if ($LASTEXITCODE -ne 0) {
    throw "Error pushing base image. Check docker login and push permissions for '$RegistryServer/$Org'."
}

Write-Host "Base image built and pushed."
Write-Host ("MODEL_BASE_IMAGE={0}" -f $baseImage)
