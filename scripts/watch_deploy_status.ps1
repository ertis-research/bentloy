param(
    [Parameter(Mandatory = $true)]
    [string]$OperationId,
    [string]$GatewayUrl = "http://localhost:8000",
    [int]$InitialIntervalSec = 2,
    [int]$MaxIntervalSec = 10,
    [int]$TimeoutSec = 1800
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Get-ProgressPercent {
    param(
        [string]$Status,
        [string]$Step
    )

    switch ($Status) {
        "queued" { return 5 }
        "building" {
            switch ($Step) {
                "create_job" { return 20 }
                "kaniko_push" { return 60 }
                default { return 40 }
            }
        }
        "pushing" { return 70 }
        "deploying" {
            switch ($Step) {
                "apply_resources" { return 80 }
                "rollout" { return 90 }
                default { return 85 }
            }
        }
        "ready" { return 100 }
        "failed" { return 100 }
        default { return 0 }
    }
}

if ($InitialIntervalSec -lt 1) {
    throw "InitialIntervalSec must be >= 1."
}
if ($MaxIntervalSec -lt $InitialIntervalSec) {
    throw "MaxIntervalSec must be >= InitialIntervalSec."
}
if ($TimeoutSec -lt 10) {
    throw "TimeoutSec must be >= 10."
}

$uri = "{0}/deploy_status" -f $GatewayUrl.TrimEnd("/")
$body = @{ operation_id = $OperationId } | ConvertTo-Json

$start = Get-Date
$interval = $InitialIntervalSec
$lastStatus = ""
$lastStep = ""
$lastUpdatedAt = ""

Write-Host ("Watching operation: {0}" -f $OperationId)
Write-Host ("Endpoint: {0}" -f $uri)

while ($true) {
    $elapsed = (Get-Date) - $start
    if ($elapsed.TotalSeconds -ge $TimeoutSec) {
        Write-Error ("Timeout after {0} seconds waiting for operation {1}" -f $TimeoutSec, $OperationId)
        exit 1
    }

    try {
        $response = Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Body $body
    }
    catch {
        Write-Warning ("deploy_status request failed: {0}" -f $_.Exception.Message)
        Start-Sleep -Seconds $interval
        if ($interval -lt $MaxIntervalSec) {
            $interval = [Math]::Min($interval + 1, $MaxIntervalSec)
        }
        continue
    }

    if ($response.error) {
        Write-Error ("API error: {0}" -f $response.error)
        exit 1
    }

    $status = [string]$response.status
    $step = [string]$response.step
    $updatedAt = [string]$response.updated_at

    $changed = (
        $status -ne $lastStatus -or
        $step -ne $lastStep -or
        $updatedAt -ne $lastUpdatedAt
    )

    if ($changed) {
        $progressPercent = Get-ProgressPercent -Status $status -Step $step
        $barFilled = [Math]::Floor($progressPercent / 5)
        $bar = "█" * $barFilled + "░" * (20 - $barFilled)
        Write-Host ("[{0}] {1,3}% {2} status={3} step={4}" -f (Get-Date -Format "HH:mm:ss"), $progressPercent, $bar, $status, $step)

        if ($response.image_ref) {
            Write-Host ("  image: {0}" -f $response.image_ref)
        }
        if ($response.k8s_job) {
            Write-Host ("  job:   {0}" -f $response.k8s_job)
        }
        if ($response.error) {
            Write-Host ("  error: {0}" -f $response.error)
        }

        $lastStatus = $status
        $lastStep = $step
        $lastUpdatedAt = $updatedAt
    }

    if ($status -eq "ready") {
        Write-Host "Deployment finished successfully."
        exit 0
    }

    if ($status -eq "failed") {
        Write-Error "Deployment failed."
        if ($response.error) {
            Write-Error ("Reason: {0}" -f $response.error)
        }
        exit 1
    }

    Start-Sleep -Seconds $interval
    if ($interval -lt $MaxIntervalSec) {
        $interval = [Math]::Min($interval + 1, $MaxIntervalSec)
    }
}
