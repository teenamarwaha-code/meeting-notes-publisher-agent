output "cloud_run_service_uri" {
  description = "HTTPS endpoint of the deployed Session Publisher ADK Agent"
  value       = google_cloud_run_v2_service.publisher_agent.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege IAM Service Account used by the agent container"
  value       = google_service_account.agent_runtime_sa.email
}

output "artifact_registry_repository" {
  description = "Artifact Registry Docker repository ID"
  value       = google_artifact_registry_repository.agent_repo.id
}
