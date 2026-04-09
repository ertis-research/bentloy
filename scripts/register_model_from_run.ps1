param(
    [Parameter(Mandatory = $true)]
    [string]$ModelName,
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [string]$ArtifactPath = "model",
    [string]$TargetStage = "Production"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

function Invoke-MlflowApi {
    param(
        [string]$Method,
        [string]$Url,
        [hashtable]$Payload = @{}
    )

    $hasPayload = $Payload.Count -gt 0
    $body = if ($hasPayload) { $Payload | ConvertTo-Json -Compress -Depth 20 } else { "" }

    try {
        if ($hasPayload) {
            $resp = Invoke-RestMethod -Method $Method -Uri $Url -ContentType "application/json" -Body $body
        }
        else {
            $resp = Invoke-RestMethod -Method $Method -Uri $Url -ContentType "application/json"
        }
        return @{
            Ok = $true
            Data = $resp
        }
    }
    catch {
        $raw = $_.ErrorDetails.Message
        if ([string]::IsNullOrWhiteSpace($raw)) {
            $raw = $_.Exception.Message
        }

        try {
            $parsed = $raw | ConvertFrom-Json
        }
        catch {
            $parsed = @{ message = $raw }
        }

        return @{
            Ok = $false
            Data = $parsed
        }
    }
}

Import-DotEnv

$mlflowTrackingUri = if ($env:MLFLOW_TRACKING_URI) { $env:MLFLOW_TRACKING_URI } else { "" }
$k8sNamespace = if ($env:K8S_NAMESPACE) { $env:K8S_NAMESPACE } else { "api-models" }

if ([string]::IsNullOrWhiteSpace($mlflowTrackingUri)) {
    throw "Error: MLFLOW_TRACKING_URI is not set (in .env or environment)."
}

$baseUrl = ($mlflowTrackingUri.TrimEnd("/")) + "/api/2.0/mlflow"

$createModel = Invoke-MlflowApi -Method "POST" -Url "$baseUrl/registered-models/create" -Payload @{
    name = $ModelName
}

if ($createModel.Ok) {
    Write-Host ("Registered model created: {0}" -f $ModelName)
}
else {
    $errorCode = if ($createModel.Data.PSObject.Properties.Name -contains "error_code") { $createModel.Data.error_code } else { "" }
    if ($errorCode -eq "RESOURCE_ALREADY_EXISTS") {
        Write-Host ("Registered model already exists: {0}" -f $ModelName)
    }
    else {
        throw ("Error creating registered model: {0}" -f ($createModel.Data | ConvertTo-Json -Depth 20 -Compress))
    }
}

$sourceUri = "runs:/{0}/{1}" -f $RunId, $ArtifactPath
$createVersion = Invoke-MlflowApi -Method "POST" -Url "$baseUrl/model-versions/create" -Payload @{
    name = $ModelName
    source = $sourceUri
    run_id = $RunId
}

if (-not $createVersion.Ok -or -not $createVersion.Data.model_version.version) {
    throw ("Error creating model version: {0}" -f ($createVersion.Data | ConvertTo-Json -Depth 20 -Compress))
}

$modelVersion = [string]$createVersion.Data.model_version.version
Write-Host ("Model version created: {0} v{1}" -f $ModelName, $modelVersion)

$transition = Invoke-MlflowApi -Method "POST" -Url "$baseUrl/model-versions/transition-stage" -Payload @{
    name = $ModelName
    version = $modelVersion
    stage = $TargetStage
    archive_existing_versions = $true
}

if ($transition.Ok -and $transition.Data.model_version.current_stage -eq $TargetStage) {
    Write-Host ("Model transitioned to stage {0}: {1} v{2}" -f $TargetStage, $ModelName, $modelVersion)
}
else {
    Write-Warning ("Transition response: {0}" -f ($transition.Data | ConvertTo-Json -Depth 20 -Compress))
}

Write-Host "Done. You can now deploy with:"
Write-Host ("  .\\k8s\\deploy_model.ps1 {0} latest {1} 1" -f $ModelName, $k8sNamespace)
