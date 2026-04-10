param(
    [ValidateSet("bootstrap", "deploy-model", "full", "help")]
    [string]$Action = "help",
    [string]$Tag = "latest",
    [string]$RegistryPrefix = "",
    [string]$Namespace = "",
    [string]$ModelName = "",
    [string]$ModelVersion = "latest",
    [int]$Replicas = 1,
    [switch]$SkipBuild,
    [switch]$SkipGateway
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Show-Usage {
    Write-Host @"
Unified Kubernetes workflow wrapper.

Usage:
  .\k8s\workflow.ps1 -Action bootstrap [options]
  .\k8s\workflow.ps1 -Action deploy-model -ModelName <name> [options]
  .\k8s\workflow.ps1 -Action full -ModelName <name> [options]

Actions:
  bootstrap      Build shared images (optional) and deploy/update gateway
  deploy-model   Deploy/update one model service
  full           bootstrap + deploy-model

Options:
  -Tag <tag>                     Image tag for build_images (default: latest)
  -RegistryPrefix <prefix>       Registry prefix (example: registry.example.com/my-project)
  -Namespace <ns>                Kubernetes namespace (default: K8S_NAMESPACE or api-models)
  -ModelName <name>              Model name for deploy-model/full
  -ModelVersion <version>        Model version (default: latest)
  -Replicas <n>                  Replica count for model deploy (default: 1)
  -SkipBuild                     Skip image build step for bootstrap/full
  -SkipGateway                   Skip gateway deploy step for full

Examples:
  .\k8s\workflow.ps1 -Action bootstrap -Tag latest -Namespace api-models
  .\k8s\workflow.ps1 -Action deploy-model -ModelName tiny_demo_model -ModelVersion latest -Namespace api-models
  .\k8s\workflow.ps1 -Action full -ModelName tiny_demo_model -Tag v2 -RegistryPrefix registry.example.com/my-project -Namespace api-models
"@
}

if ([string]::IsNullOrWhiteSpace($Namespace)) {
    $Namespace = if ($env:K8S_NAMESPACE) { $env:K8S_NAMESPACE } else { "api-models" }
}

$scriptDir = Split-Path -Parent $PSCommandPath
$buildScript = Join-Path $scriptDir "build_images.ps1"
$deployGatewayScript = Join-Path $scriptDir "deploy_gateway.ps1"
$deployModelScript = Join-Path $scriptDir "deploy_model.ps1"

function Invoke-BuildImages {
    & $buildScript -Tag $Tag -RegistryPrefix $RegistryPrefix
    if ($LASTEXITCODE -ne 0) {
        throw "build_images.ps1 failed."
    }

    if (-not [string]::IsNullOrWhiteSpace($RegistryPrefix)) {
        $env:GATEWAY_IMAGE = "$RegistryPrefix/model-gateway:$Tag"
        $env:MODEL_SERVICE_IMAGE = "$RegistryPrefix/model-service:$Tag"
    }
}

function Invoke-DeployGateway {
    & $deployGatewayScript -Namespace $Namespace
    if ($LASTEXITCODE -ne 0) {
        throw "deploy_gateway.ps1 failed."
    }
}

function Invoke-DeployModel {
    if ([string]::IsNullOrWhiteSpace($ModelName)) {
        throw "-ModelName is required for action '$Action'."
    }

    & $deployModelScript -ModelName $ModelName -ModelVersion $ModelVersion -Namespace $Namespace -Replicas $Replicas
    if ($LASTEXITCODE -ne 0) {
        throw "deploy_model.ps1 failed."
    }
}

switch ($Action) {
    "bootstrap" {
        if (-not $SkipBuild) {
            Invoke-BuildImages
        }
        Invoke-DeployGateway
    }
    "deploy-model" {
        Invoke-DeployModel
    }
    "full" {
        if (-not $SkipBuild) {
            Invoke-BuildImages
        }
        if (-not $SkipGateway) {
            Invoke-DeployGateway
        }
        Invoke-DeployModel
    }
    "help" {
        Show-Usage
    }
    default {
        throw "Unknown action: $Action"
    }
}
