// data-agent-service on Azure.
//
// The same shape the compose stack runs locally: a gateway in front of an
// executor that reaches a warehouse as the asking user. What changes is who
// provides each part — API Management instead of the apim emulator, Container
// Apps instead of a compose service, Entra instead of the entra emulator — and
// none of that is visible to the code, which is the point of the exercise.
//
// What this template does NOT create, deliberately:
//
//   * **App registrations.** They are Microsoft Graph objects, not ARM
//     resources. `docs/10-production.md` creates them with `az ad`, because a
//     half-supported Bicep extension for the one security-critical step is
//     worse than an explicit command someone can read.
//   * **The Fabric workspace and warehouse.** Fabric items are created through
//     the Fabric REST API; `seed/provision.py` already does it, against real
//     Fabric as readily as against the emulator.
//   * **OpenMetadata.** Run the managed service, or your own instance. This
//     template takes its URL and the read-only bot's token as inputs.
//
// Everything here is idempotent: re-deploying is how you change settings.

targetScope = 'resourceGroup'

@description('Short name that prefixes every resource. Lowercase letters and digits.')
@minLength(3)
@maxLength(11)
param name string

@description('Where to deploy. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Tenant that issues the tokens. Defaults to the deployment tenant.')
param tenantId string = subscription().tenantId

@description('The API app registration created in the runbook. Its identifier URI is the audience.')
param apiAppClientId string

@description('Identifier URI of the API app, e.g. api://data-agent-service.')
param audience string

@description('OpenMetadata base URL, e.g. https://your-org.getcollate.io.')
param openMetadataUrl string

@description('The warehouse sources this service may query, as the executor reads them.')
param sources array

@description('Container image for the executor.')
param executorImage string

@description('Which executor implementation the image is (py or go). Reported, not enforced.')
@allowed(['py', 'go'])
param executorImplementation string = 'py'

@description('Publisher details API Management requires.')
param publisherName string
param publisherEmail string

@description('API Management tier. Consumption has no VNet and a cold start; Basic v2 is the smallest tier suitable for steady traffic.')
@allowed(['Consumption', 'Basicv2', 'Standardv2', 'Premiumv2'])
param apimSku string = 'Basicv2'

@description('Calls per minute per caller before the gateway throttles.')
param rateCalls int = 60

var tags = { application: 'data-agent-service', managedBy: 'bicep' }
// Derived from the cloud being deployed to rather than written down: the
// hostnames differ in sovereign clouds, and a hardcoded one turns a portable
// template into a public-cloud-only one.
var issuer = '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
var sqlHost = substring(environment().suffixes.sqlServerHostname, 1)
var sqlAudience = 'https://${sqlHost}'

// ---------------------------------------------------------------- identity --
// A user-assigned identity, not a system-assigned one: it is federated to the
// API app registration in the runbook, and a user-assigned identity survives
// the container app being recreated, so that trust does not have to be redone.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-executor'
  location: location
  tags: tags
}

// ----------------------------------------------------------------- secrets --
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${name}kv'
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
  }
}

// Key Vault Secrets User. The executor reads the catalog bot's token and any
// fallback credential with its own identity; nothing is passed in environment
// variables, which is the property that makes the local stack and this one the
// same design rather than the same diagram.
resource vaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, 'secrets-user')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// --------------------------------------------------------- observability --
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${name}-insights'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ----------------------------------------------------------------- compute --
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource executor 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${name}-executor'
  location: location
  tags: union(tags, { implementation: executorImplementation })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      // Internal only: the gateway is the way in. Nothing else should be able
      // to reach an endpoint that runs SQL on a user's behalf.
      ingress: {
        external: false
        targetPort: 8090
        transport: 'http'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'executor'
          image: executorImage
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'DAS_ENTRA_ISSUER', value: issuer }
            { name: 'DAS_AGENT_AUDIENCE', value: audience }
            { name: 'DAS_MIDDLE_TIER_CLIENT_ID', value: apiAppClientId }
            { name: 'DAS_KEYVAULT_URL', value: vault.properties.vaultUri }
            { name: 'DAS_SOURCES', value: string(sources) }
            { name: 'DAS_SQL_AUDIENCE', value: sqlAudience }
            { name: 'DAS_SQL_SCOPE', value: '${sqlAudience}/user_impersonation' }
            { name: 'DAS_ROLE_SOURCE', value: 'group' }
            { name: 'DAS_REQUIRED_SCOPE', value: 'access_as_user' }
            { name: 'DAS_OM_URL', value: openMetadataUrl }
            // The identity the platform injects; azure-identity and this
            // service's own credential module both discover it the same way.
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
            // Absent on purpose: DAS_ENTRA_TLS_INSECURE. Real certificates.
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 10 }
    }
  }
}

// ----------------------------------------------------------------- gateway --
resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: '${name}-apim'
  location: location
  tags: tags
  sku: { name: apimSku, capacity: apimSku == 'Consumption' ? 0 : 1 }
  identity: { type: 'SystemAssigned' }
  properties: {
    publisherName: publisherName
    publisherEmail: publisherEmail
  }
}

// The gateway reads the catalog bot's token from Key Vault as a named value,
// so rotating it is a vault operation rather than a redeploy.
resource apimVaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, apim.id, 'secrets-user')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource apimLogger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: insights.id
    credentials: { instrumentationKey: insights.properties.InstrumentationKey }
  }
}

// The APIs themselves are NOT declared here. `seed/apim.py` creates them
// through the same ARM surface, against this service exactly as against the
// emulator — one definition of the gateway surface, exercised locally every
// day. Declaring them a second time in Bicep would be the second definition
// that drifts.

output apimName string = apim.name
output apimGatewayUrl string = apim.properties.gatewayUrl
output apimResourceId string = apim.id
output executorUrl string = 'https://${executor.properties.configuration.ingress.fqdn}'
output executorPrincipalId string = identity.properties.principalId
output executorClientId string = identity.properties.clientId
output vaultUri string = vault.properties.vaultUri
output issuer string = issuer
output rateCallsConfigured int = rateCalls
