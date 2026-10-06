# =============================================================================
# VibeForge — GCP Setup Script
# Run each section IN ORDER after gcloud auth login
# =============================================================================
#
# USAGE (PowerShell):
#   .\infra\gcp_setup.ps1
#
# What this does:
#   1. Creates a new GCP project vibeforge-dev
#   2. Enables all required APIs
#   3. Creates a service account for Terraform
#   4. Downloads the key for Terraform to use
# =============================================================================

$PROJECT_ID = "vibeforge-dev-$(Get-Random -Maximum 9999)"
$REGION     = "us-central1"
$ACCOUNT    = (gcloud config get-value account 2>$null)

Write-Host "`n=== VibeForge GCP Setup ===" -ForegroundColor Cyan
Write-Host "Account : $ACCOUNT"
Write-Host "Project : $PROJECT_ID"
Write-Host "Region  : $REGION`n"

# ── Step 1: Create project ────────────────────────────────────────────────────
Write-Host "[1/5] Creating GCP project $PROJECT_ID ..." -ForegroundColor Yellow
gcloud projects create $PROJECT_ID --name="VibeForge Dev"
gcloud config set project $PROJECT_ID

# ── Step 2: Link billing (REQUIRED for Cloud SQL, Cloud Run etc.) ─────────────
Write-Host "`n[2/5] Billing setup..." -ForegroundColor Yellow
Write-Host "Listing your billing accounts:" -ForegroundColor Gray
gcloud billing accounts list

$BILLING_ACCOUNT = Read-Host "Paste your Billing Account ID from above (format: XXXXXX-XXXXXX-XXXXXX)"
gcloud billing projects link $PROJECT_ID --billing-account=$BILLING_ACCOUNT

# ── Step 3: Enable all needed APIs ───────────────────────────────────────────
Write-Host "`n[3/5] Enabling GCP APIs (this takes ~2 minutes)..." -ForegroundColor Yellow
$APIS = @(
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "redis.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
    "pubsub.googleapis.com",
    "cloudtasks.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "aiplatform.googleapis.com",
    "compute.googleapis.com",
    "vpcaccess.googleapis.com",
    "servicenetworking.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "firebase.googleapis.com",
    "identitytoolkit.googleapis.com"
)
gcloud services enable ($APIS -join " ")
Write-Host "APIs enabled." -ForegroundColor Green

# ── Step 4: Create Terraform service account ──────────────────────────────────
Write-Host "`n[4/5] Creating Terraform service account..." -ForegroundColor Yellow
gcloud iam service-accounts create vibeforge-terraform `
    --display-name="VibeForge Terraform SA" `
    --project=$PROJECT_ID

gcloud projects add-iam-policy-binding $PROJECT_ID `
    --member="serviceAccount:vibeforge-terraform@$PROJECT_ID.iam.gserviceaccount.com" `
    --role="roles/owner"

# Download key for Terraform to use
gcloud iam service-accounts keys create ./infra/terraform/gcp-key.json `
    --iam-account="vibeforge-terraform@$PROJECT_ID.iam.gserviceaccount.com"

Write-Host "Key saved to infra/terraform/gcp-key.json" -ForegroundColor Green

# ── Step 5: Print terraform.tfvars ───────────────────────────────────────────
Write-Host "`n[5/5] Creating terraform.tfvars..." -ForegroundColor Yellow

$TFVARS = @"
project_id          = "$PROJECT_ID"
region              = "$REGION"
db_password         = "vibeforge_dev_secret_change_me"
qdrant_url          = "https://YOUR-CLUSTER.us-east4-0.gcp.cloud.qdrant.io:6333"
qdrant_api_key      = "YOUR-QDRANT-API-KEY"
litellm_master_key  = "sk-vibeforge-litellm-dev-change-me"
firebase_web_config = "{}"
"@

$TFVARS | Out-File -FilePath ".\infra\terraform\environments\dev\terraform.tfvars" -Encoding UTF8

Write-Host "`n=== DONE ===" -ForegroundColor Green
Write-Host "Project ID: $PROJECT_ID" -ForegroundColor Cyan
Write-Host "`nNext steps:"
Write-Host "  1. Go to cloud.qdrant.io → create free cluster → paste URL in terraform.tfvars"
Write-Host "  2. Edit infra\terraform\environments\dev\terraform.tfvars with your values"
Write-Host "  3. Run: cd infra\terraform\environments\dev && terraform init && terraform apply"
