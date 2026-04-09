param(
    [string]$Namespace = ""
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

        Set-Item -Path ("Env:{0}" -f $key) -Value $value
    }
}

Import-DotEnv

if ([string]::IsNullOrWhiteSpace($Namespace)) {
    $Namespace = if ($env:K8S_NAMESPACE) { $env:K8S_NAMESPACE } else { "api-models" }
}

$gatewayImage = if ($env:GATEWAY_IMAGE) { $env:GATEWAY_IMAGE } else { "model-gateway:latest" }
$gatewayPort = if ($env:GATEWAY_PORT) { [int]$env:GATEWAY_PORT } else { 8000 }
$modelServicePrefix = if ($env:MODEL_SERVICE_PREFIX) { $env:MODEL_SERVICE_PREFIX } else { "model" }
$modelUrlTemplate = if ($env:MODEL_URL_TEMPLATE) { $env:MODEL_URL_TEMPLATE } else { "http://{service_name}:3000" }

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "Error: kubectl is not installed or not in PATH."
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

$manifest = @"
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gateway-sa
  namespace: $Namespace
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: gateway-service-discovery
  namespace: $Namespace
rules:
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: gateway-service-discovery
  namespace: $Namespace
subjects:
  - kind: ServiceAccount
    name: gateway-sa
    namespace: $Namespace
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: gateway-service-discovery
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gateway
  namespace: $Namespace
  labels:
    app: gateway
spec:
  replicas: 1
  selector:
    matchLabels:
      app: gateway
  template:
    metadata:
      labels:
        app: gateway
    spec:
      serviceAccountName: gateway-sa
      containers:
        - name: gateway
          image: $gatewayImage
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 3000
          env:
            - name: MODEL_SERVICE_PREFIX
              value: "$modelServicePrefix"
            - name: MODEL_NAMESPACE
              value: "$Namespace"
            - name: MODEL_URL_TEMPLATE
              value: "$modelUrlTemplate"
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
  name: gateway
  namespace: $Namespace
  labels:
    app: gateway
spec:
  type: LoadBalancer
  selector:
    app: gateway
  ports:
    - name: http
      port: $gatewayPort
      targetPort: 3000
"@

$manifest | & kubectl apply -f - | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Error applying gateway manifest."
}

Write-Host ("Gateway deployed in namespace {0}." -f $Namespace)
Write-Host "If LoadBalancer is pending, use port-forward:"
Write-Host ("kubectl -n {0} port-forward svc/gateway 8000:{1}" -f $Namespace, $gatewayPort)
