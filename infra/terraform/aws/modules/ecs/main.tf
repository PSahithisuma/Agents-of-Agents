# =============================================================================
# ECS Fargate Module — runs all containerized services
# Replaces every Docker container in docker-compose.yml
# =============================================================================

variable "env"               { type = string }
variable "aws_region"        { type = string }
variable "vpc_id"            { type = string }
variable "private_subnet_ids"{ type = list(string) }
variable "sg_app_id"         { type = string }
variable "alb_target_groups" { type = map(string) }
variable "ecr_base"          { type = string }

variable "db_host"        { type = string }
variable "db_password"    { type = string; sensitive = true }
variable "redis_endpoint" { type = string }
variable "s3_docs_bucket" { type = string }
variable "s3_art_bucket"  { type = string }
variable "qdrant_url"     { type = string }
variable "qdrant_api_key" { type = string; sensitive = true }
variable "litellm_key"    { type = string; sensitive = true }
variable "anthropic_api_key" { type = string; sensitive = true; default = "" }
variable "bedrock_region" { type = string }

locals {
  db_url = "postgresql://vibeforge:${var.db_password}@${var.db_host}/vibeforge"
  redis_url = "redis://${var.redis_endpoint}"
}

# ── ECS Cluster ───────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "vibeforge-${var.env}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = { Env = var.env }
}

# ── IAM — Task Execution Role (ECS pulls image + writes logs) ─────────────────

resource "aws_iam_role" "ecs_exec" {
  name = "vibeforge-ecs-exec-${var.env}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_exec_policy" {
  role       = aws_iam_role.ecs_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ── IAM — Task Role (the container's own permissions) ─────────────────────────

resource "aws_iam_role" "ecs_task" {
  name = "vibeforge-ecs-task-${var.env}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

# Allow tasks to: read/write S3, call Bedrock, use CodeArtifact, trigger CodeBuild
resource "aws_iam_role_policy" "ecs_task_policy" {
  name = "vibeforge-task-policy-${var.env}"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::vf-*", "arn:aws:s3:::vf-*/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["codeartifact:GetAuthorizationToken", "codeartifact:GetRepositoryEndpoint",
                    "codeartifact:ReadFromRepository"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sts:GetServiceBearerToken"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      }
    ]
  })
}

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "services" {
  for_each          = toset(["api", "worker", "retrieval", "ingestion", "ui", "litellm", "sandbox", "capacity"])
  name              = "/vibeforge/${var.env}/${each.value}"
  retention_in_days = 14
}

# ── Helper: ECS Service factory ───────────────────────────────────────────────
# (Each service = task definition + ECS service + ALB attachment)

locals {
  services = {
    api = {
      image       = "${var.ecr_base}/vibeforge/api:latest"
      port        = 8000
      cpu         = 256
      memory      = 512
      desired     = 1
      tg_key      = "api"
      env_vars    = {
        DATABASE_URL  = local.db_url
        REDIS_URL     = local.redis_url
        S3_BUCKET     = var.s3_docs_bucket
        QDRANT_URL    = var.qdrant_url
        QDRANT_API_KEY = var.qdrant_api_key
        LITELLM_URL   = "http://localhost:4000"
        AWS_REGION    = var.aws_region
      }
    }
    worker = {
      image    = "${var.ecr_base}/vibeforge/worker:latest"
      port     = 8080
      cpu      = 256
      memory   = 512
      desired  = 1
      tg_key   = null
      env_vars = {
        DATABASE_URL = local.db_url
        REDIS_URL    = local.redis_url
        AWS_REGION   = var.aws_region
      }
    }
    retrieval = {
      image    = "${var.ecr_base}/vibeforge/retrieval:latest"
      port     = 8001
      cpu      = 256
      memory   = 512
      desired  = 1
      tg_key   = "retrieval"
      env_vars = {
        QDRANT_URL     = var.qdrant_url
        QDRANT_API_KEY = var.qdrant_api_key
      }
    }
    ingestion = {
      image    = "${var.ecr_base}/vibeforge/ingestion:latest"
      port     = 8080
      cpu      = 512
      memory   = 1024
      desired  = 1
      tg_key   = null
      env_vars = {
        QDRANT_URL   = var.qdrant_url
        S3_BUCKET    = var.s3_docs_bucket
        DATABASE_URL = local.db_url
        AWS_REGION   = var.aws_region
      }
    }
    ui = {
      image    = "${var.ecr_base}/vibeforge/ui:latest"
      port     = 8501
      cpu      = 256
      memory   = 512
      desired  = 1
      tg_key   = "ui"
      env_vars = {
        API_BASE_URL = "http://vibeforge-alb-${var.env}"
        AWS_REGION   = var.aws_region
      }
    }
    litellm = {
      image    = "${var.ecr_base}/vibeforge/litellm:latest"
      port     = 4000
      cpu      = 512
      memory   = 1024
      desired  = 1
      tg_key   = "litellm"
      env_vars = {
        LITELLM_MASTER_KEY  = var.litellm_key
        DATABASE_URL        = "postgresql://vibeforge:${var.db_password}@${var.db_host}/litellm"
        AWS_REGION_NAME     = var.bedrock_region
        ANTHROPIC_API_KEY   = var.anthropic_api_key
      }
    }
    sandbox = {
      image    = "${var.ecr_base}/vibeforge/sandbox:latest"
      port     = 8002
      cpu      = 256
      memory   = 512
      desired  = 1
      tg_key   = "sandbox"
      env_vars = {
        DATABASE_URL     = local.db_url
        S3_ARTIFACTS_BUCKET = var.s3_art_bucket
        AWS_REGION       = var.aws_region
      }
    }
    capacity = {
      image    = "${var.ecr_base}/vibeforge/capacity:latest"
      port     = 8004
      cpu      = 256
      memory   = 512
      desired  = 1
      tg_key   = "capacity"
      env_vars = {
        REDIS_URL             = local.redis_url
        DATABASE_URL          = local.db_url
        MAX_CONCURRENT_BUILDS = "10"
        LITELLM_MAX_PARALLEL  = "5"
      }
    }
  }
}

# Task definitions
resource "aws_ecs_task_definition" "services" {
  for_each = local.services

  family                   = "vibeforge-${each.key}-${var.env}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.ecs_exec.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = each.key
    image     = each.value.image
    essential = true

    portMappings = [{
      containerPort = each.value.port
      protocol      = "tcp"
    }]

    environment = [
      for k, v in each.value.env_vars : { name = k, value = v }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/vibeforge/${var.env}/${each.key}"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -sf http://localhost:${each.value.port}/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 15
    }
  }])
}

# ECS Services
resource "aws_ecs_service" "services" {
  for_each = local.services

  name            = "vibeforge-${each.key}-${var.env}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.services[each.key].arn
  desired_count   = each.value.desired
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.sg_app_id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = each.value.tg_key != null ? [each.value.tg_key] : []
    content {
      target_group_arn = var.alb_target_groups[load_balancer.value]
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  depends_on = [aws_iam_role_policy_attachment.ecs_exec_policy]

  tags = { Env = var.env }
}

output "cluster_name" { value = aws_ecs_cluster.main.name }
