# =============================================================================
# ALB Module — Application Load Balancer
# Single ALB routes traffic to all services by path prefix
# =============================================================================

variable "env"              { type = string }
variable "vpc_id"           { type = string }
variable "public_subnet_ids"{ type = list(string) }

resource "aws_lb" "main" {
  name               = "vibeforge-alb-${var.env}"
  internal           = false
  load_balancer_type = "application"
  subnets            = var.public_subnet_ids

  enable_deletion_protection = false

  tags = { Name = "vibeforge-alb-${var.env}" }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # Default → API
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ── Target Groups (one per service) ──────────────────────────────────────────

resource "aws_lb_target_group" "api" {
  name        = "vf-api-${var.env}"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"   # required for ECS Fargate

  health_check {
    path                = "/health"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_target_group" "ui" {
  name        = "vf-ui-${var.env}"
  port        = 8501
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path     = "/health"
    interval = 30
  }
}

resource "aws_lb_target_group" "retrieval" {
  name        = "vf-retrieval-${var.env}"
  port        = 8001
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check { path = "/health"; interval = 30 }
}

resource "aws_lb_target_group" "sandbox" {
  name        = "vf-sandbox-${var.env}"
  port        = 8002
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check { path = "/health"; interval = 30 }
}

resource "aws_lb_target_group" "capacity" {
  name        = "vf-capacity-${var.env}"
  port        = 8004
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check { path = "/health"; interval = 30 }
}

resource "aws_lb_target_group" "litellm" {
  name        = "vf-litellm-${var.env}"
  port        = 4000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check { path = "/health"; interval = 30 }
}

# ── Listener Rules (path-based routing) ──────────────────────────────────────

resource "aws_lb_listener_rule" "ui" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ui.arn
  }
  condition {
    path_pattern { values = ["/ui/*", "/ui"] }
  }
}

resource "aws_lb_listener_rule" "retrieval" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.retrieval.arn
  }
  condition {
    path_pattern { values = ["/retrieval/*"] }
  }
}

resource "aws_lb_listener_rule" "sandbox" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 30

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.sandbox.arn
  }
  condition {
    path_pattern { values = ["/sandbox/*"] }
  }
}

resource "aws_lb_listener_rule" "capacity" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 40

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.capacity.arn
  }
  condition {
    path_pattern { values = ["/capacity/*"] }
  }
}

resource "aws_lb_listener_rule" "litellm" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 50

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.litellm.arn
  }
  condition {
    path_pattern { values = ["/llm/*"] }
  }
}

output "alb_dns_name" { value = aws_lb.main.dns_name }
output "alb_arn"      { value = aws_lb.main.arn }

output "target_group_arns" {
  value = {
    api      = aws_lb_target_group.api.arn
    ui       = aws_lb_target_group.ui.arn
    retrieval= aws_lb_target_group.retrieval.arn
    sandbox  = aws_lb_target_group.sandbox.arn
    capacity = aws_lb_target_group.capacity.arn
    litellm  = aws_lb_target_group.litellm.arn
  }
}
