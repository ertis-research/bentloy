param(
    [string]$GatewayUrl = "http://localhost:8000",
    [string]$ModelName = "",
    [string]$PayloadPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-JsonRequest {
    param(
        [string]$Method,
        [string]$Url,
        [string]$BodyJson = ""
    )

    if ([string]::IsNullOrWhiteSpace($BodyJson)) {
        return Invoke-RestMethod -Method $Method -Uri $Url -ContentType "application/json"
    }
    return Invoke-RestMethod -Method $Method -Uri $Url -ContentType "application/json" -Body $BodyJson
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function To-PrettyJson {
    param($Object)
    return ($Object | ConvertTo-Json -Depth 20)
}

Write-Step "Checking gateway readiness/liveness"
try {
    $ready = Invoke-JsonRequest -Method GET -Url "$GatewayUrl/readyz"
    Write-Host "readyz: $(To-PrettyJson $ready)"
}
catch {
    Write-Warning "Could not query /readyz: $($_.Exception.Message)"
}

try {
    $live = Invoke-JsonRequest -Method GET -Url "$GatewayUrl/livez"
    Write-Host "livez: $(To-PrettyJson $live)"
}
catch {
    Write-Warning "Could not query /livez: $($_.Exception.Message)"
}

Write-Step "Querying /list_models"
try {
    $listResponse = Invoke-JsonRequest -Method POST -Url "$GatewayUrl/list_models" -BodyJson "{}"
    Write-Host (To-PrettyJson $listResponse)
}
catch {
    Write-Warning "Could not query /list_models: $($_.Exception.Message)"
}

if (-not [string]::IsNullOrWhiteSpace($ModelName) -and -not [string]::IsNullOrWhiteSpace($PayloadPath)) {
    Write-Step "Testing /predict for model_name='$ModelName' using payload '$PayloadPath'"

    if (-not (Test-Path -LiteralPath $PayloadPath)) {
        throw "PayloadPath does not exist: $PayloadPath"
    }

    $payloadRaw = Get-Content -Raw -LiteralPath $PayloadPath
    $payloadData = $payloadRaw | ConvertFrom-Json

    # If file already contains {model_name, data}, use it as-is.
    # If file only contains the "data" body, wrap it.
    if ($payloadData.PSObject.Properties.Name -contains "model_name" -and $payloadData.PSObject.Properties.Name -contains "data") {
        $finalPayload = $payloadData
    }
    else {
        $finalPayload = @{
            model_name = $ModelName
            data = $payloadData
        }
    }

    $finalPayloadJson = $finalPayload | ConvertTo-Json -Depth 30

    try {
        $predictResponse = Invoke-JsonRequest -Method POST -Url "$GatewayUrl/predict" -BodyJson $finalPayloadJson
        Write-Host (To-PrettyJson $predictResponse)
    }
    catch {
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            Write-Error $_.ErrorDetails.Message
        }
        else {
            throw
        }
    }
}
else {
    Write-Step "Skipping /predict test"
    Write-Host "To test predict: .\\scripts\\test_models_rest_api.ps1 -ModelName <name> -PayloadPath <file.json>"
}
