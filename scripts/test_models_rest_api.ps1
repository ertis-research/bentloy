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

Write-Step "Comprobando readiness/liveness del gateway"
try {
    $ready = Invoke-JsonRequest -Method GET -Url "$GatewayUrl/readyz"
    Write-Host "readyz: $(To-PrettyJson $ready)"
}
catch {
    Write-Warning "No se pudo consultar /readyz: $($_.Exception.Message)"
}

try {
    $live = Invoke-JsonRequest -Method GET -Url "$GatewayUrl/livez"
    Write-Host "livez: $(To-PrettyJson $live)"
}
catch {
    Write-Warning "No se pudo consultar /livez: $($_.Exception.Message)"
}

Write-Step "Consultando /list_models"
try {
    $listResponse = Invoke-JsonRequest -Method POST -Url "$GatewayUrl/list_models" -BodyJson "{}"
    Write-Host (To-PrettyJson $listResponse)
}
catch {
    Write-Warning "No se pudo consultar /list_models: $($_.Exception.Message)"
}

if (-not [string]::IsNullOrWhiteSpace($ModelName) -and -not [string]::IsNullOrWhiteSpace($PayloadPath)) {
    Write-Step "Probando /predict para model_name='$ModelName' usando payload '$PayloadPath'"

    if (-not (Test-Path -LiteralPath $PayloadPath)) {
        throw "PayloadPath no existe: $PayloadPath"
    }

    $payloadRaw = Get-Content -Raw -LiteralPath $PayloadPath
    $payloadData = $payloadRaw | ConvertFrom-Json

    # Si el archivo ya contiene {model_name, data}, se usa tal cual.
    # Si contiene solo el body de "data", se envuelve.
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
    Write-Step "Sin prueba de /predict"
    Write-Host "Para probar predict: .\\scripts\\test_models_rest_api.ps1 -ModelName <nombre> -PayloadPath <archivo.json>"
}
