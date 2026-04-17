<#
.SYNOPSIS
Submits a Kubernetes Job that builds a model image from MLflow artifacts.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$ModelName,
    [string]$ModelVersion = "latest",
    [string]$Namespace = "",
    [string]$RunId = "",
    [string]$ModelUri = "",
    [string]$TargetImage = "",
    [switch]$InsecureRegistryTls,
    [switch]$Wait
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

# Normalizes registry input and returns host[:port] without scheme or path.
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

# Imports KEY=VALUE pairs from .env into the current process.
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

# Converts a model name into a deterministic Kubernetes slug.
function To-Slug {
    param([string]$Text)
    $slug = $Text.ToLowerInvariant().Replace("_", "-")
    $slug = [Regex]::Replace($slug, "[^a-z0-9-]+", "-")
    $slug = [Regex]::Replace($slug, "-+", "-")
    return $slug.Trim("-")
}

# Validates Kubernetes DNS label format.
function Test-ValidK8sName {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
}

# Validates allowed characters for a container image reference.
function Test-ValidImageRef {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[a-z0-9./:_-]+$")
}

# Validates registry host[:port] format.
function Test-ValidRegistryServer {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[A-Za-z0-9.-]+(:[0-9]+)?$")
}

# Parses common truthy values (1/true/yes) from text.
function Test-TrueLikeValue {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }

    switch ($Value.Trim().ToLowerInvariant()) {
        "1" { return $true }
        "true" { return $true }
        "yes" { return $true }
        default { return $false }
    }
}

# Escapes backslashes and quotes for YAML double-quoted string values.
function Escape-YamlDoubleQuoted {
    param([string]$Value)

    $escaped = $Value.Replace("\", "\\")
    return $escaped.Replace('"', '\"')
}

# Replaces placeholder tokens in a template file with runtime values.
function Render-Template {
    param(
        [string]$TemplatePath,
        [hashtable]$Replacements
    )

    $content = Get-Content -LiteralPath $TemplatePath -Raw
    foreach ($key in $Replacements.Keys) {
        $content = $content.Replace($key, [string]$Replacements[$key])
    }
    return $content
}

Import-DotEnv

if ([string]::IsNullOrWhiteSpace($Namespace)) {
    $Namespace = if ($env:K8S_NAMESPACE) { $env:K8S_NAMESPACE } else { "api-models" }
}

$mlflowTrackingUri = if ($env:MLFLOW_TRACKING_URI) { $env:MLFLOW_TRACKING_URI } else { "" }
if ([string]::IsNullOrWhiteSpace($mlflowTrackingUri) -and [string]::IsNullOrWhiteSpace($ModelUri)) {
    throw "Set MLFLOW_TRACKING_URI or pass -ModelUri."
}

$registryServer = if ($env:REGISTRY_SERVER) { $env:REGISTRY_SERVER } else { "" }
$registryServer = Normalize-RegistryServer -Value $registryServer
$registryServer = $registryServer.ToLowerInvariant()
$tuOrg = if ($env:TU_ORG) { $env:TU_ORG } else { "" }
$registrySecret = if ($env:MODEL_REGISTRY_SECRET) { $env:MODEL_REGISTRY_SECRET } else { "model-registry-creds" }
$kanikoImage = if ($env:KANIKO_IMAGE) { $env:KANIKO_IMAGE } else { "gcr.io/kaniko-project/executor:v1.23.2-debug" }

if (-not [string]::IsNullOrWhiteSpace($registryServer) -and -not (Test-ValidRegistryServer -Value $registryServer)) {
    throw "REGISTRY_SERVER must be a registry host (example: registry.example.com or registry.example.com:5000)."
}

$skipTlsVerify = $InsecureRegistryTls.IsPresent
if (-not $skipTlsVerify) {
    $skipTlsVerify = Test-TrueLikeValue -Value (if ($env:KANIKO_SKIP_TLS_VERIFY) { $env:KANIKO_SKIP_TLS_VERIFY } else { "" })
}

$modelSlug = To-Slug -Text $ModelName
$modelVersionSlug = [Regex]::Replace($ModelVersion.ToLowerInvariant(), "[^a-z0-9._-]+", "-")

if ([string]::IsNullOrWhiteSpace($modelSlug)) {
    throw "ModelName produced an empty slug."
}
if (-not (Test-ValidK8sName -Value $modelSlug)) {
    throw "Model slug '$modelSlug' is not a valid Kubernetes label value."
}
if (-not (Test-ValidK8sName -Value $Namespace)) {
    throw "Namespace '$Namespace' is not a valid Kubernetes namespace name."
}
if (-not [string]::IsNullOrWhiteSpace($RunId) -and -not [Regex]::IsMatch($RunId, "^[A-Za-z0-9._-]{4,128}$")) {
    throw "Invalid -RunId format."
}

if ([string]::IsNullOrWhiteSpace($TargetImage)) {
    if ([string]::IsNullOrWhiteSpace($tuOrg)) {
        throw "Set TU_ORG or pass -TargetImage."
    }
    if ([string]::IsNullOrWhiteSpace($registryServer)) {
        throw "Set REGISTRY_SERVER or pass -TargetImage."
    }

    if (-not [string]::IsNullOrWhiteSpace($RunId)) {
        $runShort = if ($RunId.Length -gt 8) { $RunId.Substring(0, 8) } else { $RunId }
        $tag = "v{0}-r{1}" -f $modelVersionSlug, $runShort
    }
    else {
        $tag = "v{0}-t{1}" -f $modelVersionSlug, (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
    }

    $TargetImage = "$registryServer/$tuOrg/model-${modelSlug}:$tag"
}

if (-not (Test-ValidImageRef -Value $TargetImage)) {
    throw "Target image '$TargetImage' has invalid characters."
}

$baseImage = if ($env:MODEL_BASE_IMAGE) { $env:MODEL_BASE_IMAGE } else { "" }
if ([string]::IsNullOrWhiteSpace($baseImage)) {
    if ([string]::IsNullOrWhiteSpace($tuOrg)) {
        throw "Set MODEL_BASE_IMAGE or TU_ORG."
    }
    if ([string]::IsNullOrWhiteSpace($registryServer)) {
        throw "Set MODEL_BASE_IMAGE or REGISTRY_SERVER."
    }
    $baseImage = "$registryServer/$tuOrg/model-service-base:py310"
}

$jobName = "build-model-$modelSlug-" + (Get-Date -Format "HHmmss")
$jobName = ([Regex]::Replace($jobName.ToLowerInvariant(), "[^a-z0-9-]+", "-")).Trim("-")
if ($jobName.Length -gt 63) {
    $jobName = $jobName.Substring(0, 63).TrimEnd("-")
}

$kanikoExtraArgs = New-Object System.Collections.Generic.List[string]
if ($skipTlsVerify) {
    [void]$kanikoExtraArgs.Add("--skip-tls-verify")
    [void]$kanikoExtraArgs.Add("--skip-tls-verify-pull")
    if (-not [string]::IsNullOrWhiteSpace($registryServer)) {
        [void]$kanikoExtraArgs.Add("--skip-tls-verify-registry=$registryServer")
    }
    Write-Warning "TLS verification is disabled for Kaniko registry operations."
}

$kanikoExtraArgsBlock = ""
if ($kanikoExtraArgs.Count -gt 0) {
    $kanikoExtraArgsBlock = ($kanikoExtraArgs | ForEach-Object { "            - $_" }) -join "`n"
}

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "Error: kubectl is not installed or not in PATH."
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

$null = & kubectl -n $Namespace get secret $registrySecret 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Secret '$registrySecret' not found in namespace '$Namespace'."
}

$scriptDir = Split-Path -Parent $PSCommandPath
$templatePath = Join-Path (Join-Path (Join-Path $scriptDir "yaml") "model_image_job") "job.yaml"
if (-not (Test-Path -LiteralPath $templatePath)) {
    throw "Missing template file: $templatePath"
}

$replacements = @{
    "__JOB_NAME__" = $jobName
    "__NAMESPACE__" = $Namespace
    "__MODEL_SLUG__" = $modelSlug
    "__REGISTRY_SECRET__" = $registrySecret
    "__MLFLOW_TRACKING_URI__" = (Escape-YamlDoubleQuoted -Value $mlflowTrackingUri)
    "__MODEL_NAME__" = (Escape-YamlDoubleQuoted -Value $ModelName)
    "__MODEL_VERSION__" = (Escape-YamlDoubleQuoted -Value $ModelVersion)
    "__MODEL_URI__" = (Escape-YamlDoubleQuoted -Value $ModelUri)
    "__KANIKO_IMAGE__" = $kanikoImage
    "__TARGET_IMAGE__" = $TargetImage
    "__BASE_IMAGE__" = $baseImage
    "__KANIKO_TLS_ARGS_BLOCK__" = $kanikoExtraArgsBlock
}

$manifest = Render-Template -TemplatePath $templatePath -Replacements $replacements

$manifest | & kubectl apply -f - | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Error applying build job manifest."
}

Write-Host "Build Job submitted."
Write-Host ("Job: {0}" -f $jobName)
Write-Host ("Namespace: {0}" -f $Namespace)
Write-Host ("Target image: {0}" -f $TargetImage)
Write-Host ("Base image: {0}" -f $baseImage)
Write-Host ""
Write-Host "Check status:"
Write-Host ("  kubectl -n {0} get job {1}" -f $Namespace, $jobName)
Write-Host ("  kubectl -n {0} logs job/{1} -c prepare-build-context --tail=200" -f $Namespace, $jobName)
Write-Host ("  kubectl -n {0} logs job/{1} -c kaniko --tail=200" -f $Namespace, $jobName)

if ($Wait) {
    Write-Host ""
    Write-Host "Waiting for Job completion..."
    & kubectl -n $Namespace wait --for=condition=complete --timeout=1800s ("job/{0}" -f $jobName) | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Job did not complete successfully. Showing logs."
        & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c prepare-build-context --tail=200 | Out-Host
        & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c kaniko --tail=200 | Out-Host
        throw "Build Job failed."
    }

    Write-Host "Job completed successfully."
    & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c prepare-build-context --tail=200 | Out-Host
    & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c kaniko --tail=200 | Out-Host
}
