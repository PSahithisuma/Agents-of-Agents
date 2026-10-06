# =============================================================================
# Artifact Registry Module — replaces Nexus + verdaccio + devpi + Athens
#
# Repos:
#   vibeforge-docker  → all service container images
#   vibeforge-maven   → Maven Central proxy (replaces Nexus)
#   vibeforge-npm     → npmjs.org proxy (replaces verdaccio)
#   vibeforge-python  → PyPI proxy (replaces devpi)
#   vibeforge-go      → Go module proxy (replaces Athens)
# =============================================================================

variable "project_id" { type = string }
variable "region"     { type = string }

# Docker images — all service containers stored here
resource "google_artifact_registry_repository" "docker" {
  repository_id = "vibeforge-docker"
  format        = "DOCKER"
  location      = var.region
  project       = var.project_id
  description   = "VibeForge service container images"
}

# Maven — proxies Maven Central, caches for offline sandbox builds (Contract C18)
resource "google_artifact_registry_repository" "maven" {
  repository_id = "vibeforge-maven"
  format        = "MAVEN"
  location      = var.region
  project       = var.project_id
  description   = "Maven Central proxy — replaces Nexus"

  remote_repository_config {
    description = "Maven Central"
    maven_repository {
      public_repository = "MAVEN_CENTRAL"
    }
  }

  mode = "REMOTE_REPOSITORY"  # acts as transparent cache
}

# npm — proxies npmjs.org (replaces verdaccio)
resource "google_artifact_registry_repository" "npm" {
  repository_id = "vibeforge-npm"
  format        = "NPM"
  location      = var.region
  project       = var.project_id
  description   = "npm registry proxy — replaces verdaccio"

  remote_repository_config {
    description = "npmjs.org"
    npm_repository {
      public_repository = "NPMJS"
    }
  }

  mode = "REMOTE_REPOSITORY"
}

# Python — proxies PyPI (replaces devpi)
resource "google_artifact_registry_repository" "python" {
  repository_id = "vibeforge-python"
  format        = "PYTHON"
  location      = var.region
  project       = var.project_id
  description   = "PyPI proxy — replaces devpi"

  remote_repository_config {
    description = "PyPI"
    python_repository {
      public_repository = "PYPI"
    }
  }

  mode = "REMOTE_REPOSITORY"
}

# Go — proxies proxy.golang.org (replaces Athens)
resource "google_artifact_registry_repository" "go" {
  repository_id = "vibeforge-go"
  format        = "GO"
  location      = var.region
  project       = var.project_id
  description   = "Go module proxy — replaces Athens"

  remote_repository_config {
    description = "Go module proxy"
    go_repository {
      public_repository = "GO_PROXY"
    }
  }

  mode = "REMOTE_REPOSITORY"
}

output "docker_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/vibeforge-docker"
}
output "maven_repo" {
  value = "${var.region}-maven.pkg.dev/${var.project_id}/vibeforge-maven"
}
output "npm_repo" {
  value = "${var.region}-npm.pkg.dev/${var.project_id}/vibeforge-npm"
}
output "python_repo" {
  value = "${var.region}-python.pkg.dev/${var.project_id}/vibeforge-python"
}
output "go_repo" {
  value = "${var.region}-go.pkg.dev/${var.project_id}/vibeforge-go"
}
