# Every input the deployment takes. Named to match the settings they become, so
# filling .env.prod from `terraform output` is a copy rather than a lookup.

variable "name" {
  description = "Short name that prefixes every resource. Lowercase letters and digits."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,10}$", var.name))
    error_message = "name must be 3-11 characters, lowercase letters and digits, starting with a letter."
  }
}

variable "location" {
  description = "Where to deploy."
  type        = string
}

variable "resource_group_name" {
  description = "An existing resource group to deploy into."
  type        = string
}

variable "tenant_id" {
  description = "Tenant that issues the tokens. Defaults to the credential's tenant."
  type        = string
  default     = ""
}

variable "audience" {
  description = "Identifier URI of the API app, e.g. api://data-agent-service."
  type        = string
  default     = "api://data-agent-service"
}

variable "openmetadata_url" {
  description = "OpenMetadata base URL, e.g. https://your-org.getcollate.io."
  type        = string
}

variable "sources" {
  description = <<-DESC
    The warehouse sources this service may query, as the executor reads them.
    Passed through verbatim as DAS_SOURCES, so the shape is the shape documented
    in .env.example -- one definition of a source, read the same way locally and
    here.
  DESC
  type        = list(any)
}

variable "executor_image" {
  description = "Container image for the executor."
  type        = string
}

variable "executor_implementation" {
  description = "Which executor implementation the image is. Reported as a tag, not enforced."
  type        = string
  default     = "py"

  validation {
    condition     = contains(["py", "go"], var.executor_implementation)
    error_message = "executor_implementation must be py or go."
  }
}

variable "publisher_name" {
  description = "Publisher name API Management requires."
  type        = string
}

variable "publisher_email" {
  description = "Publisher email API Management requires."
  type        = string
}

variable "apim_sku" {
  description = "API Management tier. Consumption has no VNet and a cold start; Basic v2 is the smallest tier suitable for steady traffic."
  type        = string
  default     = "BasicV2_1"

  validation {
    condition     = can(regex("^(Consumption_0|BasicV2_[0-9]+|StandardV2_[0-9]+|PremiumV2_[0-9]+)$", var.apim_sku))
    error_message = "apim_sku must be Consumption_0, BasicV2_N, StandardV2_N or PremiumV2_N."
  }
}

variable "rate_calls" {
  description = "Calls per minute per caller before the gateway throttles."
  type        = number
  default     = 60
}

variable "manage_app_registration" {
  description = <<-DESC
    Whether Terraform declares the API app registration, its exposed scope and
    the federated credential.

    True needs directory permissions the resource-only path does not
    (Application.ReadWrite.OwnedBy, and Application Administrator to consent to
    the exposed scope). Set it false when the directory is administered
    separately, and pass api_app_client_id instead.
  DESC
  type        = bool
  default     = true
}

variable "api_app_client_id" {
  description = "An existing API app registration to use when manage_app_registration is false."
  type        = string
  default     = ""

  validation {
    condition     = var.api_app_client_id == "" || can(regex("^[0-9a-fA-F-]{36}$", var.api_app_client_id))
    error_message = "api_app_client_id must be a GUID, or empty."
  }
}
