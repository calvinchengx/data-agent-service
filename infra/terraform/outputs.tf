# The values .env.prod needs. Every one marked "<from deploy>" in
# .env.prod.example is produced here, so filling that file is a copy rather
# than a hunt through the portal.

output "apim_name" {
  description = "DAS_APIM_SERVICE"
  value       = azurerm_api_management.main.name
}

output "apim_gateway_url" {
  description = "DAS_APIM_BASE"
  value       = azurerm_api_management.main.gateway_url
}

output "apim_resource_id" {
  value = azurerm_api_management.main.id
}

output "executor_url" {
  description = "DAS_EXECUTOR_URL"
  value       = "https://${azurerm_container_app.executor.ingress[0].fqdn}"
}

output "executor_principal_id" {
  description = "The identity to grant on the warehouse and the catalog."
  value       = azurerm_user_assigned_identity.executor.principal_id
}

output "executor_client_id" {
  value = azurerm_user_assigned_identity.executor.client_id
}

output "vault_uri" {
  description = "DAS_KEYVAULT_URL"
  value       = azurerm_key_vault.main.vault_uri
}

output "issuer" {
  description = "DAS_ENTRA_ISSUER"
  value       = local.issuer
}

output "api_app_client_id" {
  description = "DAS_MIDDLE_TIER_CLIENT_ID. Declared here when manage_app_registration is true."
  value       = local.api_client_id
}

output "rate_calls_configured" {
  value = var.rate_calls
}
