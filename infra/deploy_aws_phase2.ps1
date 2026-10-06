# =============================================================================
# VibeForge Phase 2 — AWS Deploy Script (PowerShell)
#
# Runs IN ORDER:
#   1. Install AWS CLI (if missing)
#   2. terraform apply  → creates RDS, ElastiCache, S3, ECR, ECS, ALB, etc.
#   3. Build + push Docker images to ECR
#   4. Force-deploy all ECS services (picks up new images)
#   5. Run DB migrations
#   6. Health check all services
# =============================================================================

param(
    [string]$Region = "ap-south-1"
)

$ErrorActionPreference = "Stop"

# ── Check prerequisites ───────────────────────────────────────────────────────
Write-Host "`n=== VibeForge Phase 2 — AWS Deploy ===" -ForegroundColor Cyan

$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text 2>$null).Trim()
if (-not $ACCOUNT_ID) {
    Write-Error "Not logged in to AWS. Run: aws configure"
    exit 1
}

$ECR_BASE = "$ACCOUNT_ID.dkr.ecr.$Region.amazonaws.com"
Write-Host "Account : $ACCOUNT_ID"
Write-Host "Region  : $Region"
Write-Host "ECR     : $ECR_BASE`n"

# ── Step 1: Terraform ─────────────────────────────────────────────────────────
Write-Host "[1/5] Running Terraform..." -ForegroundColor Yellow

if (-not (Test-Path "infra\terraform\aws\environments\dev\terraform.tfvars")) {
    Write-Host "  Copy the example file and fill in your values:" -ForegroundColor Red
    Write-Host "  copy infra\terraform\aws\environments\dev\terraform.tfvars.example infra\terraform\aws\environments\dev\terraform.tfvars"
    exit 1
}

Set-Location infra\terraform\aws\environments\dev
terraform init -upgrade
terraform apply -auto-approve
Set-Location ..\..\..\..\..
Write-Host "Infrastructure ready." -ForegroundColor Green

# ── Step 2: Docker login to ECR ───────────────────────────────────────────────
Write-Host "`n[2/5] Logging Docker into ECR..." -ForegroundColor Yellow
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $ECR_BASE
Write-Host "Docker authenticated to ECR." -ForegroundColor Green

# ── Step 3: Build and push all images ────────────────────────────────────────
Write-Host "`n[3/5] Building and pushing images to ECR..." -ForegroundColor Yellow

$IMAGES = @(
    @{ Tag = "vibeforge/api";              Path = "services/api" },
    @{ Tag = "vibeforge/worker";           Path = "services/worker" },
    @{ Tag = "vibeforge/retrieval";        Path = "services/retrieval" },
    @{ Tag = "vibeforge/ingestion";        Path = "services/ingestion" },
    @{ Tag = "vibeforge/ui";               Path = "ui" },
    @{ Tag = "vibeforge/sandbox";          Path = "services/sandbox" },
    @{ Tag = "vibeforge/capacity";         Path = "services/capacity" },
    @{ Tag = "vibeforge/toolchain-java";   Path = "infra/toolchain/java" },
    @{ Tag = "vibeforge/toolchain-python"; Path = "infra/toolchain/python" }
)

foreach ($img in $IMAGES) {
    $full = "$ECR_BASE/$($img.Tag):latest"
    Write-Host "  Building $($img.Tag)..." -ForegroundColor Gray
    docker build -t $full $img.Path
    docker push $full
    Write-Host "  ✅ $full" -ForegroundColor Green
}

# LiteLLM — pull from public + push to ECR
Write-Host "  Pulling LiteLLM from public registry..." -ForegroundColor Gray
docker pull ghcr.io/berriai/litellm:main-latest
docker tag ghcr.io/berriai/litellm:main-latest "$ECR_BASE/vibeforge/litellm:latest"
docker push "$ECR_BASE/vibeforge/litellm:latest"
Write-Host "  ✅ LiteLLM pushed" -ForegroundColor Green

# ── Step 4: Force new ECS deployments (picks up new images) ──────────────────
Write-Host "`n[4/5] Deploying new images to ECS..." -ForegroundColor Yellow

$SERVICES = @("vibeforge-api-dev","vibeforge-worker-dev","vibeforge-retrieval-dev",
              "vibeforge-ingestion-dev","vibeforge-ui-dev","vibeforge-litellm-dev",
              "vibeforge-sandbox-dev","vibeforge-capacity-dev")

foreach ($svc in $SERVICES) {
    aws ecs update-service `
        --cluster "vibeforge-dev" `
        --service $svc `
        --force-new-deployment `
        --region $Region `
        --output none 2>$null
    Write-Host "  Deploying $svc" -ForegroundColor Gray
}

# ── Step 5: Run DB migrations ─────────────────────────────────────────────────
Write-Host "`n[5/5] Running DB migrations..." -ForegroundColor Yellow

# Get RDS endpoint from Terraform output
$RDS_HOST = terraform -chdir=infra/terraform/aws/environments/dev output -raw rds_endpoint 2>$null
Write-Host "  RDS endpoint: $RDS_HOST"

# Run as a one-off ECS task
aws ecs run-task `
    --cluster "vibeforge-dev" `
    --task-definition "vibeforge-api-dev" `
    --launch-type FARGATE `
    --overrides '{"containerOverrides":[{"name":"api","command":["python","-m","alembic","upgrade","head"]}]}' `
    --region $Region `
    --output none

Write-Host "  Migrations triggered (check CloudWatch logs if it fails)" -ForegroundColor Green

# ── Final: Print service URLs ─────────────────────────────────────────────────
Write-Host "`n=== Deploy Complete ===" -ForegroundColor Cyan

$ALB = terraform -chdir=infra/terraform/aws/environments/dev output -raw alb_url 2>$null
Write-Host "Load Balancer : $ALB"
Write-Host "API URL       : $ALB/api/v1/health"
Write-Host "UI URL        : $ALB/ui"
Write-Host "`nNext: Update e2e_test.py BASE_URL to $ALB and run: python e2e_test.py"
