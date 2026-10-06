# =============================================================================
# Cloud Storage Module — GCS Buckets
# Replaces: vibeforge-minio Docker container
#
# Buckets:
#   vf-docs-{env}       → uploaded documents (PDFs, DOCXs)
#   vf-artifacts-{env}  → build artifacts, SBOMs, GateReports
#   vf-bundles-{env}    → final delivery bundles (src.zip, runbook)
# =============================================================================

variable "project_id" { type = string }
variable "region"     { type = string }
variable "env"        { type = string; default = "dev" }

locals {
  location = var.region
}

resource "google_storage_bucket" "docs" {
  name          = "vf-docs-${var.env}-${var.project_id}"
  location      = local.location
  project       = var.project_id
  force_destroy = true   # allow destroy in dev

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition { age = 90 }   # delete unprocessed docs after 90 days
    action    { type = "Delete" }
  }
}

resource "google_storage_bucket" "artifacts" {
  name          = "vf-artifacts-${var.env}-${var.project_id}"
  location      = local.location
  project       = var.project_id
  force_destroy = true

  uniform_bucket_level_access = true
}

resource "google_storage_bucket" "bundles" {
  name          = "vf-bundles-${var.env}-${var.project_id}"
  location      = local.location
  project       = var.project_id
  force_destroy = true

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition { age = 365 }   # keep bundles for 1 year
    action    { type = "Delete" }
  }
}

output "docs_bucket"      { value = google_storage_bucket.docs.name }
output "artifacts_bucket" { value = google_storage_bucket.artifacts.name }
output "bundles_bucket"   { value = google_storage_bucket.bundles.name }
