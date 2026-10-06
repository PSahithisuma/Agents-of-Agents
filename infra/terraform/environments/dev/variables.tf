variable "project_id" {
  description = "GCP project ID"
  type        = string
  # Set this to your GCP project ID, e.g. 'vibeforge-dev-123456'
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"   # cheapest region, lowest latency for most users
}

variable "db_user" {
  description = "PostgreSQL username"
  type        = string
  default     = "vibeforge"
}

variable "db_password" {
  description = "PostgreSQL password — use GCP Secret Manager in prod"
  type        = string
  sensitive   = true
}

variable "qdrant_url" {
  description = "Qdrant Cloud cluster URL (get from cloud.qdrant.io free tier)"
  type        = string
  # e.g. "https://your-cluster.us-east4-0.gcp.cloud.qdrant.io:6333"
}

variable "qdrant_api_key" {
  description = "Qdrant Cloud API key"
  type        = string
  sensitive   = true
}

variable "litellm_master_key" {
  description = "LiteLLM proxy master key"
  type        = string
  sensitive   = true
  default     = "sk-vibeforge-litellm-dev-change-me"
}

variable "firebase_web_config" {
  description = "Firebase web config JSON string (from Firebase console)"
  type        = string
  sensitive   = true
}
