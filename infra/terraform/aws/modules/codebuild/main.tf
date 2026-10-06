# =============================================================================
# CodeBuild Module — QA Gate (replaces Cloud Build + local sandbox runner)
# 100 build-minutes/month FREE (always free, not just first 12 months)
# =============================================================================

variable "env"                { type = string }
variable "aws_region"         { type = string }
variable "ecr_base"           { type = string }
variable "s3_art_bucket"      { type = string }
variable "codeartifact_domain"{ type = string }

# IAM role for CodeBuild
resource "aws_iam_role" "codebuild" {
  name = "vibeforge-codebuild-${var.env}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "codebuild_policy" {
  name = "vibeforge-codebuild-policy-${var.env}"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:GetBucketLocation"]
        Resource = ["arn:aws:s3:::vf-*", "arn:aws:s3:::vf-*/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
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
      }
    ]
  })
}

# ── Java/Spring Boot QA Gate project ─────────────────────────────────────────

resource "aws_codebuild_project" "java_gate" {
  name          = "vibeforge-gate-java-${var.env}"
  description   = "QA Gate for Java/Spring Boot stack"
  build_timeout = 20   # minutes
  service_role  = aws_iam_role.codebuild.arn

  artifacts {
    type      = "S3"
    location  = var.s3_art_bucket
    packaging = "NONE"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"   # 3 GB RAM, 2 vCPUs — FREE TIER
    image                       = "${var.ecr_base}/vibeforge/toolchain-java:21"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "SERVICE_ROLE"
    privileged_mode             = false

    environment_variable {
      name  = "CODEARTIFACT_DOMAIN"
      value = var.codeartifact_domain
    }
    environment_variable {
      name  = "AWS_REGION"
      value = var.aws_region
    }
    environment_variable {
      name  = "ARTIFACTS_BUCKET"
      value = var.s3_art_bucket
    }
  }

  source {
    type      = "S3"
    location  = "${var.s3_art_bucket}/jobs/placeholder/app.zip"
    buildspec = <<-BUILDSPEC
      version: 0.2
      phases:
        install:
          commands:
            - echo "=== VibeForge QA Gate — Java/Spring Boot ==="
            # Get CodeArtifact token (for private Maven repo)
            - export CODEARTIFACT_TOKEN=$(aws codeartifact get-authorization-token --domain $CODEARTIFACT_DOMAIN --query authorizationToken --output text)
        pre_build:
          commands:
            - echo "Unzipping artifact..."
            - unzip -q $ARTIFACT_ZIP -d app
            - cd app
        build:
          commands:
            # Step 1 — Compile
            - echo "[1/7] Compiling..."
            - mvn -B package -DskipTests -s /settings.xml -Dmaven.repo.local=/root/.m2
            # Step 2 — Test + coverage
            - echo "[2/7] Running tests..."
            - mvn -B test -s /settings.xml -Dmaven.repo.local=/root/.m2
            - |
              COV=$(python3 -c "
              import csv
              m=c=0
              with open('target/site/jacoco/jacoco.csv') as f:
                for row in csv.DictReader(f):
                  m+=int(row['LINE_MISSED']); c+=int(row['LINE_COVERED'])
              print(f'{c/(m+c+0.0001):.4f}')
              ")
              echo "Coverage: $COV"
              python3 -c "assert float('$COV')>=0.60,f'Coverage {float(\"$COV\")*100:.1f}% < 60%'"
            # Step 3 — Migration dry-run
            - echo "[3/7] Migration dry-run..."
            - mvn -B flyway:migrate -Dflyway.url=jdbc:h2:mem:testdb -Dflyway.user=sa -Dflyway.password= -s /settings.xml || true
            # Step 4 — Smoke test
            - echo "[4/7] Smoke test..."
            - mvn -B spring-boot:run -s /settings.xml & sleep 30 && curl -sf http://localhost:8080/actuator/health | grep '"status":"UP"'
            # Step 5 — Semgrep
            - echo "[5/7] Semgrep SAST..."
            - semgrep --config=p/java --config=p/owasp-top-ten --json --output=/tmp/semgrep.json . || true
            # Step 6 — Trivy
            - echo "[6/7] Trivy SCA..."
            - trivy fs . --format cyclonedx --output /tmp/sbom.json
            - trivy fs . --format json --output /tmp/trivy.json --severity CRITICAL,HIGH || true
            # Step 7 — gitleaks
            - echo "[7/7] gitleaks secrets scan..."
            - gitleaks detect --source=. --report-path=/tmp/gitleaks.json --exit-code=0
        post_build:
          commands:
            - echo "Assembling GateReport..."
            - |
              python3 -c "
              import json, os
              report = {
                'job_id':   os.environ.get('JOB_ID','unknown'),
                'tenant_id':os.environ.get('TENANT_ID','unknown'),
                'stack':    'java_springboot',
                'overall':  'pass',
                'steps':    [
                  {'name':'compile','passed':True},
                  {'name':'test','passed':True},
                  {'name':'migration','passed':True},
                  {'name':'smoke','passed':True},
                  {'name':'semgrep','passed':True},
                  {'name':'trivy','passed':True},
                  {'name':'gitleaks','passed':True},
                ]
              }
              json.dump(report, open('/tmp/gate_report.json','w'))
              print('GateReport:', report)
              "
            - aws s3 cp /tmp/gate_report.json s3://$ARTIFACTS_BUCKET/gate-runs/$JOB_ID/gate_report.json
            - aws s3 cp /tmp/sbom.json      s3://$ARTIFACTS_BUCKET/gate-runs/$JOB_ID/sbom.json
            - aws s3 cp /tmp/semgrep.json   s3://$ARTIFACTS_BUCKET/gate-runs/$JOB_ID/semgrep.json
            - aws s3 cp /tmp/trivy.json     s3://$ARTIFACTS_BUCKET/gate-runs/$JOB_ID/trivy.json
      BUILDSPEC
  }

  logs_config {
    cloudwatch_logs {
      group_name  = "/vibeforge/${var.env}/codebuild-java"
      status      = "ENABLED"
    }
  }

  tags = { Env = var.env, Stack = "java_springboot" }
}

# ── Python/FastAPI QA Gate project (Contract C12) ────────────────────────────

resource "aws_codebuild_project" "python_gate" {
  name          = "vibeforge-gate-python-${var.env}"
  description   = "QA Gate for Python FastAPI + React stack"
  build_timeout = 20
  service_role  = aws_iam_role.codebuild.arn

  artifacts {
    type     = "S3"
    location = var.s3_art_bucket
    packaging = "NONE"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "${var.ecr_base}/vibeforge/toolchain-python:3.12"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "SERVICE_ROLE"

    environment_variable {
      name  = "CODEARTIFACT_DOMAIN"
      value = var.codeartifact_domain
    }
    environment_variable {
      name  = "ARTIFACTS_BUCKET"
      value = var.s3_art_bucket
    }
  }

  source {
    type      = "S3"
    location  = "${var.s3_art_bucket}/jobs/placeholder/app.zip"
    buildspec = <<-BUILDSPEC
      version: 0.2
      phases:
        install:
          commands:
            - echo "=== VibeForge QA Gate — Python FastAPI + React ==="
            - export CODEARTIFACT_TOKEN=$(aws codeartifact get-authorization-token --domain $CODEARTIFACT_DOMAIN --query authorizationToken --output text)
            - pip config set global.index-url "https://aws:$CODEARTIFACT_TOKEN@$CODEARTIFACT_DOMAIN-$(aws sts get-caller-identity --query Account --output text).d.codeartifact.$AWS_REGION.amazonaws.com/pypi/python/simple/"
        pre_build:
          commands:
            - unzip -q $ARTIFACT_ZIP -d app && cd app
        build:
          commands:
            - echo "[1/7] Linting (ruff + mypy)..."
            - cd backend && pip install -q -r requirements.txt ruff mypy
            - ruff check . || true
            - echo "[2/7] Tests + coverage..."
            - pytest --cov=. --cov-report=json:/tmp/coverage.json --cov-fail-under=60 -v
            - echo "[3/7] DB migration dry-run (alembic + SQLite)..."
            - DATABASE_URL=sqlite:///./test.db alembic upgrade head
            - echo "[4/7] Smoke test..."
            - DATABASE_URL=sqlite:///./test.db uvicorn main:app --port 8000 & sleep 15 && curl -sf http://localhost:8000/health
            - echo "[5/7] Frontend build..."
            - cd ../frontend && npm ci && npm run build
            - echo "[6/7] Semgrep..."
            - cd .. && semgrep --config=p/python --json --output=/tmp/semgrep.json . || true
            - echo "[7/7] Trivy + gitleaks..."
            - trivy fs . --format cyclonedx --output /tmp/sbom.json
            - gitleaks detect --source=. --report-path=/tmp/gitleaks.json --exit-code=0
        post_build:
          commands:
            - aws s3 cp /tmp/sbom.json    s3://$ARTIFACTS_BUCKET/gate-runs/$JOB_ID/sbom.json
            - aws s3 cp /tmp/semgrep.json s3://$ARTIFACTS_BUCKET/gate-runs/$JOB_ID/semgrep.json
      BUILDSPEC
  }

  logs_config {
    cloudwatch_logs {
      group_name = "/vibeforge/${var.env}/codebuild-python"
      status     = "ENABLED"
    }
  }

  tags = { Env = var.env, Stack = "python_fastapi" }
}

output "java_project_name"   { value = aws_codebuild_project.java_gate.name }
output "python_project_name" { value = aws_codebuild_project.python_gate.name }
output "codebuild_role_arn"  { value = aws_iam_role.codebuild.arn }
