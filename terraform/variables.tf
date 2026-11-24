variable "project_id" {
  description = "Google Cloud Project ID"
  type        = string
}

variable "region" {
  description = "Google Cloud deployment region"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name"
  type        = string
  default     = "session-publisher-agent"
}

variable "container_image" {
  description = "Container image URI in Artifact Registry"
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "host_organizer_email" {
  description = "Calendar host email whose sessions qualify for recap publishing"
  type        = string
  default     = "organizer@example.com"
}

variable "required_attendee_group" {
  description = "Required Google Group attendee email filter"
  type        = string
  default     = "account-leads@example.com"
}

variable "target_chat_space_id" {
  description = "Target Google Chat space resource ID (spaces/<ID>)"
  type        = string
  default     = "spaces/AAAA_OFFICIAL_SPACE"
}
