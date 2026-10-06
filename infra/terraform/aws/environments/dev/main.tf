# =============================================================================
# VibeForge — AWS Dev Environment
# One terraform apply deploys the entire Phase 2 stack on AWS
# =============================================================================

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ── VPC ───────────────────────────────────────────────────────────────────────
module "vpc" {
  source     = "../../modules/vpc"
  env        = var.env
  aws_region = var.aws_region
}

# ── RDS (PostgreSQL 16) ───────────────────────────────────────────────────────
module "rds" {
  source            = "../../modules/rds"
  env               = var.env
  vpc_id            = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  db_password       = var.db_password
  sg_app_id         = module.vpc.sg_app_id
}

# ── ElastiCache (Redis) ───────────────────────────────────────────────────────
module "elasticache" {
  source            = "../../modules/elasticache"
  env               = var.env
  vpc_id            = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  sg_app_id         = module.vpc.sg_app_id
}

# ── S3 Buckets ────────────────────────────────────────────────────────────────
module "s3" {
  source     = "../../modules/s3"
  env        = var.env
  aws_region = var.aws_region
}

# ── ECR (Docker image registry) ───────────────────────────────────────────────
module "ecr" {
  source = "../../modules/ecr"
  env    = var.env
}

# ── Cognito (Auth — replaces Keycloak) ───────────────────────────────────────
module "cognito" {
  source   = "../../modules/cognito"
  env      = var.env
  app_url  = module.alb.alb_dns_name
}

# ── CodeArtifact (Dep mirrors — replaces Nexus/verdaccio/devpi/Athens) ───────
module "codeartifact" {
  source = "../../modules/codeartifact"
  env    = var.env
}

# ── Application Load Balancer ─────────────────────────────────────────────────
module "alb" {
  source            = "../../modules/alb"
  env               = var.env
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnet_ids
}

# ── ECS Fargate Cluster ───────────────────────────────────────────────────────
module "ecs" {
  source             = "../../modules/ecs"
  env                = var.env
  aws_region         = var.aws_region
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  sg_app_id          = module.vpc.sg_app_id
  alb_target_groups  = module.alb.target_group_arns
  ecr_base           = module.ecr.ecr_base_url

  # Secrets / connection strings
  db_host        = module.rds.endpoint
  db_password    = var.db_password
  redis_endpoint = module.elasticache.endpoint
  s3_docs_bucket = module.s3.docs_bucket
  s3_art_bucket  = module.s3.artifacts_bucket
  qdrant_url     = var.qdrant_url
  qdrant_api_key = var.qdrant_api_key
  litellm_key    = var.litellm_master_key
  anthropic_api_key = var.anthropic_api_key
  bedrock_region = var.aws_region
}

# ── CodeBuild (QA Gate) ───────────────────────────────────────────────────────
module "codebuild" {
  source         = "../../modules/codebuild"
  env            = var.env
  aws_region     = var.aws_region
  ecr_base       = module.ecr.ecr_base_url
  s3_art_bucket  = module.s3.artifacts_bucket
  codeartifact_domain = module.codeartifact.domain_name
}

# ── Outputs ───────────────────────────────────────────────────────────────────
output "alb_url"         { value = "http://${module.alb.alb_dns_name}" }
output "rds_endpoint"    { value = module.rds.endpoint }
output "redis_endpoint"  { value = module.elasticache.endpoint }
output "ecr_base"        { value = module.ecr.ecr_base_url }
output "docs_bucket"     { value = module.s3.docs_bucket }
output "artifacts_bucket"{ value = module.s3.artifacts_bucket }
output "cognito_pool_id" { value = module.cognito.user_pool_id }
output "cognito_client_id"{ value = module.cognito.client_id }
