<#
.SYNOPSIS
Builds gateway and model_service images and optionally pushes them.
#>

param(
    [string]$Tag = "latest",
    [string]$RegistryPrefix = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
$PSNativeCommandUseErrorActionPreference = $false

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Error: docker is required and is not in PATH."
}

if ([string]::IsNullOrWhiteSpace($RegistryPrefix)) {
    $gatewayImage = "model-gateway:$Tag"
    $modelServiceImage = "model-service:$Tag"
}
else {
    $gatewayImage = "$RegistryPrefix/model-gateway:$Tag"
    $modelServiceImage = "$RegistryPrefix/model-service:$Tag"
}

Write-Host "Building shared images using Dockerfiles..."

& docker build -t $gatewayImage ./gateway
if ($LASTEXITCODE -ne 0) {
    throw "Docker build failed for gateway."
}

& docker build -t $modelServiceImage ./model_service
if ($LASTEXITCODE -ne 0) {
    throw "Docker build failed for model_service."
}

function Push-IfNeeded {
    if ([string]::IsNullOrWhiteSpace($RegistryPrefix)) {
        return
    }

    Write-Host "Pushing images to registry..."
    & docker push $gatewayImage
    if ($LASTEXITCODE -ne 0) {
        throw "Docker push failed for $gatewayImage"
    }

    & docker push $modelServiceImage
    if ($LASTEXITCODE -ne 0) {
        throw "Docker push failed for $modelServiceImage"
    }
}

Push-IfNeeded

Write-Host "Built images:"
Write-Host ("  {0}" -f $gatewayImage)
Write-Host ("  {0}" -f $modelServiceImage)
Write-Host ""
Write-Host "Use these env vars before deploy scripts:"
Write-Host ('$env:GATEWAY_IMAGE="{0}"' -f $gatewayImage)
Write-Host ('$env:MODEL_SERVICE_IMAGE="{0}"' -f $modelServiceImage)
