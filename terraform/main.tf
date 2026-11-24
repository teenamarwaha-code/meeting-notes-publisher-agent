terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Enable required Google Cloud APIs
resource "google_project_service" "required_apis" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "dlp.googleapis.com",
    "aiplatform.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

# Artifact Registry Repository for Agent Container Images
resource "google_artifact_registry_repository" "agent_repo" {
  location      = var.region
  repository_id = "${var.service_name}-repo"
  description   = "Container repository for Enterprise Session Publisher ADK Agent"
  format        = "DOCKER"
  depends_on    = [google_project_service.required_apis]
}

# Least-Privilege Runtime Service Account
resource "google_service_account" "agent_runtime_sa" {
  account_id   = "${var.service_name}-sa"
  display_name = "Session Publisher ADK Agent Runtime SA"
}

# Managed Secrets in Google Cloud Secret Manager (values populated out-of-band)
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "gemini-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required_apis]
}

resource "google_secret_manager_secret" "workspace_oauth_json" {
  secret_id = "workspace-oauth-token-json"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required_apis]
}

resource "google_secret_manager_secret_iam_member" "gemini_secret_accessor" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "workspace_secret_accessor" {
  secret_id = google_secret_manager_secret.workspace_oauth_json.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

resource "google_project_iam_member" "trace_writer" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

resource "google_project_iam_member" "dlp_redactor" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.agent_runtime_sa.email}"
}

# Cloud Run v2 Service hosting the Multi-Agent Web UI & HITL API
resource "google_cloud_run_v2_service" "publisher_agent" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.agent_runtime_sa.email

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = var.container_image

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "USE_SECRET_MANAGER"
        value = "true"
      }
      env {
        name  = "HOST_ORGANIZER_EMAIL"
        value = var.host_organizer_email
      }
      env {
        name  = "REQUIRED_ATTENDEE_GROUP"
        value = var.required_attendee_group
      }
      env {
        name  = "TARGET_CHAT_SPACE_ID"
        value = var.target_chat_space_id
      }
      env {
        name  = "PRESERVE_DEFAULT_DRIVE_PERMISSIONS"
        value = "true"
      }
      env {
        name  = "REQUIRE_HITL_APPROVAL"
        value = "true"
      }
    }
  }

  depends_on = [google_project_service.required_apis]
}
