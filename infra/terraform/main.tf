# data-agent-service on Azure.
#
# The same shape the compose stack runs locally: a gateway in front of an
# executor that reaches a warehouse as the asking user. What changes is who
# provides each part -- API Management instead of the apim emulator, Container
# Apps instead of a compose service, Entra instead of the entra emulator -- and
# none of that is visible to the code, which is the point of the exercise.
#
# What this definition does NOT create, deliberately:
#
#   * **The Fabric workspace and warehouse.** Fabric items are created through
#     the Fabric REST API; seed/provision.py already does it, against real
#     Fabric as readily as against the emulator.
#   * **OpenMetadata.** Run the managed service, or your own instance. This
#     takes its URL as an input.
#   * **The APIs on the gateway.** seed/apim.py creates them through the same
#     ARM surface, against this service exactly as against the emulator -- one
#     definition of the gateway surface, exercised locally every day.
#     Declaring them again here would be the second definition that drifts.
#
# Everything here is idempotent: re-applying is how you change settings.

# ----------------------------------------------------------------- secrets --
resource "azurerm_key_vault" "main" {
  name                          = "${var.name}kv"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  tenant_id                     = local.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = false
  soft_delete_retention_days    = 90
  public_network_access_enabled = true
  tags                          = local.tags
}

# Key Vault Secrets User. The executor reads the catalog bot's token and any
# fallback credential with its own identity; nothing is passed in environment
# variables, which is the property that makes the local stack and this one the
# same design rather than the same diagram.
resource "azurerm_role_assignment" "executor_vault_reader" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.executor.principal_id
  principal_type       = "ServicePrincipal"
}

# --------------------------------------------------------- observability --
resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.name}-logs"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

resource "azurerm_application_insights" "main" {
  name                = "${var.name}-insights"
  location            = var.location
  resource_group_name = var.resource_group_name
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.main.id
  tags                = local.tags
}

# ----------------------------------------------------------------- compute --
resource "azurerm_container_app_environment" "main" {
  name                       = "${var.name}-env"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.tags
}

resource "azurerm_container_app" "executor" {
  name                         = "${var.name}-executor"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"
  tags                         = merge(local.tags, { implementation = var.executor_implementation })

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.executor.id]
  }

  # Internal only: the gateway is the way in. Nothing else should be able to
  # reach an endpoint that runs SQL on a user's behalf.
  ingress {
    external_enabled = false
    target_port      = 8090
    transport        = "http"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 1
    max_replicas = 10

    container {
      name   = "executor"
      image  = var.executor_image
      cpu    = 1.0
      memory = "2Gi"

      env {
        name  = "DAS_ENTRA_ISSUER"
        value = local.issuer
      }
      env {
        name  = "DAS_AGENT_AUDIENCE"
        value = var.audience
      }
      env {
        name  = "DAS_MIDDLE_TIER_CLIENT_ID"
        value = local.api_client_id
      }
      env {
        name  = "DAS_KEYVAULT_URL"
        value = azurerm_key_vault.main.vault_uri
      }
      env {
        name  = "DAS_SOURCES"
        value = jsonencode(var.sources)
      }
      env {
        name  = "DAS_SQL_AUDIENCE"
        value = local.sql_audience
      }
      env {
        name  = "DAS_SQL_SCOPE"
        value = "${local.sql_audience}/user_impersonation"
      }
      env {
        name  = "DAS_ROLE_SOURCE"
        value = "group"
      }
      env {
        name  = "DAS_REQUIRED_SCOPE"
        value = "access_as_user"
      }
      env {
        name  = "DAS_OM_URL"
        value = var.openmetadata_url
      }
      # Rules that deny by catalog tag (docs/00-plan.md §19). The bot token is
      # a reference the app resolves with its own managed identity, so the
      # secret is in the vault and its NAME is in the deployment.
      env {
        name  = "DAS_OM_BOT_TOKEN"
        value = "keyvault:${var.om_bot_secret_name}"
      }
      env {
        name  = "DAS_TAG_REFRESH_S"
        value = tostring(var.tag_refresh_seconds)
      }
      # The identity the platform injects; azure-identity and this service's
      # own credential module both discover it the same way.
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.executor.client_id
      }
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }
      # Absent on purpose: DAS_ENTRA_TLS_INSECURE. Real certificates.
    }
  }
}

# ----------------------------------------------------------------- gateway --
resource "azurerm_api_management" "main" {
  name                = "${var.name}-apim"
  location            = var.location
  resource_group_name = var.resource_group_name
  publisher_name      = var.publisher_name
  publisher_email     = var.publisher_email
  sku_name            = var.apim_sku
  tags                = local.tags

  identity {
    type = "SystemAssigned"
  }
}

# The gateway reads the catalog bot's token from Key Vault as a named value, so
# rotating it is a vault operation rather than a redeploy.
resource "azurerm_role_assignment" "apim_vault_reader" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_api_management.main.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_api_management_logger" "appinsights" {
  name                = "appinsights"
  api_management_name = azurerm_api_management.main.name
  resource_group_name = var.resource_group_name
  resource_id         = azurerm_application_insights.main.id

  application_insights {
    instrumentation_key = azurerm_application_insights.main.instrumentation_key
  }
}
