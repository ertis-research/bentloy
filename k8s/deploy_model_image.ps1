param(
    [Parameter(Mandatory = $true)]
    [string]$ModelName,
    [Parameter(Mandatory = $true)]
    [string]$ModelImage,
    [string]$Namespace = "",
    [int]$Replicas = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

if ([string]::IsNullOrWhiteSpace($Namespace)) {
    $Namespace = if ($env:K8S_NAMESPACE) { $env:K8S_NAMESPACE } else { "api-models" }
}

$scriptDir = Split-Path -Parent $PSCommandPath
$deployScript = Join-Path $scriptDir "deploy_model.ps1"

$env:MODEL_SERVICE_IMAGE = $ModelImage
$env:MODEL_URI = "/models/model"

& $deployScript -ModelName $ModelName -ModelVersion "image" -Namespace $Namespace -Replicas $Replicas
if ($LASTEXITCODE -ne 0) {
    throw "deploy_model.ps1 failed."
}
