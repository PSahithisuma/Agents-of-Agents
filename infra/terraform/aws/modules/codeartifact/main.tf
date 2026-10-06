# =============================================================================
# CodeArtifact Module — dep mirrors (replaces Nexus/verdaccio/devpi/Athens)
# 2GB + 100K requests/month free for 12 months
# =============================================================================

variable "env" { type = string }

# One domain holds all repos
resource "aws_codeartifact_domain" "vibeforge" {
  domain = "vibeforge-${var.env}"
}

# Maven — mirrors Maven Central (Contract C18: offline builds)
resource "aws_codeartifact_repository" "maven" {
  repository = "maven"
  domain     = aws_codeartifact_domain.vibeforge.domain

  external_connections {
    external_connection_name = "public:maven-central"
  }
}

# npm — mirrors npmjs.org
resource "aws_codeartifact_repository" "npm" {
  repository = "npm"
  domain     = aws_codeartifact_domain.vibeforge.domain

  external_connections {
    external_connection_name = "public:npmjs"
  }
}

# PyPI — mirrors pypi.org
resource "aws_codeartifact_repository" "python" {
  repository = "python"
  domain     = aws_codeartifact_domain.vibeforge.domain

  external_connections {
    external_connection_name = "public:pypi"
  }
}

# NuGet (for .NET if needed later)
resource "aws_codeartifact_repository" "nuget" {
  repository = "nuget"
  domain     = aws_codeartifact_domain.vibeforge.domain

  external_connections {
    external_connection_name = "public:nuget-org"
  }
}

output "domain_name"    { value = aws_codeartifact_domain.vibeforge.domain }
output "domain_owner"   { value = aws_codeartifact_domain.vibeforge.owner }
output "maven_repo"     { value = aws_codeartifact_repository.maven.repository }
output "npm_repo"       { value = aws_codeartifact_repository.npm.repository }
output "python_repo"    { value = aws_codeartifact_repository.python.repository }
