// Tokens: the service's own identity, and the user's, on the user's behalf.
// The Go half of services/warehouse-query-py/credential.py — same two hops,
// same standard protocols, same order of preference (federated first, a Key
// Vault secret second, never an environment variable).
package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const exchangeAudience = "api://AzureADTokenExchange"

var insecureTLS = strings.EqualFold(os.Getenv("DAS_ENTRA_TLS_INSECURE"), "true")

func httpClient() *http.Client {
	transport := &http.Transport{}
	if insecureTLS {
		transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true} // #nosec G402 — dev certificates only
	}
	return &http.Client{Timeout: 60 * time.Second, Transport: transport}
}

var client = httpClient()

type Credential struct {
	authority   string
	clientID    string
	keyVault    string
	secretName  string
	miEndpoint  string
	miHeader    string
	mu          sync.Mutex
	tokens      map[string]cachedToken
	secret      string
	secretKnown bool
}

type cachedToken struct {
	value     string
	expiresAt time.Time
}

func NewCredential() *Credential {
	issuer := strings.TrimSuffix(os.Getenv("DAS_ENTRA_ISSUER"), "/")
	authority := strings.TrimSuffix(issuer, "/v2.0")
	secretName := os.Getenv("DAS_EXECUTOR_SECRET_NAME")
	if secretName == "" {
		// The NAME of a secret in Key Vault, not a secret. gosec cannot tell
		// the difference, and a name is exactly what belongs in code.
		secretName = "das-executor-client-secret" //nolint:gosec // G101: vault key name
	}
	return &Credential{
		authority:  authority,
		clientID:   os.Getenv("DAS_MIDDLE_TIER_CLIENT_ID"),
		keyVault:   strings.TrimSuffix(os.Getenv("DAS_KEYVAULT_URL"), "/"),
		secretName: secretName,
		miEndpoint: os.Getenv("IDENTITY_ENDPOINT"),
		miHeader:   os.Getenv("IDENTITY_HEADER"),
		tokens:     map[string]cachedToken{},
	}
}

// How long any single identity call may take. Short on purpose: a token
// endpoint that has stopped answering should surface as a failed request, not
// as a request that never returns.
const tokenCallTimeout = 30 * time.Second

func (c *Credential) cached(key string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if hit, ok := c.tokens[key]; ok && time.Until(hit.expiresAt) > time.Minute {
		return hit.value, true
	}
	return "", false
}

func (c *Credential) store(key, value string, expires time.Time) {
	c.mu.Lock()
	c.tokens[key] = cachedToken{value: value, expiresAt: expires}
	c.mu.Unlock()
}

// ManagedIdentityToken uses the App Service protocol — the same two variables
// the platform sets in Azure, so this code is unchanged in production.
func (c *Credential) ManagedIdentityToken(resource string) (string, error) {
	key := "mi:" + resource
	if token, ok := c.cached(key); ok {
		return token, nil
	}
	if c.miEndpoint == "" {
		return "", fmt.Errorf("no managed identity endpoint (IDENTITY_ENDPOINT unset)")
	}
	target := c.miEndpoint + "?resource=" + url.QueryEscape(resource) + "&api-version=2019-08-01"
	// Every outbound call carries a deadline it can be cancelled by. A client
	// timeout alone stops a hung response; it does not stop a caller that has
	// already given up from holding the connection.
	ctx, cancel := context.WithTimeout(context.Background(), tokenCallTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("X-IDENTITY-HEADER", c.miHeader)
	var payload struct {
		AccessToken string `json:"access_token"`
		ExpiresOn   string `json:"expires_on"`
	}
	if err := doJSON(req, &payload); err != nil {
		return "", err
	}
	expires := time.Now().Add(time.Hour)
	if seconds, err := strconv.ParseInt(payload.ExpiresOn, 10, 64); err == nil {
		expires = time.Unix(seconds, 0)
	}
	c.store(key, payload.AccessToken, expires)
	return payload.AccessToken, nil
}

func (c *Credential) clientSecret() string {
	c.mu.Lock()
	if c.secretKnown {
		defer c.mu.Unlock()
		return c.secret
	}
	c.mu.Unlock()
	value := ""
	if c.keyVault != "" {
		if token, err := c.ManagedIdentityToken("https://vault.azure.net"); err == nil {
			ctx, cancel := context.WithTimeout(context.Background(), tokenCallTimeout)
			defer cancel()
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet,
				c.keyVault+"/secrets/"+c.secretName+"?api-version=7.5", nil)
			req.Header.Set("Authorization", "Bearer "+token)
			var payload struct {
				Value string `json:"value"`
			}
			if err := doJSON(req, &payload); err == nil {
				value = payload.Value
			}
		}
	}
	c.mu.Lock()
	c.secret, c.secretKnown = value, true
	c.mu.Unlock()
	return value
}

// OnBehalfOf exchanges the USER's token for a data-plane token that still
// carries the user, so the source applies that user's own permissions.
func (c *Credential) OnBehalfOf(userAssertion, scope, cacheKey string) (string, error) {
	key := scope + "|" + cacheKey
	if token, ok := c.cached(key); ok {
		return token, nil
	}
	form := url.Values{
		"grant_type":          {"urn:ietf:params:oauth:grant-type:jwt-bearer"},
		"client_id":           {c.clientID},
		"assertion":           {userAssertion},
		"scope":               {scope},
		"requested_token_use": {"on_behalf_of"},
	}
	var problems []string

	if assertion, err := c.ManagedIdentityToken(exchangeAudience); err == nil {
		federated := cloneValues(form)
		federated.Set("client_assertion_type",
			"urn:ietf:params:oauth:client-assertion-type:jwt-bearer")
		federated.Set("client_assertion", assertion)
		if token, expires, err := c.tokenRequest(federated); err == nil {
			c.store(key, token, expires)
			return token, nil
		} else {
			problems = append(problems, "federated credential: "+err.Error())
		}
	}
	if secret := c.clientSecret(); secret != "" {
		withSecret := cloneValues(form)
		withSecret.Set("client_secret", secret)
		if token, expires, err := c.tokenRequest(withSecret); err == nil {
			c.store(key, token, expires)
			return token, nil
		} else {
			problems = append(problems, "client secret: "+err.Error())
		}
	}
	if len(problems) == 0 {
		problems = append(problems, "no credential")
	}
	return "", fmt.Errorf("on-behalf-of exchange failed — %s", strings.Join(problems, "; "))
}

func cloneValues(in url.Values) url.Values {
	out := url.Values{}
	for k, v := range in {
		out[k] = append([]string(nil), v...)
	}
	return out
}

func (c *Credential) tokenRequest(form url.Values) (string, time.Time, error) {
	ctx, cancel := context.WithTimeout(context.Background(), tokenCallTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.authority+"/oauth2/v2.0/token",
		strings.NewReader(form.Encode()))
	if err != nil {
		return "", time.Time{}, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	var payload struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := doJSON(req, &payload); err != nil {
		return "", time.Time{}, err
	}
	seconds := payload.ExpiresIn
	if seconds == 0 {
		seconds = 3600
	}
	return payload.AccessToken, time.Now().Add(time.Duration(seconds) * time.Second), nil
}

func doJSON(req *http.Request, out any) error {
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("%d %s", resp.StatusCode, truncate(string(body), 300))
	}
	return json.Unmarshal(body, out)
}

func getJSON(target, token string, out any) error {
	ctx, cancel := context.WithTimeout(context.Background(), tokenCallTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	return doJSON(req, out)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
