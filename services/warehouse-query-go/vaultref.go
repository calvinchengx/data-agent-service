// Resolving `keyvault:<name>` references, the Go half.
//
// The reference syntax is written by an OPERATOR into configuration, so both
// executors have to understand it identically or the same settings file means
// two things. vaultref.py is the Python half and this mirrors its contract:
//
//   - a literal is returned unchanged -- a deployment may inject a value
//     directly, and a client with no identity always will;
//   - `keyvault:<name>` is fetched with this process's own managed identity;
//   - a failure to resolve is an ERROR, never a fallback to the reference
//     string. Sending `keyvault:…` as a credential fails at whatever received
//     it, with a message about the header rather than about the vault.
package main

import (
	"fmt"
	"os"
	"strings"
	"sync"
)

const vaultRefPrefix = "keyvault:"

var (
	vaultRefMu    sync.Mutex
	vaultRefCache = map[string]string{}
)

func isVaultRef(value string) bool { return strings.HasPrefix(value, vaultRefPrefix) }

// resolveRef expands a reference, or returns a literal untouched.
func resolveRef(value string) (string, error) {
	if !isVaultRef(value) {
		return value, nil
	}
	name := strings.TrimSpace(strings.TrimPrefix(value, vaultRefPrefix))
	if name == "" {
		return "", fmt.Errorf("%q names no secret", value)
	}

	vaultRefMu.Lock()
	if hit, ok := vaultRefCache[name]; ok {
		vaultRefMu.Unlock()
		return hit, nil
	}
	vaultRefMu.Unlock()

	vault := strings.TrimSuffix(os.Getenv("DAS_KEYVAULT_URL"), "/")
	if vault == "" {
		return "", fmt.Errorf("cannot resolve %s: DAS_KEYVAULT_URL is not set", value)
	}
	token, err := cred.ManagedIdentityToken("https://vault.azure.net")
	if err != nil {
		return "", fmt.Errorf("cannot resolve %s: no managed identity: %w", value, err)
	}
	var payload struct {
		Value string `json:"value"`
	}
	if err := getJSON(vault+"/secrets/"+name+"?api-version=7.5", token, &payload); err != nil {
		return "", fmt.Errorf("cannot resolve %s from %s: %w", value, vault, err)
	}

	vaultRefMu.Lock()
	vaultRefCache[name] = payload.Value
	vaultRefMu.Unlock()
	return payload.Value, nil
}
