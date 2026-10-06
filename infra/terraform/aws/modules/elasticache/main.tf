# =============================================================================
# ElastiCache Module — Redis 7 (replaces vibeforge-redis Docker container)
# cache.t3.micro = FREE for 12 months on AWS Free Tier
# =============================================================================

variable "env"               { type = string }
variable "vpc_id"            { type = string }
variable "private_subnet_ids"{ type = list(string) }
variable "sg_app_id"         { type = string }

resource "aws_elasticache_subnet_group" "main" {
  name       = "vibeforge-redis-subnet-${var.env}"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "redis" {
  name   = "vibeforge-redis-${var.env}"
  vpc_id = var.vpc_id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [var.sg_app_id]
  }
  egress {
    from_port   = 0; to_port = 0; protocol = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "vibeforge-sg-redis-${var.env}" }
}

resource "aws_elasticache_cluster" "main" {
  cluster_id           = "vibeforge-redis-${var.env}"
  engine               = "redis"
  node_type            = "cache.t3.micro"   # FREE TIER
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  engine_version       = "7.0"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]

  tags = { Name = "vibeforge-redis-${var.env}" }
}

output "endpoint" {
  value = "${aws_elasticache_cluster.main.cache_nodes[0].address}:6379"
}
