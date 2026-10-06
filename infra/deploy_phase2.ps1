# =============================================================================
# VibeForge Phase 2 — Full GCP Deploy Script
#
# Runs AFTER gcp_setup.ps1 and after terraform.tfvars is filled in.
#
# What it does (in order):
#   1. terraform init + apply  → creates all cloud resources
#   2. Build all Docker images
#   3. Push images to Artifact Registry
#   4. Deploy each service to Cloud Run
#   5. Run DB migrations on Cloud SQL
#   6. Verify all Cloud Run services are healthy
# =============================================================================

param(
    [string]$ProjectId = "",
    [string]$Region    = "us-central1"
)

# Auto-detect project if not passed
if (-not $ProjectId) {
    $ProjectId = (gcloud config get-value project 2>$null).Trim()
}

if (-not $ProjectId) {
    Write-Error "No GCP project set. Run: gcloud config set project YOUR_PROJECT_ID"
    exit 1
}

$REGISTRY = "$Region-docker.pkg.dev/$ProjectId/vibeforge-docker"

Write-Host "`n=== VibeForge Phase 2 GCP Deploy ===" -ForegroundColor Cyan
Write-Host "Project  : $ProjectId"
Write-Host "Region   : $Region"
Write-Host "Registry : $REGISTRY`n"

# ── Step 1: Terraform ─────────────────────────────────────────────────────────
Write-Host "[1/6] Running Terraform..." -ForegroundColor Yellow
Set-Location infra\terraform\environments\dev
terraform init -upgrade
terraform apply -auto-approve
Set-Location ..\..\..\..

Write-Host "Infrastructure created." -ForegroundColor Green

# ── Step 2: Configure Docker for Artifact Registry ───────────────────────────
Write-Host "`n[2/6] Configuring Docker auth for Artifact Registry..." -ForegroundColor Yellow
gcloud auth configure-docker "$Region-docker.pkg.dev" --quiet

# ── Step 3: Build + Push Docker images ───────────────────────────────────────
Write-Host "`n[3/6] Building and pushing service images..." -ForegroundColor Yellow

$SERVICES = @(
    @{ Name = "api";       Path = "services/api";       Tag = "api" },
    @{ Name = "worker";    Path = "services/worker";     Tag = "worker" },
    @{ Name = "retrieval"; Path = "services/retrieval";  Tag = "retrieval" },
    @{ Name = "ingestion"; Path = "services/ingestion";  Tag = "ingestion" },
    @{ Name = "ui";        Path = "ui";                  Tag = "ui" },
    @{ Name = "litellm";   Path = $null;                 Tag = "litellm"; Image = "ghcr.io/berriai/litellm:main-latest" },
    @{ Name = "sandbox";   Path = "services/sandbox";    Tag = "sandbox" },
    @{ Name = "delivery";  Path = "services/delivery";   Tag = "delivery" },
    @{ Name = "capacity";  Path = "services/capacity";   Tag = "capacity" }
)

foreach ($svc in $SERVICES) {
    $tag = "$REGISTRY/$($svc.Tag):latest"
    Write-Host "  Building $($svc.Name)..." -ForegroundColor Gray

    if ($svc.Image) {
        # External image — just re-tag and push
        docker pull $svc.Image
        docker tag  $svc.Image $tag
    } else {
        docker build -t $tag $svc.Path
    }

    docker push $tag
    Write-Host "  Pushed $tag" -ForegroundColor Green
}

# ── Step 4: Build + Push toolchain images ────────────────────────────────────
Write-Host "`n[4/6] Building toolchain images..." -ForegroundColor Yellow

docker build -t "$REGISTRY/toolchain-java:21"     infra/toolchain/java/
docker push     "$REGISTRY/toolchain-java:21"

docker build -t "$REGISTRY/toolchain-python:3.12" infra/toolchain/python/
docker push     "$REGISTRY/toolchain-python:3.12"

Write-Host "Toolchain images pushed." -ForegroundColor Green

# ── Step 5: Run DB migrations ─────────────────────────────────────────────────
Write-Host "`n[5/6] Running database migrations..." -ForegroundColor Yellow
# Get Cloud SQL connection string from Terraform output
$SQL_IP = terraform -chdir=infra/terraform/environments/dev output -raw sql_ip 2>$null
Write-Host "Cloud SQL IP: $SQL_IP"

# Run migrations via Cloud Run job (one-shot)
gcloud run jobs create vibeforge-migrate `
    --image="$REGISTRY/api:latest" `
    --region=$Region `
    --project=$ProjectId `
    --command="python" `
    --args="-m,alembic,upgrade,head" `
    --set-env-vars="DATABASE_URL=postgresql://vibeforge:vibeforge_dev_secret_change_me@$SQL_IP/vibeforge" `
    2>$null

gcloud run jobs execute vibeforge-migrate --region=$Region --project=$ProjectId --wait

Write-Host "Migrations complete." -ForegroundColor Green

# ── Step 6: Health check all Cloud Run services ───────────────────────────────
Write-Host "`n[6/6] Verifying Cloud Run services..." -ForegroundColor Yellow

$CLOUD_RUN_SERVICES = @("vibeforge-api", "vibeforge-retrieval", "vibeforge-ui",
                         "vibeforge-sandbox", "vibeforge-capacity")

foreach ($svc in $CLOUD_RUN_SERVICES) {
    $url = gcloud run services describe $svc `
        --region=$Region --project=$ProjectId `
        --format="value(status.url)" 2>$null

    if ($url) {
        try {
            $resp = Invoke-WebRequest "$url/health" -UseBasicParsing -TimeoutSec 15
            if ($resp.StatusCode -eq 200) {
                Write-Host "  ✅ $svc  →  $url" -ForegroundColor Green
            } else {
                Write-Host "  ❌ $svc  →  HTTP $($resp.StatusCode)" -ForegroundColor Red
            }
        } catch {
            Write-Host "  ⚠  $svc  →  $url (not yet healthy)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  ⚠  $svc  →  not deployed yet" -ForegroundColor Yellow
    }
}

# ── Final output ──────────────────────────────────────────────────────────────
Write-Host "`n=== Phase 2 Deploy Complete ===" -ForegroundColor Cyan

$API_URL = gcloud run services describe vibeforge-api `
    --region=$Region --project=$ProjectId `
    --format="value(status.url)" 2>$null
$UI_URL = gcloud run services describe vibeforge-ui `
    --region=$Region --project=$ProjectId `
    --format="value(status.url)" 2>$null

Write-Host "API : $API_URL"
Write-Host "UI  : $UI_URL"
Write-Host "`nNext: Update e2e_test.py BASE URLs to these Cloud Run URLs and run tests."
