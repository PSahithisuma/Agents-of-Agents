# =============================================================================
# Cloud Build Module — QA Gate pipeline (replaces Sandbox Runner)
#
# Cloud Build runs the full gate sequence per job:
#   compile → test (≥60% cov) → migration → smoke → semgrep → trivy → gitleaks
# =============================================================================

variable "project_id" { type = string }
variable "region"     { type = string }

# Service account for Cloud Build with minimal permissions
resource "google_service_account" "cloud_build" {
  account_id   = "vibeforge-cloud-build"
  display_name = "VibeForge Cloud Build SA"
  project      = var.project_id
}

# Cloud Build needs to read from Artifact Registry
resource "google_project_iam_member" "build_artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.cloud_build.email}"
}

# Cloud Build writes GateReport to Cloud Storage
resource "google_project_iam_member" "build_storage_writer" {
  project = var.project_id
  role    = "roles/storage.objectCreator"
  member  = "serviceAccount:${google_service_account.cloud_build.email}"
}

# Cloud Build logs go to Cloud Logging
resource "google_project_iam_member" "build_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.cloud_build.email}"
}

output "build_sa_email" { value = google_service_account.cloud_build.email }
