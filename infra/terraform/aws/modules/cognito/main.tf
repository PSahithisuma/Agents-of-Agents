# =============================================================================
# Cognito Module — Auth (replaces Keycloak Docker container)
# 50,000 MAU always free — no credit card needed after that limit
# =============================================================================

variable "env"     { type = string }
variable "app_url" { type = string; default = "" }

resource "aws_cognito_user_pool" "main" {
  name = "vibeforge-${var.env}"

  # Username = email
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_uppercase = true
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
  }

  # JWT tokens (same format our FastAPI already expects)
  schema {
    name                = "tenant_id"
    attribute_data_type = "String"
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 64
    }
  }

  schema {
    name                = "role"
    attribute_data_type = "String"
    mutable             = true
    string_attribute_constraints {
      min_length = 1
      max_length = 32
    }
  }

  # Email verification
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "VibeForge — Verify your email"
    email_message        = "Your verification code is {####}"
  }

  tags = { Env = var.env }
}

resource "aws_cognito_user_pool_client" "app" {
  name         = "vibeforge-app-${var.env}"
  user_pool_id = aws_cognito_user_pool.main.id

  # Allow authorization code flow (for Streamlit UI)
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["email", "openid", "profile"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]

  callback_urls = compact([
    "http://localhost:8501",
    var.app_url != "" ? "http://${var.app_url}" : ""
  ])
  logout_urls = compact([
    "http://localhost:8501",
    var.app_url != "" ? "http://${var.app_url}" : ""
  ])

  # Token validity
  access_token_validity  = 1    # 1 hour
  id_token_validity      = 1    # 1 hour
  refresh_token_validity = 30   # 30 days

  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "vibeforge-${var.env}"
  user_pool_id = aws_cognito_user_pool.main.id
}

output "user_pool_id"   { value = aws_cognito_user_pool.main.id }
output "client_id"      { value = aws_cognito_user_pool_client.app.id }
output "issuer_url"     { value = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.main.id}" }
output "jwks_uri"       { value = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.main.id}/.well-known/jwks.json" }
output "login_url"      { value = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.name}.amazoncognito.com/login" }

data "aws_region" "current" {}
