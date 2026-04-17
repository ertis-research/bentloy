<#
.SYNOPSIS
Deploys one model_service instance from template manifests.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$ModelName,
    [string]$ModelVersion = "latest",
    [string]$Namespace = "",
    [int]$Replicas = 1,
    [string]$DeployedName = "",
    [switch]$AppendDate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
$PSNativeCommandUseErrorActionPreference = $false

# Imports KEY=VALUE pairs from .env only when variables are not already set in the process.
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

        $existingValue = [Environment]::GetEnvironmentVariable($key)
        if ([string]::IsNullOrEmpty($existingValue)) {
            Set-Item -Path ("Env:{0}" -f $key) -Value $value
        }
    }
}

# Converts a free-form name into a Kubernetes-safe slug.
function To-Slug {
    param([string]$Text)

    $slug = $Text.ToLowerInvariant().Replace("_", "-")
    $slug = [Regex]::Replace($slug, "[^a-z0-9-]+", "-")
    $slug = [Regex]::Replace($slug, "-+", "-")
    $slug = $slug.Trim("-")
    return $slug
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

$modelServiceImage = if ($env:MODEL_SERVICE_IMAGE) { $env:MODEL_SERVICE_IMAGE } else { "model-service:latest" }
$modelServicePrefix = if ($env:MODEL_SERVICE_PREFIX) { $env:MODEL_SERVICE_PREFIX } else { "model" }
$mlflowTrackingUri = if ($env:MLFLOW_TRACKING_URI) { $env:MLFLOW_TRACKING_URI } else { "" }
$modelUri = if ($env:MODEL_URI) { $env:MODEL_URI } else { "" }
$modelRegistrySecret = if ($env:MODEL_REGISTRY_SECRET) { $env:MODEL_REGISTRY_SECRET } else { "" }

# If the image points to a remote registry and no pull secret was provided,
# default to the standard secret used in this project.
if (
    [string]::IsNullOrWhiteSpace($modelRegistrySecret) -and
    [Regex]::IsMatch($modelServiceImage, "^[a-z0-9.-]+(:[0-9]+)?/")
) {
    $modelRegistrySecret = "model-registry-creds"
}

if ([string]::IsNullOrWhiteSpace($mlflowTrackingUri) -and [string]::IsNullOrWhiteSpace($modelUri)) {
    throw "Error: set MLFLOW_TRACKING_URI or MODEL_URI (in .env or environment)."
}

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "Error: kubectl is not installed or not in PATH."
}

$deployedNameEffective = if ([string]::IsNullOrWhiteSpace($DeployedName)) {
    $ModelName
}
else {
    $DeployedName.Trim()
}

if ($AppendDate.IsPresent) {
    $dateSuffix = (Get-Date).ToUniversalTime().ToString("yyyyMMdd")
    $deployedNameEffective = "{0}_{1}" -f $deployedNameEffective, $dateSuffix
}

$modelSlug = To-Slug -Text $deployedNameEffective
$deployName = "{0}-{1}" -f $modelServicePrefix, $modelSlug
$serviceName = "{0}-{1}" -f $modelServicePrefix, $modelSlug

if ([string]::IsNullOrWhiteSpace($modelSlug)) {
    throw "MODEL_NAME produced an empty slug."
}
if (-not (Test-ValidK8sName -Value $Namespace)) {
    throw "Invalid namespace '$Namespace'."
}
if (-not (Test-ValidK8sName -Value $modelSlug)) {
    throw "Invalid model slug '$modelSlug'."
}
if (-not (Test-ValidImageRef -Value $modelServiceImage)) {
    throw "Invalid MODEL_SERVICE_IMAGE '$modelServiceImage'."
}
if (-not [string]::IsNullOrWhiteSpace($modelRegistrySecret) -and -not (Test-ValidK8sName -Value $modelRegistrySecret)) {
    throw "Invalid MODEL_REGISTRY_SECRET '$modelRegistrySecret'."
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

$scriptDir = Split-Path -Parent $PSCommandPath
$templateDir = Join-Path (Join-Path $scriptDir "yaml") "model_service"
$deploymentTemplatePath = Join-Path $templateDir "deployment.yaml"
$serviceTemplatePath = Join-Path $templateDir "service.yaml"

if (-not (Test-Path -LiteralPath $deploymentTemplatePath)) {
    throw "Missing template file: $deploymentTemplatePath"
}
if (-not (Test-Path -LiteralPath $serviceTemplatePath)) {
    throw "Missing template file: $serviceTemplatePath"
}

$modelUriEnvBlock = ""
if (-not [string]::IsNullOrWhiteSpace($modelUri)) {
    $escapedModelUri = Escape-YamlDoubleQuoted -Value $modelUri
    $modelUriEnvBlock = @"
            - name: MODEL_URI
              value: "$escapedModelUri"
"@
    $modelUriEnvBlock = $modelUriEnvBlock.TrimEnd("`r", "`n")
}

$imagePullSecretsBlock = ""
if (-not [string]::IsNullOrWhiteSpace($modelRegistrySecret)) {
    $imagePullSecretsBlock = @"
      imagePullSecrets:
        - name: $modelRegistrySecret
"@
    $imagePullSecretsBlock = $imagePullSecretsBlock.TrimEnd("`r", "`n")
}

$replacements = @{
    "__DEPLOY_NAME__" = $deployName
    "__SERVICE_NAME__" = $serviceName
    "__NAMESPACE__" = $Namespace
    "__MODEL_SLUG__" = $modelSlug
    "__REPLICAS__" = [string]$Replicas
    "__MODEL_SERVICE_IMAGE__" = (Escape-YamlDoubleQuoted -Value $modelServiceImage)
    "__MLFLOW_TRACKING_URI__" = (Escape-YamlDoubleQuoted -Value $mlflowTrackingUri)
    "__MODEL_NAME__" = (Escape-YamlDoubleQuoted -Value $ModelName)
    "__MODEL_VERSION__" = (Escape-YamlDoubleQuoted -Value $ModelVersion)
    "__IMAGE_PULL_SECRETS_BLOCK__" = $imagePullSecretsBlock
    "__MODEL_URI_ENV_BLOCK__" = $modelUriEnvBlock
}

$deploymentManifest = Render-Template -TemplatePath $deploymentTemplatePath -Replacements $replacements
$serviceManifest = Render-Template -TemplatePath $serviceTemplatePath -Replacements $replacements
$manifest = "$deploymentManifest`n---`n$serviceManifest`n"

$manifest | & kubectl apply -f - | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Error applying deployment/service manifest."
}

Write-Host ("Model deployed: {0} ({1})" -f $ModelName, $ModelVersion)
Write-Host ("Deployed alias: {0}" -f $deployedNameEffective)
Write-Host ("Namespace: {0}" -f $Namespace)
Write-Host ("Internal service URL: http://{0}.{1}.svc.cluster.local:3000" -f $serviceName, $Namespace)
Write-Host ("Call through gateway with model_name={0}" -f $deployedNameEffective)
