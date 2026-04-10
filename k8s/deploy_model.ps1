param(
    [Parameter(Mandatory = $true)]
    [string]$ModelName,
    [string]$ModelVersion = "latest",
    [string]$Namespace = "",
    [int]$Replicas = 1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
$PSNativeCommandUseErrorActionPreference = $false

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

function To-Slug {
    param([string]$Text)

    $slug = $Text.ToLowerInvariant().Replace("_", "-")
    $slug = [Regex]::Replace($slug, "[^a-z0-9-]+", "-")
    $slug = [Regex]::Replace($slug, "-+", "-")
    $slug = $slug.Trim("-")
    return $slug
}

function Test-ValidK8sName {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
}

function Test-ValidImageRef {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[a-z0-9./:_-]+$")
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

$modelSlug = To-Slug -Text $ModelName
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

$modelUriEnvBlock = ""
if (-not [string]::IsNullOrWhiteSpace($modelUri)) {
    $modelUriEnvBlock = @"
            - name: MODEL_URI
              value: "$modelUri"
"@
}

$imagePullSecretsBlock = ""
if (-not [string]::IsNullOrWhiteSpace($modelRegistrySecret)) {
    $imagePullSecretsBlock = @"
      imagePullSecrets:
        - name: $modelRegistrySecret
"@
}

$manifest = @"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $deployName
  namespace: $Namespace
  labels:
    app: model-service
    model-slug: $modelSlug
spec:
  replicas: $Replicas
  selector:
    matchLabels:
      app: model-service
      model-slug: $modelSlug
  template:
    metadata:
      labels:
        app: model-service
        model-slug: $modelSlug
    spec:
$imagePullSecretsBlock
      containers:
        - name: model-service
          image: $modelServiceImage
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 3000
          env:
            - name: MLFLOW_TRACKING_URI
              value: "$mlflowTrackingUri"
            - name: MODEL_NAME
              value: "$ModelName"
            - name: MODEL_VERSION
              value: "$ModelVersion"
$modelUriEnvBlock
          readinessProbe:
            httpGet:
              path: /readyz
              port: 3000
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /livez
              port: 3000
            initialDelaySeconds: 20
            periodSeconds: 15
---
apiVersion: v1
kind: Service
metadata:
  name: $serviceName
  namespace: $Namespace
  labels:
    app: model-service
    model-slug: $modelSlug
spec:
  type: ClusterIP
  selector:
    app: model-service
    model-slug: $modelSlug
  ports:
    - name: http
      port: 3000
      targetPort: 3000
"@

$manifest | & kubectl apply -f - | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Error applying deployment/service manifest."
}

Write-Host ("Model deployed: {0} ({1})" -f $ModelName, $ModelVersion)
Write-Host ("Namespace: {0}" -f $Namespace)
Write-Host ("Internal service URL: http://{0}.{1}.svc.cluster.local:3000" -f $serviceName, $Namespace)
Write-Host ("Call through gateway with model_name={0}" -f $ModelName)
