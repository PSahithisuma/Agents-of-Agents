# =============================================================================
# VibeForge — GCP Dev Environment
# Deploys the full Phase 2+3 stack on Google Cloud Platform.
#
# Usage:
#   cd infra/terraform/environments/dev
#   terraform init
#   terraform apply
# =============================================================================

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
  }
  # Uncomment after creating GCS bucket for state:
  # backend "gcs" {
  #   bucket = "vibeforge-terraform-state"
  #   prefix = "dev"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

# ── VPC ───────────────────────────────────────────────────────────────────────
module "vpc" {
  source     = "../../modules/vpc"
  project_id = var.project_id
  region     = var.region
}

# ── Cloud SQL (PostgreSQL 16) ─────────────────────────────────────────────────
module "cloud_sql" {
  source        = "../../modules/cloud_sql"
  project_id    = var.project_id
  region        = var.region
  network_id    = module.vpc.network_id
  tier          = "db-f1-micro"    # smallest — ~$7/month
  db_password   = var.db_password
}

# ── Memorystore (Redis 7) ─────────────────────────────────────────────────────
module "memorystore" {
  source     = "../../modules/memorystore"
  project_id = var.project_id
  region     = var.region
  network_id = module.vpc.network_id
  memory_gb  = 1
}

# ── Cloud Storage Buckets ─────────────────────────────────────────────────────
module "cloud_storage" {
  source     = "../../modules/cloud_storage"
  project_id = var.project_id
  region     = var.region
  env        = "dev"
}

# ── Artifact Registry ─────────────────────────────────────────────────────────
module "artifact_registry" {
  source     = "../../modules/artifact_registry"
  project_id = var.project_id
  region     = var.region
}

# ── Cloud Build (QA Gate) ─────────────────────────────────────────────────────
module "cloud_build" {
  source     = "../../modules/cloud_build"
  project_id = var.project_id
  region     = var.region
}

# ── Cloud Run Services ────────────────────────────────────────────────────────
module "api" {
  source         = "../../modules/cloud_run"
  project_id     = var.project_id
  region         = var.region
  name           = "vibeforge-api"
  image          = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/api:latest"
  port           = 8000
  min_instances  = 0
  max_instances  = 5
  vpc_connector  = module.vpc.connector_id
  env_vars = {
    DATABASE_URL  = "postgresql://${var.db_user}:${var.db_password}@${module.cloud_sql.private_ip}:5432/vibeforge"
    REDIS_URL     = "redis://${module.memorystore.host}:6379"
    GCS_BUCKET    = module.cloud_storage.docs_bucket
    QDRANT_URL    = var.qdrant_url
    LITELLM_URL   = "https://${module.litellm.url}"
    RETRIEVAL_URL = "https://${module.retrieval.url}"
  }
}

module "worker" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-worker"
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/worker:latest"
  port          = 8080
  min_instances = 0
  max_instances = 3
  vpc_connector = module.vpc.connector_id
  env_vars = {
    DATABASE_URL = "postgresql://${var.db_user}:${var.db_password}@${module.cloud_sql.private_ip}:5432/vibeforge"
    REDIS_URL    = "redis://${module.memorystore.host}:6379"
  }
}

module "retrieval" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-retrieval"
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/retrieval:latest"
  port          = 8001
  min_instances = 0
  max_instances = 3
  vpc_connector = module.vpc.connector_id
  env_vars = {
    QDRANT_URL = var.qdrant_url
  }
}

module "ingestion" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-ingestion"
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/ingestion:latest"
  port          = 8080
  min_instances = 0
  max_instances = 2
  vpc_connector = module.vpc.connector_id
  env_vars = {
    QDRANT_URL  = var.qdrant_url
    GCS_BUCKET  = module.cloud_storage.docs_bucket
    DATABASE_URL = "postgresql://${var.db_user}:${var.db_password}@${module.cloud_sql.private_ip}:5432/vibeforge"
  }
}

module "litellm" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-litellm"
  image         = "ghcr.io/berriai/litellm:main-latest"
  port          = 4000
  min_instances = 0
  max_instances = 3
  vpc_connector = module.vpc.connector_id
  env_vars = {
    DATABASE_URL       = "postgresql://${var.db_user}:${var.db_password}@${module.cloud_sql.private_ip}:5432/litellm"
    LITELLM_MASTER_KEY = var.litellm_master_key
    VERTEX_PROJECT     = var.project_id
    VERTEX_LOCATION    = var.region
  }
}

module "ui" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-ui"
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/ui:latest"
  port          = 8501
  min_instances = 0
  max_instances = 3
  vpc_connector = module.vpc.connector_id
  env_vars = {
    API_BASE_URL   = "https://${module.api.url}"
    FIREBASE_WEB_CONFIG = var.firebase_web_config
  }
}

module "sandbox" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-sandbox"
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/sandbox:latest"
  port          = 8002
  min_instances = 0
  max_instances = 5
  vpc_connector = module.vpc.connector_id
  env_vars = {
    PROJECT_ID       = var.project_id
    ARTIFACTS_BUCKET = module.cloud_storage.artifacts_bucket
    DATABASE_URL     = "postgresql://${var.db_user}:${var.db_password}@${module.cloud_sql.private_ip}:5432/vibeforge"
    API_URL          = "https://${module.api.url}"
  }
}

module "delivery" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-delivery"
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/delivery:latest"
  port          = 8003
  min_instances = 0
  max_instances = 3
  vpc_connector = module.vpc.connector_id
  env_vars = {
    PROJECT_ID       = var.project_id
    ARTIFACTS_BUCKET = module.cloud_storage.artifacts_bucket
    DATABASE_URL     = "postgresql://${var.db_user}:${var.db_password}@${module.cloud_sql.private_ip}:5432/vibeforge"
    API_URL          = "https://${module.api.url}"
  }
}

module "capacity" {
  source        = "../../modules/cloud_run"
  project_id    = var.project_id
  region        = var.region
  name          = "vibeforge-capacity"
  image         = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker/capacity:latest"
  port          = 8004
  min_instances = 1     # always-on — scheduler must be available
  max_instances = 1
  vpc_connector = module.vpc.connector_id
  env_vars = {
    REDIS_URL       = "redis://${module.memorystore.host}:6379"
    DATABASE_URL    = "postgresql://${var.db_user}:${var.db_password}@${module.cloud_sql.private_ip}:5432/vibeforge"
    MAX_CONCURRENT_BUILDS  = "10"
    LITELLM_MAX_PARALLEL   = "5"
  }
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "api_url"      { value = "https://${module.api.url}" }
output "ui_url"       { value = "https://${module.ui.url}" }
output "sandbox_url"  { value = "https://${module.sandbox.url}" }
output "delivery_url" { value = "https://${module.delivery.url}" }
output "capacity_url" { value = "https://${module.capacity.url}" }
output "sql_ip"       { value = module.cloud_sql.private_ip }
output "redis_host"   { value = module.memorystore.host }
output "docs_bucket"  { value = module.cloud_storage.docs_bucket }
output "artifact_registry_docker" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker"
}
