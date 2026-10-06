# =============================================================================
# VPC Module — Private network for Cloud Run, Cloud SQL, Memorystore
# All services communicate over private IPs — no public internet exposure
# =============================================================================

variable "project_id" { type = string }
variable "region"     { type = string }

resource "google_compute_network" "vibeforge" {
  name                    = "vibeforge-vpc"
  auto_create_subnetworks = false
  project                 = var.project_id
}

resource "google_compute_subnetwork" "vibeforge" {
  name          = "vibeforge-subnet"
  ip_cidr_range = "10.0.0.0/24"
  region        = var.region
  network       = google_compute_network.vibeforge.id
  project       = var.project_id
}

# VPC Connector — allows Cloud Run to reach Cloud SQL + Memorystore over private IP
resource "google_vpc_access_connector" "vibeforge" {
  name          = "vibeforge-connector"
  region        = var.region
  project       = var.project_id
  network       = google_compute_network.vibeforge.name
  ip_cidr_range = "10.8.0.0/28"   # small /28 required by connector
  min_throughput = 200
  max_throughput = 1000
}

output "network_id"   { value = google_compute_network.vibeforge.id }
output "subnet_id"    { value = google_compute_subnetwork.vibeforge.id }
output "connector_id" { value = google_vpc_access_connector.vibeforge.id }
