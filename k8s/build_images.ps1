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

function Build-WithDockerfile {
    Write-Host "Building images with Dockerfile fallback..."

    & docker build -t $gatewayImage ./gateway
    if ($LASTEXITCODE -ne 0) {
        throw "Docker build failed for gateway."
    }

    & docker build -t $modelServiceImage ./model_service
    if ($LASTEXITCODE -ne 0) {
        throw "Docker build failed for model_service."
    }
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

$forceDocker = ($env:FORCE_DOCKER_BUILD -eq "1")
$bentoMode = "none"

if (-not $forceDocker) {
    if (Get-Command bentoml -ErrorAction SilentlyContinue) {
        $bentoMode = "bentoml"
    }
    elseif (Get-Command uv -ErrorAction SilentlyContinue) {
        $bentoMode = "uv"
    }
}

function Invoke-Bento {
    param(
        [string]$ServiceDir,
        [string[]]$Args
    )

    Push-Location $ServiceDir
    try {
        if ($bentoMode -eq "bentoml") {
            $output = & bentoml @Args 2>&1
        }
        else {
            $output = & uv run bentoml @Args 2>&1
        }
        return @{
            ExitCode = $LASTEXITCODE
            Output = @($output)
        }
    }
    finally {
        Pop-Location
    }
}

function Extract-BentoTag {
    param([object[]]$OutputLines)

    $lines = @(
        $OutputLines |
        ForEach-Object { "$_".Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($lines.Count -eq 0) {
        return ""
    }
    $last = $lines[-1]
    return ($last -replace "^__tag__:", "").Trim()
}

if ($forceDocker -or $bentoMode -eq "none") {
    if ($bentoMode -eq "none" -and -not $forceDocker) {
        Write-Host "BentoML CLI not available."
    }
    Build-WithDockerfile
    Push-IfNeeded
}
else {
    try {
        Write-Host "Building Bentos..."

        $gwBuild = Invoke-Bento -ServiceDir "gateway" -Args @("build", "-f", "bentofile.yaml", "-o", "tag")
        $msBuild = Invoke-Bento -ServiceDir "model_service" -Args @("build", "-f", "bentofile.yaml", "-o", "tag")

        if ($gwBuild.ExitCode -ne 0 -or $msBuild.ExitCode -ne 0) {
            throw "Bento build failed."
        }

        $gatewayBentoTag = Extract-BentoTag -OutputLines $gwBuild.Output
        $modelServiceBentoTag = Extract-BentoTag -OutputLines $msBuild.Output

        if ([string]::IsNullOrWhiteSpace($gatewayBentoTag) -or [string]::IsNullOrWhiteSpace($modelServiceBentoTag)) {
            throw "Bento tags missing."
        }

        Write-Host "Containerizing Bentos..."
        $gwContainerize = Invoke-Bento -ServiceDir "." -Args @("containerize", $gatewayBentoTag, "-t", $gatewayImage)
        if ($gwContainerize.ExitCode -ne 0) {
            throw "Gateway containerize failed."
        }

        $msContainerize = Invoke-Bento -ServiceDir "." -Args @("containerize", $modelServiceBentoTag, "-t", $modelServiceImage)
        if ($msContainerize.ExitCode -ne 0) {
            throw "Model-service containerize failed."
        }

        Push-IfNeeded

        Write-Host "Built Bento tags:"
        Write-Host ("  {0}" -f $gatewayBentoTag)
        Write-Host ("  {0}" -f $modelServiceBentoTag)
    }
    catch {
        Write-Warning ("{0} Falling back to Dockerfile build." -f $_.Exception.Message)
        Build-WithDockerfile
        Push-IfNeeded
    }
}

Write-Host "Built images:"
Write-Host ("  {0}" -f $gatewayImage)
Write-Host ("  {0}" -f $modelServiceImage)
Write-Host ""
Write-Host "Use these env vars before deploy scripts:"
Write-Host ('$env:GATEWAY_IMAGE="{0}"' -f $gatewayImage)
Write-Host ('$env:MODEL_SERVICE_IMAGE="{0}"' -f $modelServiceImage)
