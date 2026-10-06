# =============================================================================
# Cloud Run Module — reusable for ALL VibeForge services
# Replaces every individual Docker container in docker-compose.yml
# =============================================================================

variable "project_id"    { type = string }
variable "region"        { type = string }
variable "name"          { type = string }
variable "image"         { type = string }
variable "port"          { type = number; default = 8080 }
variable "min_instances" { type = number; default = 0 }
variable "max_instances" { type = number; default = 5 }
variable "vpc_connector" { type = string }
variable "env_vars"      { type = map(string); default = {} }
variable "cpu"           { type = string; default = "1" }
variable "memory"        { type = string; default = "512Mi" }

resource "google_cloud_run_v2_service" "service" {
  name     = var.name
  location = var.region
  project  = var.project_id

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    vpc_access {
      connector = var.vpc_connector
      egress    = "PRIVATE_RANGES_ONLY"  # private IPs go through VPC, internet direct
    }

    containers {
      image = var.image
      ports { container_port = var.port }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle = true   # throttle CPU when not handling requests (saves cost)
      }

      dynamic "env" {
        for_each = var.env_vars
        content {
          name  = env.key
          value = env.value
        }
      }

      liveness_probe {
        http_get { path = "/health" }
        initial_delay_seconds = 15
        period_seconds        = 30
        failure_threshold     = 3
      }
    }
  }
}

# Allow unauthenticated (public) access
# In prod: remove this and use Firebase Auth / IAP instead
resource "google_cloud_run_v2_service_iam_member" "public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "url" { value = google_cloud_run_v2_service.service.uri }
