// Bearer validation against the tenant's JWKS: signature, issuer, audience,
// expiry. The gateway validates the same token in production; this is the
// layer that cannot be bypassed by reaching the service directly.
package main

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"math/big"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type TokenVerifier struct {
	Issuer  string
	JWKSURL string
	mu      sync.RWMutex
	keys    map[string]any
	fetched time.Time
}

func NewTokenVerifier() *TokenVerifier {
	issuer := strings.TrimSuffix(os.Getenv("DAS_ENTRA_ISSUER"), "/")
	jwks := os.Getenv("DAS_ENTRA_JWKS_URL")
	if jwks == "" {
		jwks = strings.TrimSuffix(issuer, "/v2.0") + "/discovery/v2.0/keys"
	}
	return &TokenVerifier{Issuer: issuer, JWKSURL: jwks, keys: map[string]any{}}
}

type jwksDocument struct {
	Keys []struct {
		Kid string `json:"kid"`
		Kty string `json:"kty"`
		N   string `json:"n"`
		E   string `json:"e"`
	} `json:"keys"`
}

func (v *TokenVerifier) key(kid string) (any, error) {
	v.mu.RLock()
	key, ok := v.keys[kid]
	fresh := time.Since(v.fetched) < time.Hour
	v.mu.RUnlock()
	if ok {
		return key, nil
	}
	if fresh && len(v.keys) > 0 {
		return nil, fmt.Errorf("unknown key %s", kid)
	}
	if err := v.refresh(); err != nil {
		return nil, err
	}
	v.mu.RLock()
	defer v.mu.RUnlock()
	if key, ok := v.keys[kid]; ok {
		return key, nil
	}
	return nil, fmt.Errorf("unknown key %s", kid)
}

func (v *TokenVerifier) refresh() error {
	req, err := http.NewRequestWithContext(context.Background(), http.MethodGet, v.JWKSURL, nil)
	if err != nil {
		return err
	}
	var doc jwksDocument
	if err := doJSON(req, &doc); err != nil {
		return err
	}
	keys := map[string]any{}
	for _, k := range doc.Keys {
		if k.Kty != "RSA" || k.Kid == "" {
			continue
		}
		parsed, err := rsaKeyFromJWK(k.N, k.E)
		if err != nil {
			continue
		}
		keys[k.Kid] = parsed
	}
	if len(keys) == 0 {
		return errors.New("the key document carried no usable RSA key")
	}
	v.mu.Lock()
	v.keys, v.fetched = keys, time.Now()
	v.mu.Unlock()
	return nil
}

// Verify returns the claims of a token this tenant signed for this audience.
// Any failure to VERIFY is the caller's problem, never a server error.
func (v *TokenVerifier) Verify(raw, audience string) (map[string]any, error) {
	claims := jwt.MapClaims{}
	_, err := jwt.ParseWithClaims(raw, claims, func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodRSA); !ok {
			return nil, fmt.Errorf("unexpected signing method %v", t.Header["alg"])
		}
		kid, _ := t.Header["kid"].(string)
		if kid == "" {
			return nil, errors.New("no key id in the token header")
		}
		return v.key(kid)
	}, jwt.WithIssuer(v.Issuer), jwt.WithAudience(audience),
		jwt.WithValidMethods([]string{"RS256"}), jwt.WithLeeway(60*time.Second))
	if err != nil {
		return nil, err
	}
	return map[string]any(claims), nil
}

// rsaKeyFromJWK rebuilds a public key from a JWK's base64url modulus and
// exponent. golang-jwt parses PEM, not JWK, and a key server speaks JWK.
func rsaKeyFromJWK(modulus, exponent string) (*rsa.PublicKey, error) {
	n, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(modulus, "="))
	if err != nil {
		return nil, err
	}
	e, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(exponent, "="))
	if err != nil {
		return nil, err
	}
	if len(e) == 0 || len(e) > 8 {
		return nil, errors.New("implausible exponent")
	}
	padded := make([]byte, 8)
	copy(padded[8-len(e):], e)
	// A published exponent that does not fit in an int is not a key we can
	// use, and silently wrapping it to a negative number would make signature
	// verification meaningless rather than failed.
	value := binary.BigEndian.Uint64(padded)
	if value > math.MaxInt32 {
		return nil, fmt.Errorf("jwks: exponent %d is out of range", value)
	}
	return &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: int(value)}, nil
}
