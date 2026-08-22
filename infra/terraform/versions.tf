# Providers, pinned.
#
# Two providers, doing two different jobs. `azurerm` declares the resources
# ARM owns. `azuread` declares the app registration, its identifier URI, its
# exposed scope and the federated credential — Microsoft Graph objects that
# ARM cannot express at all, and which the Bicep template this replaces had to
# leave to a runbook of `az ad` commands.
#
# That gap was not cosmetic: the two hardest identity defects in this project
# (an OBO exchange addressed to the wrong audience, and a missing
# `user_impersonation` scope on the SQL resource app) were both mistakes in
# steps a human typed. Declaring them is the reason to prefer this definition.

terraform {
  required_version = ">= 1.9, < 2.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.14"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.1"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is deliberately not configured here. `terraform init -backend-config`
  # supplies it, because where state lives is a property of the environment and
  # not of the system being described. docs/10-production.md has the storage
  # account recipe.
  backend "azurerm" {}
}

provider "azurerm" {
  features {
    key_vault {
      # Soft delete is on (90 days). Purging on destroy would defeat it, and a
      # vault holding the catalog bot's token is exactly what recovery exists
      # for.
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
  }
}

provider "azuread" {}
