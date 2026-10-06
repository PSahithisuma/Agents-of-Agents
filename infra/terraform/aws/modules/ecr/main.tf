# =============================================================================
# ECR Module — Docker image registry (replaces Artifact Registry Docker repo)
# 500MB always free
# =============================================================================

variable "env" { type = string }

locals {
  repos = [
    "vibeforge/api",
    "vibeforge/worker",
    "vibeforge/retrieval",
    "vibeforge/ingestion",
    "vibeforge/ui",
    "vibeforge/litellm",
    "vibeforge/sandbox",
    "vibeforge/delivery",
    "vibeforge/capacity",
    "vibeforge/toolchain-java",
    "vibeforge/toolchain-python",
  ]
}

resource "aws_ecr_repository" "repos" {
  for_each             = toset(local.repos)
  name                 = each.value
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true   # free basic scanning
  }

  tags = { Env = var.env }
}

# Lifecycle: keep only last 5 images per repo (saves storage)
resource "aws_ecr_lifecycle_policy" "cleanup" {
  for_each   = aws_ecr_repository.repos
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

output "ecr_base_url" {
  value = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com"
}
