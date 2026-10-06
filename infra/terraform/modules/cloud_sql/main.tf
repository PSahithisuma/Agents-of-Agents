# =============================================================================
# Cloud SQL Module — PostgreSQL 16, private IP only
# Replaces: vibeforge-postgres Docker container
# =============================================================================

variable "project_id"  { type = string }
variable "region"      { type = string }
variable "network_id"  { type = string }
variable "tier"        { type = string; default = "db-f1-micro" }
variable "db_password" { type = string; sensitive = true }
variable "db_name"     { type = string; default = "vibeforge" }
variable "db_user"     { type = string; default = "vibeforge" }

resource "google_sql_database_instance" "vibeforge" {
  name             = "vibeforge-postgres"
  database_version = "POSTGRES_16"
  region           = var.region
  project          = var.project_id

  settings {
    tier              = var.tier
    availability_type = "ZONAL"    # single-zone (cheaper for dev)
    disk_size         = 20         # GB
    disk_autoresize   = true

    backup_configuration {
      enabled    = true
      start_time = "02:00"
    }

    ip_configuration {
      ipv4_enabled    = false   # NO public IP — private only
      private_network = var.network_id
    }

    database_flags {
      name  = "max_connections"
      value = "100"
    }
  }

  deletion_protection = false   # allow destroy in dev
}

# Main application database
resource "google_sql_database" "vibeforge" {
  name     = var.db_name
  instance = google_sql_database_instance.vibeforge.name
  project  = var.project_id
}

# LiteLLM sibling database (same as 00_create_dbs.sh did locally)
resource "google_sql_database" "litellm" {
  name     = "litellm"
  instance = google_sql_database_instance.vibeforge.name
  project  = var.project_id
}

# Langfuse sibling database
resource "google_sql_database" "langfuse" {
  name     = "langfuse"
  instance = google_sql_database_instance.vibeforge.name
  project  = var.project_id
}

resource "google_sql_user" "vibeforge" {
  name     = var.db_user
  instance = google_sql_database_instance.vibeforge.name
  password = var.db_password
  project  = var.project_id
}

output "private_ip"       { value = google_sql_database_instance.vibeforge.private_ip_address }
output "connection_name"  { value = google_sql_database_instance.vibeforge.connection_name }
