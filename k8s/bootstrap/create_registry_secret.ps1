param(
    [string]$Namespace = "",
    [string]$SecretName = "",
    [string]$RegistryServer = "",
    [string]$RegistryUsername = "",
    [string]$RegistryPassword = "",
    [string]$RegistryEmail = "noreply@example.com"
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

if ([string]::IsNullOrWhiteSpace($Namespace)) {
    $Namespace = if ($env:K8S_NAMESPACE) { $env:K8S_NAMESPACE } else { "api-models" }
}
if ([string]::IsNullOrWhiteSpace($SecretName)) {
    $SecretName = if ($env:MODEL_REGISTRY_SECRET) { $env:MODEL_REGISTRY_SECRET } else { "model-registry-creds" }
}
if ([string]::IsNullOrWhiteSpace($RegistryServer)) {
    $RegistryServer = if ($env:REGISTRY_SERVER) { $env:REGISTRY_SERVER } else { "" }
}
if ([string]::IsNullOrWhiteSpace($RegistryUsername)) {
    $RegistryUsername = if ($env:REGISTRY_USERNAME) { $env:REGISTRY_USERNAME } else { "" }
}
if ([string]::IsNullOrWhiteSpace($RegistryPassword)) {
    $RegistryPassword = if ($env:REGISTRY_PASSWORD) { $env:REGISTRY_PASSWORD } else { "" }
}
$RegistryServer = Normalize-RegistryServer -Value $RegistryServer

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "Error: kubectl is not installed or not in PATH."
}

if ([string]::IsNullOrWhiteSpace($RegistryUsername) -or [string]::IsNullOrWhiteSpace($RegistryPassword)) {
    throw "Error: set REGISTRY_USERNAME and REGISTRY_PASSWORD (args or environment) before running this script."
}
if ([string]::IsNullOrWhiteSpace($RegistryServer)) {
    throw "Error: set REGISTRY_SERVER (args or environment). No default registry is assumed."
}
if (-not (Test-ValidRegistryServer -Value $RegistryServer)) {
    throw "Error: REGISTRY_SERVER must be a registry host (example: registry.example.com or registry.example.com:5000)."
}

$namespaceExists = $true
try {
    $null = & kubectl get namespace $Namespace 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        $namespaceExists = $false
    }
}
catch {
    $namespaceExists = $false
}

if (-not $namespaceExists) {
    & kubectl create namespace $Namespace | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Error creating namespace: $Namespace"
    }
}

& kubectl -n $Namespace create secret docker-registry $SecretName `
    --docker-server=$RegistryServer `
    --docker-username=$RegistryUsername `
    --docker-password=$RegistryPassword `
    --docker-email=$RegistryEmail `
    --dry-run=client -o yaml |
    & kubectl apply -f - | Out-Host

if ($LASTEXITCODE -ne 0) {
    throw "Error creating/updating registry secret."
}

Write-Host "Registry secret ensured."
Write-Host ("Namespace: {0}" -f $Namespace)
Write-Host ("Secret: {0}" -f $SecretName)
Write-Host ("Registry: {0}" -f $RegistryServer)
