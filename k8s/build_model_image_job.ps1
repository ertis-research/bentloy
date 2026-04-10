param(
    [Parameter(Mandatory = $true)]
    [string]$ModelName,
    [string]$ModelVersion = "latest",
    [string]$Namespace = "",
    [string]$RunId = "",
    [string]$ModelUri = "",
    [string]$TargetImage = "",
    [switch]$InsecureRegistryTls,
    [switch]$Wait
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Normalize-RegistryServer {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }

    $candidate = $Value.Trim()
    if ($candidate -match "^[a-zA-Z][a-zA-Z0-9+.-]*://") {
        try {
            $uri = [Uri]$candidate
            if (-not [string]::IsNullOrWhiteSpace($uri.Host)) {
                if ($uri.IsDefaultPort) {
                    return $uri.Host
                }
                return ("{0}:{1}" -f $uri.Host, $uri.Port)
            }
        }
        catch {
        }
    }

    $candidate = ($candidate -replace "^[a-zA-Z][a-zA-Z0-9+.-]*://", "")
    return ($candidate -split "/", 2)[0].Trim()
}

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

function To-Slug {
    param([string]$Text)
    $slug = $Text.ToLowerInvariant().Replace("_", "-")
    $slug = [Regex]::Replace($slug, "[^a-z0-9-]+", "-")
    $slug = [Regex]::Replace($slug, "-+", "-")
    return $slug.Trim("-")
}

function Test-ValidK8sName {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
}

function Test-ValidImageRef {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[a-z0-9./:_-]+$")
}

function Test-ValidRegistryServer {
    param([string]$Value)
    return [Regex]::IsMatch($Value, "^[A-Za-z0-9.-]+(:[0-9]+)?$")
}

function Test-TrueLikeValue {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }

    switch ($Value.Trim().ToLowerInvariant()) {
        "1" { return $true }
        "true" { return $true }
        "yes" { return $true }
        default { return $false }
    }
}

Import-DotEnv

if ([string]::IsNullOrWhiteSpace($Namespace)) {
    $Namespace = if ($env:K8S_NAMESPACE) { $env:K8S_NAMESPACE } else { "api-models" }
}

$mlflowTrackingUri = if ($env:MLFLOW_TRACKING_URI) { $env:MLFLOW_TRACKING_URI } else { "" }
if ([string]::IsNullOrWhiteSpace($mlflowTrackingUri) -and [string]::IsNullOrWhiteSpace($ModelUri)) {
    throw "Set MLFLOW_TRACKING_URI or pass -ModelUri."
}

$registryServer = if ($env:REGISTRY_SERVER) { $env:REGISTRY_SERVER } else { "" }
$registryServer = Normalize-RegistryServer -Value $registryServer
$registryServer = $registryServer.ToLowerInvariant()
$tuOrg = if ($env:TU_ORG) { $env:TU_ORG } else { "" }
$registrySecret = if ($env:MODEL_REGISTRY_SECRET) { $env:MODEL_REGISTRY_SECRET } else { "model-registry-creds" }
$kanikoImage = if ($env:KANIKO_IMAGE) { $env:KANIKO_IMAGE } else { "gcr.io/kaniko-project/executor:v1.23.2-debug" }

if (-not [string]::IsNullOrWhiteSpace($registryServer) -and -not (Test-ValidRegistryServer -Value $registryServer)) {
    throw "REGISTRY_SERVER must be a registry host (example: registry.example.com or registry.example.com:5000)."
}

$skipTlsVerify = $InsecureRegistryTls.IsPresent
if (-not $skipTlsVerify) {
    $skipTlsVerify = Test-TrueLikeValue -Value (if ($env:KANIKO_SKIP_TLS_VERIFY) { $env:KANIKO_SKIP_TLS_VERIFY } else { "" })
}

$modelSlug = To-Slug -Text $ModelName
$modelVersionSlug = [Regex]::Replace($ModelVersion.ToLowerInvariant(), "[^a-z0-9._-]+", "-")

if ([string]::IsNullOrWhiteSpace($modelSlug)) {
    throw "ModelName produced an empty slug."
}
if (-not (Test-ValidK8sName -Value $modelSlug)) {
    throw "Model slug '$modelSlug' is not a valid Kubernetes label value."
}
if (-not (Test-ValidK8sName -Value $Namespace)) {
    throw "Namespace '$Namespace' is not a valid Kubernetes namespace name."
}
if (-not [string]::IsNullOrWhiteSpace($RunId) -and -not [Regex]::IsMatch($RunId, "^[A-Za-z0-9._-]{4,128}$")) {
    throw "Invalid -RunId format."
}

if ([string]::IsNullOrWhiteSpace($TargetImage)) {
    if ([string]::IsNullOrWhiteSpace($tuOrg)) {
        throw "Set TU_ORG or pass -TargetImage."
    }
    if ([string]::IsNullOrWhiteSpace($registryServer)) {
        throw "Set REGISTRY_SERVER or pass -TargetImage."
    }

    if (-not [string]::IsNullOrWhiteSpace($RunId)) {
        $runShort = if ($RunId.Length -gt 8) { $RunId.Substring(0, 8) } else { $RunId }
        $tag = "v{0}-r{1}" -f $modelVersionSlug, $runShort
    }
    else {
        $tag = "v{0}-t{1}" -f $modelVersionSlug, (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")
    }

    $TargetImage = "$registryServer/$tuOrg/model-${modelSlug}:$tag"
}

if (-not (Test-ValidImageRef -Value $TargetImage)) {
    throw "Target image '$TargetImage' has invalid characters."
}

$baseImage = if ($env:MODEL_BASE_IMAGE) { $env:MODEL_BASE_IMAGE } else { "" }
if ([string]::IsNullOrWhiteSpace($baseImage)) {
    if ([string]::IsNullOrWhiteSpace($tuOrg)) {
        throw "Set MODEL_BASE_IMAGE or TU_ORG."
    }
    if ([string]::IsNullOrWhiteSpace($registryServer)) {
        throw "Set MODEL_BASE_IMAGE or REGISTRY_SERVER."
    }
    $baseImage = "$registryServer/$tuOrg/model-service-base:py310"
}

$jobName = "build-model-$modelSlug-" + (Get-Date -Format "HHmmss")
$jobName = ([Regex]::Replace($jobName.ToLowerInvariant(), "[^a-z0-9-]+", "-")).Trim("-")
if ($jobName.Length -gt 63) {
    $jobName = $jobName.Substring(0, 63).TrimEnd("-")
}

$kanikoExtraArgs = New-Object System.Collections.Generic.List[string]
if ($skipTlsVerify) {
    [void]$kanikoExtraArgs.Add("--skip-tls-verify")
    [void]$kanikoExtraArgs.Add("--skip-tls-verify-pull")
    if (-not [string]::IsNullOrWhiteSpace($registryServer)) {
        [void]$kanikoExtraArgs.Add("--skip-tls-verify-registry=$registryServer")
    }
    Write-Warning "TLS verification is disabled for Kaniko registry operations."
}

$kanikoExtraArgsYaml = ""
if ($kanikoExtraArgs.Count -gt 0) {
    $kanikoExtraArgsYaml = "`n" + (($kanikoExtraArgs | ForEach-Object { "            - $_" }) -join "`n")
}

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

$null = & kubectl -n $Namespace get secret $registrySecret 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Secret '$registrySecret' not found in namespace '$Namespace'."
}

$manifest = @"
apiVersion: batch/v1
kind: Job
metadata:
  name: $jobName
  namespace: $Namespace
  labels:
    app: model-image-build
    model-slug: $modelSlug
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app: model-image-build
        model-slug: $modelSlug
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      imagePullSecrets:
        - name: $registrySecret
      volumes:
        - name: workspace
          emptyDir: {}
        - name: registry-creds
          secret:
            secretName: $registrySecret
            items:
              - key: .dockerconfigjson
                path: config.json
      initContainers:
        - name: prepare-build-context
          image: python:3.10-slim
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "2Gi"
          env:
            - name: MLFLOW_TRACKING_URI
              value: "$mlflowTrackingUri"
            - name: MODEL_NAME
              value: "$ModelName"
            - name: MODEL_VERSION
              value: "$ModelVersion"
            - name: MODEL_URI
              value: "$ModelUri"
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -euo pipefail
              python -m pip install --no-cache-dir mlflow==3.10.0 'PyYAML>=6.0'
              python - <<'PY'
              import json
              import os
              import re
              import shutil
              import textwrap
              from pathlib import Path
              import mlflow
              from mlflow.tracking import MlflowClient
              import yaml

              tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "").strip()
              model_name = os.environ["MODEL_NAME"]
              model_version = os.getenv("MODEL_VERSION", "latest").strip() or "latest"
              explicit_model_uri = os.getenv("MODEL_URI", "").strip()

              if tracking_uri:
                  mlflow.set_tracking_uri(tracking_uri)

              workspace = Path("/workspace/context")
              workspace.mkdir(parents=True, exist_ok=True)
              download_root = Path("/workspace/download")
              if download_root.exists():
                  shutil.rmtree(download_root)
              download_root.mkdir(parents=True, exist_ok=True)

              def resolve_model_uri() -> str:
                  if explicit_model_uri:
                      return explicit_model_uri
                  if model_version != "latest":
                      return f"models:/{model_name}/{model_version}"

                  client = MlflowClient()
                  latest_versions = client.get_registered_model(model_name).latest_versions
                  if not latest_versions:
                      raise ValueError(f"No registered versions for model '{model_name}'.")

                  production_versions = [
                      v.version
                      for v in latest_versions
                      if (v.current_stage or "").lower() == "production" and v.version
                  ]
                  if production_versions:
                      chosen_version = max(production_versions, key=lambda value: int(value))
                      return f"models:/{model_name}/{chosen_version}"

                  available_versions = [v.version for v in latest_versions if v.version]
                  chosen_version = max(available_versions, key=lambda value: int(value))
                  return f"models:/{model_name}/{chosen_version}"

              resolved_model_uri = resolve_model_uri()
              downloaded_path = Path(
                  mlflow.artifacts.download_artifacts(
                      artifact_uri=resolved_model_uri,
                      dst_path=str(download_root),
                  )
              )

              model_dst = workspace / "model"
              if model_dst.exists():
                  shutil.rmtree(model_dst)
              shutil.copytree(downloaded_path, model_dst)

              def package_key(requirement: str) -> str:
                  requirement = requirement.strip()
                  if not requirement:
                      return ""
                  token = re.split(r"[<>=!~\\[\\s]", requirement, maxsplit=1)[0]
                  return token.lower().replace("_", "-")

              def load_requirements() -> list[str]:
                  collected: list[str] = []

                  for path in (model_dst / "requirements.txt", model_dst / "pip_requirements.txt"):
                      if not path.exists():
                          continue
                      for raw_line in path.read_text(encoding="utf-8").splitlines():
                          line = raw_line.strip()
                          if line and not line.startswith("#"):
                              collected.append(line)

                  conda_path = model_dst / "conda.yaml"
                  if conda_path.exists():
                      data = yaml.safe_load(conda_path.read_text(encoding="utf-8")) or {}
                      for entry in data.get("dependencies", []):
                          if isinstance(entry, dict) and "pip" in entry:
                              for req in entry.get("pip") or []:
                                  line = str(req).strip()
                                  if line and not line.startswith("#"):
                                      collected.append(line)
                  return collected

              base_packages = {"bentoml", "mlflow", "numpy", "pandas"}
              filtered_requirements: list[str] = []
              seen_packages: set[str] = set()

              for requirement in load_requirements():
                  key = package_key(requirement)
                  if not key or key in base_packages or key in seen_packages:
                      continue
                  seen_packages.add(key)
                  filtered_requirements.append(requirement)

              requirements_path = workspace / "extra_requirements.txt"
              if filtered_requirements:
                  requirements_path.write_text("\n".join(filtered_requirements) + "\n", encoding="utf-8")
              else:
                  requirements_path.write_text("", encoding="utf-8")

              dockerfile_content = textwrap.dedent(
                  """\
                  ARG BASE_IMAGE
                  FROM `${BASE_IMAGE}
                  WORKDIR /app
                  COPY model /models/model
                  COPY extra_requirements.txt /tmp/extra_requirements.txt
                  RUN if [ -s /tmp/extra_requirements.txt ]; then pip install --no-cache-dir -r /tmp/extra_requirements.txt; fi
                  ENV MODEL_URI=/models/model
                  """
              )
              (workspace / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

              metadata = {
                  "model_name": model_name,
                  "model_version_requested": model_version,
                  "model_uri_resolved": resolved_model_uri,
                  "extra_requirements": filtered_requirements,
              }
              (workspace / "build_meta.json").write_text(
                  json.dumps(metadata, indent=2),
                  encoding="utf-8",
              )

              print("Prepared model build context at /workspace/context")
              print(json.dumps(metadata, indent=2))
              PY
          volumeMounts:
            - name: workspace
              mountPath: /workspace
      containers:
        - name: kaniko
          image: $kanikoImage
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2000m"
              memory: "4Gi"
          args:
            - --dockerfile=/workspace/context/Dockerfile
            - --context=dir:///workspace/context
            - --destination=$TargetImage
            - --build-arg=BASE_IMAGE=$baseImage
            - --snapshot-mode=redo
            - --use-new-run
$kanikoExtraArgsYaml
          volumeMounts:
            - name: workspace
              mountPath: /workspace
            - name: registry-creds
              mountPath: /kaniko/.docker
              readOnly: true
"@

$manifest | & kubectl apply -f - | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Error applying build job manifest."
}

Write-Host "Build Job submitted."
Write-Host ("Job: {0}" -f $jobName)
Write-Host ("Namespace: {0}" -f $Namespace)
Write-Host ("Target image: {0}" -f $TargetImage)
Write-Host ("Base image: {0}" -f $baseImage)
Write-Host ""
Write-Host "Check status:"
Write-Host ("  kubectl -n {0} get job {1}" -f $Namespace, $jobName)
Write-Host ("  kubectl -n {0} logs job/{1} -c prepare-build-context --tail=200" -f $Namespace, $jobName)
Write-Host ("  kubectl -n {0} logs job/{1} -c kaniko --tail=200" -f $Namespace, $jobName)

if ($Wait) {
    Write-Host ""
    Write-Host "Waiting for Job completion..."
    & kubectl -n $Namespace wait --for=condition=complete --timeout=1800s ("job/{0}" -f $jobName) | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Job did not complete successfully. Showing logs."
        & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c prepare-build-context --tail=200 | Out-Host
        & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c kaniko --tail=200 | Out-Host
        throw "Build Job failed."
    }

    Write-Host "Job completed successfully."
    & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c prepare-build-context --tail=200 | Out-Host
    & kubectl -n $Namespace logs ("job/{0}" -f $jobName) -c kaniko --tail=200 | Out-Host
}
