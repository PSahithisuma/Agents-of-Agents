# =============================================================================
# RDS Module — PostgreSQL 16 (replaces vibeforge-postgres Docker container)
# db.t3.micro = FREE for 12 months on AWS Free Tier
# =============================================================================

variable "env"               { type = string }
variable "vpc_id"            { type = string }
variable "private_subnet_ids"{ type = list(string) }
variable "db_password"       { type = string; sensitive = true }
variable "sg_app_id"         { type = string }
variable "db_name"           { type = string; default = "vibeforge" }
variable "db_username"       { type = string; default = "vibeforge" }

resource "aws_db_subnet_group" "main" {
  name       = "vibeforge-db-subnet-${var.env}"
  subnet_ids = var.private_subnet_ids
  tags       = { Name = "vibeforge-db-subnet-${var.env}" }
}

resource "aws_security_group" "rds" {
  name   = "vibeforge-rds-${var.env}"
  vpc_id = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.sg_app_id]
  }
  egress {
    from_port   = 0; to_port = 0; protocol = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "vibeforge-sg-rds-${var.env}" }
}

resource "aws_db_instance" "main" {
  identifier             = "vibeforge-postgres-${var.env}"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.t3.micro"   # FREE TIER
  allocated_storage      = 20              # GB — free tier limit
  storage_type           = "gp2"
  db_name                = var.db_name
  username               = var.db_username
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false           # private only
  skip_final_snapshot    = true            # allow destroy in dev
  deletion_protection    = false
  backup_retention_period = 7              # 7 day backups
  multi_az               = false           # single AZ for dev (cheaper)

  tags = { Name = "vibeforge-rds-${var.env}" }
}

output "endpoint" { value = aws_db_instance.main.endpoint }
output "db_name"  { value = aws_db_instance.main.db_name }
output "username" { value = aws_db_instance.main.username }
