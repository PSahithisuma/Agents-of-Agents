# =============================================================================
# S3 Module — 3 buckets (replaces MinIO Docker container)
# S3 = 5GB free for 12 months
# =============================================================================

variable "env"        { type = string }
variable "aws_region" { type = string }

resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  suffix = random_id.suffix.hex
}

resource "aws_s3_bucket" "docs" {
  bucket        = "vf-docs-${var.env}-${local.suffix}"
  force_destroy = true
  tags          = { Name = "vf-docs-${var.env}" }
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = "vf-artifacts-${var.env}-${local.suffix}"
  force_destroy = true
  tags          = { Name = "vf-artifacts-${var.env}" }
}

resource "aws_s3_bucket" "bundles" {
  bucket        = "vf-bundles-${var.env}-${local.suffix}"
  force_destroy = true
  tags          = { Name = "vf-bundles-${var.env}" }
}

# Block all public access (private buckets)
resource "aws_s3_bucket_public_access_block" "docs" {
  bucket                  = aws_s3_bucket.docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "bundles" {
  bucket                  = aws_s3_bucket.bundles.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lifecycle: auto-delete old docs after 90 days
resource "aws_s3_bucket_lifecycle_configuration" "docs" {
  bucket = aws_s3_bucket.docs.id
  rule {
    id     = "expire-old-docs"
    status = "Enabled"
    expiration { days = 90 }
    filter {}
  }
}

output "docs_bucket"      { value = aws_s3_bucket.docs.bucket }
output "artifacts_bucket" { value = aws_s3_bucket.artifacts.bucket }
output "bundles_bucket"   { value = aws_s3_bucket.bundles.bucket }

terraform {
  required_providers {
    random = { source = "hashicorp/random" }
  }
}
