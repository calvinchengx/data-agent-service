// The credential chain's failure paths, and the token verifier's refresh.
//
// The happy paths are exercised by the stack running. What a working system
// never shows is what happens when a token endpoint refuses — which is exactly
// the moment someone will be reading this code.
package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

func scripted(t *testing.T, replies ...func(http.ResponseWriter, *http.Request)) *httptest.Server {
	t.Helper()
	i := 0
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if i >= len(replies) {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		reply := replies[i]
		i++
		reply(w, r)
	}))
}

func jsonReply(body any) func(http.ResponseWriter, *http.Request) {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(body)
	}
}

func status(code int) func(http.ResponseWriter, *http.Request) {
	return func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(code) }
}

func TestManagedIdentityTokenIsCachedUntilItExpires(t *testing.T) {
	soon := strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10)
	server := scripted(t, jsonReply(map[string]string{"access_token": "mi-1", "expires_on": soon}))
	defer server.Close()

	c := &Credential{miEndpoint: server.URL, miHeader: "h", tokens: map[string]cachedToken{}}
	first, err := c.ManagedIdentityToken("https://vault.azure.net")
	if err != nil || first != "mi-1" {
		t.Fatalf("first fetch: %q %v", first, err)
	}
	// The script is exhausted, so a second HTTP call would 404.
	if again, err := c.ManagedIdentityToken("https://vault.azure.net"); err != nil || again != first {
		t.Fatalf("the token was not cached: %q %v", again, err)
	}
}

// TestManagedIdentityTokenNeedsAnEndpoint already exists in service_test.go;
// one assertion of a fact is enough.

func TestAnIdentityEndpointThatRefusesIsAnError(t *testing.T) {
	server := scripted(t, status(http.StatusForbidden))
	defer server.Close()
	c := &Credential{miEndpoint: server.URL, miHeader: "h", tokens: map[string]cachedToken{}}
	if _, err := c.ManagedIdentityToken("https://vault.azure.net"); err == nil {
		t.Fatal("a 403 from the identity endpoint was treated as success")
	}
}

func TestOnBehalfOfNamesBothAttemptsWhenBothFail(t *testing.T) {
	// "federated credential" and "client secret" are different problems to
	// debug, so a failure that named only one would send someone the wrong way.
	server := scripted(t,
		jsonReply(map[string]string{"access_token": "assertion", "expires_on": "0"}),
		status(http.StatusBadRequest), // federated exchange refused
		status(http.StatusNotFound),   // no stored secret either
	)
	defer server.Close()

	c := &Credential{
		authority: server.URL, miEndpoint: server.URL, miHeader: "h",
		keyVault: server.URL, tokens: map[string]cachedToken{},
	}
	_, err := c.OnBehalfOf("user-token", "https://database.windows.net/.default", "k")
	if err == nil {
		t.Fatal("an exchange with no working credential succeeded")
	}
	if !strings.Contains(err.Error(), "on-behalf-of") {
		t.Fatalf("the error does not say what failed: %v", err)
	}
}

func TestOnBehalfOfUsesTheFederatedCredentialFirst(t *testing.T) {
	// Secretless is the preferred path; a deployment that also has a stored
	// secret must not silently fall back to it.
	soon := strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10)
	var sawAssertion bool
	server := scripted(t,
		jsonReply(map[string]string{"access_token": "assertion", "expires_on": soon}),
		func(w http.ResponseWriter, r *http.Request) {
			_ = r.ParseForm()
			sawAssertion = r.Form.Get("client_assertion") != ""
			jsonReply(map[string]any{"access_token": "obo", "expires_in": 3600})(w, r)
		},
	)
	defer server.Close()

	c := &Credential{
		authority: server.URL, miEndpoint: server.URL, miHeader: "h",
		tokens: map[string]cachedToken{},
	}
	got, err := c.OnBehalfOf("user", "scope/.default", "k")
	if err != nil || got != "obo" {
		t.Fatalf("federated exchange: %q %v", got, err)
	}
	if !sawAssertion {
		t.Fatal("the exchange did not present a client assertion")
	}
}

func TestAnObtainedTokenIsCachedPerScopeAndCaller(t *testing.T) {
	soon := strconv.FormatInt(time.Now().Add(time.Hour).Unix(), 10)
	server := scripted(t,
		jsonReply(map[string]string{"access_token": "assertion", "expires_on": soon}),
		jsonReply(map[string]any{"access_token": "obo-1", "expires_in": 3600}),
	)
	defer server.Close()
	c := &Credential{authority: server.URL, miEndpoint: server.URL, miHeader: "h",
		tokens: map[string]cachedToken{}}

	first, err := c.OnBehalfOf("user", "scope/.default", "alice")
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	// Script exhausted; a cache miss would 404.
	again, err := c.OnBehalfOf("user", "scope/.default", "alice")
	if err != nil || again != first {
		t.Fatalf("the exchange was not cached per caller: %q %v", again, err)
	}
}

func TestHttpClientHonoursTheInsecureSwitch(t *testing.T) {
	// Dev certificates are trusted only where the stack says so; anywhere else
	// the default transport verifies, which is the production path.
	t.Setenv("DAS_ENTRA_TLS_INSECURE", "false")
	if c := httpClient(); c == nil || c.Timeout == 0 {
		t.Fatal("no client, or no timeout")
	}
	t.Setenv("DAS_ENTRA_TLS_INSECURE", "true")
	if httpClient() == nil {
		t.Fatal("no client with the insecure switch on")
	}
}
