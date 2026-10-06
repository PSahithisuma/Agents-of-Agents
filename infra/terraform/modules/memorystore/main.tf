# =============================================================================
# Memorystore Module — Redis 7, private IP
# Replaces: vibeforge-redis Docker container
# Used for: semantic cache, rate limits, Arq → Cloud Tasks bridge
# =============================================================================

variable "project_id" { type = string }
variable "region"     { type = string }
variable "network_id" { type = string }
variable "memory_gb"  { type = number; default = 1 }

resource "google_redis_instance" "vibeforge" {
  name           = "vibeforge-redis"
  tier           = "BASIC"      # no replication needed for dev
  memory_size_gb = var.memory_gb
  region         = var.region
  project        = var.project_id

  authorized_network = var.network_id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"
  redis_version      = "REDIS_7_0"

  redis_configs = {
    maxmemory-policy = "allkeys-lru"   # evict least-recently-used when full
  }
}

output "host" { value = google_redis_instance.vibeforge.host }
output "port" { value = google_redis_instance.vibeforge.port }
