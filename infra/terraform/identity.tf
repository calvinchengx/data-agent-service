# The identity chain, declared.
#
# Bicep could not express any of this: app registrations are Microsoft Graph
# objects, not ARM resources, so the template this replaces left them to a
# runbook. Both of the identity defects that cost the most time in this project
# were mistakes in those hand-typed steps, which is the argument for declaring
# them rather than describing them.

data "azurerm_client_config" "current" {}

locals {
  tenant_id = var.tenant_id != "" ? var.tenant_id : data.azurerm_client_config.current.tenant_id

  # Derived from the cloud, not written down: hostnames differ in sovereign
  # clouds, and hardcoding one turns a portable definition into a
  # public-cloud-only one.
  issuer        = "https://login.microsoftonline.com/${local.tenant_id}/v2.0"
  sql_audience  = "https://database.windows.net"
  api_client_id = var.manage_app_registration ? azuread_application.api[0].client_id : var.api_app_client_id

  tags = {
    application = "data-agent-service"
    managedBy   = "terraform"
  }
}

# A user-assigned identity, not a system-assigned one: it is federated to the
# API app registration below, and a user-assigned identity survives the
# container app being recreated, so that trust does not have to be rebuilt.
resource "azurerm_user_assigned_identity" "executor" {
  name                = "${var.name}-executor"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = local.tags
}

# ---------------------------------------------------------------- the app --
# The API app registration IS the middle tier. OBO addresses the user's token
# to this app, so the audience the agent asks for and the client_id that
# performs the exchange must be the same registration -- getting that wrong
# produces "wrong audience" at the exchange, which reads like a scope problem
# and is not.
resource "azuread_application" "api" {
  count = var.manage_app_registration ? 1 : 0

  display_name     = "${var.name} data-agent-service API"
  identifier_uris  = [var.audience]
  owners           = [data.azurerm_client_config.current.object_id]
  sign_in_audience = "AzureADMyOrg"

  api {
    requested_access_token_version = 2

    # The scope the agent requests. Its absence on the SQL resource app was a
    # real defect here: the token was issued, and the engine refused it at
    # sign-in, where it reads as an outage rather than as a missing scope.
    oauth2_permission_scope {
      id                         = random_uuid.access_as_user.result
      value                      = "access_as_user"
      type                       = "User"
      admin_consent_display_name = "Query governed data as the signed-in user"
      admin_consent_description  = "Allows the data agent to query governed sources on behalf of the signed-in user, with that user's own permissions."
      user_consent_display_name  = "Query governed data as you"
      user_consent_description   = "Allows the data agent to query data as you, with your own permissions."
      enabled                    = true
    }
  }

  # Confidential client: it performs the on-behalf-of exchange, which a public
  # client may not do.
  web {
    implicit_grant {
      access_token_issuance_enabled = false
      id_token_issuance_enabled     = false
    }
  }

  lifecycle {
    # The scope id must never change: consent is recorded against it, and a new
    # id silently revokes every existing grant.
    ignore_changes = [api[0].oauth2_permission_scope]
  }
}

resource "random_uuid" "access_as_user" {}

resource "azuread_service_principal" "api" {
  count = var.manage_app_registration ? 1 : 0

  client_id = azuread_application.api[0].client_id
  owners    = [data.azurerm_client_config.current.object_id]
}

# The federated credential: the executor's managed identity proves it is the
# API app WITHOUT a client secret. The Bicep path fell back to a secret in Key
# Vault because a runbook step is easy to skip; declared, it is the default.
#
# docs/upstream-issues.md #6 records that the local stack cannot exercise this
# -- the entra emulator does not implement federated client assertions -- so
# this is the one part of the definition that only real Azure can witness.
resource "azuread_application_federated_identity_credential" "executor" {
  count = var.manage_app_registration ? 1 : 0

  application_id = azuread_application.api[0].id
  display_name   = "${var.name}-executor-mi"
  description    = "The executor's user-assigned managed identity, acting as the API app."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://login.microsoftonline.com/${local.tenant_id}/v2.0"
  subject        = azurerm_user_assigned_identity.executor.principal_id
}
