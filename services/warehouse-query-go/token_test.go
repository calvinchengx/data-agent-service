// Token verification: signatures are checked for real, not stubbed.
//
// A test that fakes verification proves the handlers work for a caller who was
// never authenticated, which is the one thing this service must not do. These
// tests generate a key, sign with it, and let the real verifier decide.
package main

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const testIssuer = "https://entra.test/v2.0"
const testAudience = "api://data-agent-service"

type signer struct {
	key *rsa.PrivateKey
	kid string
}

func newSigner(t *testing.T) *signer {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	return &signer{key: key, kid: "test-key"}
}

func (s *signer) token(t *testing.T, mutate func(jwt.MapClaims)) string {
	t.Helper()
	claims := jwt.MapClaims{
		"iss":                testIssuer,
		"aud":                testAudience,
		"sub":                "alice-sub",
		"oid":                "alice-oid",
		"preferred_username": "alice@entraemulator.dev",
		"scp":                "access_as_user",
		"roles":              []any{"Data.Analyst"},
		"iat":                time.Now().Add(-time.Minute).Unix(),
		"exp":                time.Now().Add(10 * time.Minute).Unix(),
	}
	if mutate != nil {
		mutate(claims)
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	tok.Header["kid"] = s.kid
	signed, err := tok.SignedString(s.key)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	return signed
}

func (s *signer) verifier() *TokenVerifier {
	v := &TokenVerifier{Issuer: testIssuer, keys: map[string]any{}, fetched: time.Now()}
	v.keys[s.kid] = &s.key.PublicKey
	return v
}

func (s *signer) jwksJSON() string {
	pub := s.key.PublicKey
	n := base64.RawURLEncoding.EncodeToString(pub.N.Bytes())
	e := base64.RawURLEncoding.EncodeToString(big.NewInt(int64(pub.E)).Bytes())
	doc, _ := json.Marshal(map[string]any{"keys": []map[string]string{
		{"kid": s.kid, "kty": "RSA", "n": n, "e": e},
	}})
	return string(doc)
}

func TestVerifyAcceptsATokenThisTenantSigned(t *testing.T) {
	s := newSigner(t)
	claims, err := s.verifier().Verify(s.token(t, nil), testAudience)
	if err != nil {
		t.Fatalf("a valid token was rejected: %v", err)
	}
	if claims["oid"] != "alice-oid" {
		t.Fatalf("claims not returned: %v", claims)
	}
}

func TestVerifyRejects(t *testing.T) {
	s := newSigner(t)
	other := newSigner(t)
	cases := []struct {
		name  string
		token func() string
		want  string
	}{
		{"another issuer", func() string {
			return s.token(t, func(c jwt.MapClaims) { c["iss"] = "https://evil.test/v2.0" })
		}, "iss"},
		{"another audience", func() string {
			return s.token(t, func(c jwt.MapClaims) { c["aud"] = "api://someone-else" })
		}, "aud"},
		{"expired", func() string {
			return s.token(t, func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-time.Hour).Unix() })
		}, "expired"},
		{"another key", func() string { return other.token(t, nil) }, "key"},
		{"not a token", func() string { return "not.a.token" }, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := s.verifier().Verify(tc.token(), testAudience); err == nil {
				t.Fatal("a token that should not verify was accepted")
			}
		})
	}
}

func TestVerifyRejectsAnUnsignedToken(t *testing.T) {
	// alg=none is the classic bypass; WithValidMethods must refuse it.
	tok := jwt.NewWithClaims(jwt.SigningMethodNone, jwt.MapClaims{
		"iss": testIssuer, "aud": testAudience, "exp": time.Now().Add(time.Hour).Unix(),
	})
	tok.Header["kid"] = "test-key"
	raw, err := tok.SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Skipf("cannot build an alg=none token: %v", err)
	}
	if _, err := newSigner(t).verifier().Verify(raw, testAudience); err == nil {
		t.Fatal("an unsigned token was accepted")
	}
}

func TestVerifyRejectsATokenWithNoKeyID(t *testing.T) {
	s := newSigner(t)
	claims := jwt.MapClaims{
		"iss": testIssuer, "aud": testAudience, "exp": time.Now().Add(time.Hour).Unix(),
	}
	raw, err := jwt.NewWithClaims(jwt.SigningMethodRS256, claims).SignedString(s.key)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	if _, err := s.verifier().Verify(raw, testAudience); err == nil {
		t.Fatal("a token with no kid was accepted")
	}
}

func TestTheKeySetIsFetchedFromTheTenantAndCached(t *testing.T) {
	s := newSigner(t)
	hits := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		hits++
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(s.jwksJSON()))
	}))
	defer server.Close()

	v := &TokenVerifier{Issuer: testIssuer, JWKSURL: server.URL, keys: map[string]any{}}
	if _, err := v.Verify(s.token(t, nil), testAudience); err != nil {
		t.Fatalf("verify against a fetched key set: %v", err)
	}
	if _, err := v.Verify(s.token(t, nil), testAudience); err != nil {
		t.Fatalf("second verify: %v", err)
	}
	if hits != 1 {
		t.Fatalf("the key set was fetched %d times; it should be cached", hits)
	}
}

func TestAnUnknownKeyIDRefreshesOnceThenFails(t *testing.T) {
	s := newSigner(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(s.jwksJSON()))
	}))
	defer server.Close()
	v := &TokenVerifier{Issuer: testIssuer, JWKSURL: server.URL, keys: map[string]any{}}

	other := newSigner(t)
	other.kid = "rotated-away"
	if _, err := v.Verify(other.token(t, nil), testAudience); err == nil {
		t.Fatal("a token signed by a key the tenant does not publish was accepted")
	}
}

func TestAnUnreachableKeyServerIsAnError(t *testing.T) {
	v := &TokenVerifier{
		Issuer:  testIssuer,
		JWKSURL: "http://127.0.0.1:1/keys",
		keys:    map[string]any{},
	}
	if _, err := v.Verify(newSigner(t).token(t, nil), testAudience); err == nil {
		t.Fatal("verification succeeded with no reachable key server")
	}
}

func TestRsaKeyFromJWKRoundTrips(t *testing.T) {
	s := newSigner(t)
	pub := s.key.PublicKey
	key, err := rsaKeyFromJWK(
		base64.RawURLEncoding.EncodeToString(pub.N.Bytes()),
		base64.RawURLEncoding.EncodeToString(big.NewInt(int64(pub.E)).Bytes()),
	)
	if err != nil {
		t.Fatalf("rebuild: %v", err)
	}
	if key.N.Cmp(pub.N) != 0 || key.E != pub.E {
		t.Fatal("the rebuilt key does not match the original")
	}
}

func TestRsaKeyFromJWKRejectsAnOutOfRangeExponent(t *testing.T) {
	// Converting this without a bounds check wraps to a negative modulus, which
	// makes signature verification meaningless rather than failed.
	huge := base64.RawURLEncoding.EncodeToString([]byte{0xFF, 0xFF, 0xFF, 0xFF, 0xFF})
	if _, err := rsaKeyFromJWK("AQAB", huge); err == nil {
		t.Fatal("an exponent that does not fit in an int was accepted")
	}
}

func TestRsaKeyFromJWKRejectsGarbage(t *testing.T) {
	if _, err := rsaKeyFromJWK("!!!not base64!!!", "AQAB"); err == nil {
		t.Fatal("a malformed modulus was accepted")
	}
	if _, err := rsaKeyFromJWK("AQAB", "!!!"); err == nil {
		t.Fatal("a malformed exponent was accepted")
	}
}

func TestAJWKSDocumentThatIsNotJSONIsAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("<html>not a key set</html>"))
	}))
	defer server.Close()
	v := &TokenVerifier{Issuer: testIssuer, JWKSURL: server.URL, keys: map[string]any{}}
	if _, err := v.Verify(newSigner(t).token(t, nil), testAudience); err == nil {
		t.Fatal("an HTML key set was accepted")
	}
}

func TestNewTokenVerifierDerivesTheKeySetURLFromTheIssuer(t *testing.T) {
	t.Setenv("DAS_ENTRA_ISSUER", "https://login.microsoftonline.com/tid/v2.0")
	t.Setenv("DAS_ENTRA_JWKS_URL", "")
	v := NewTokenVerifier()
	if !strings.HasSuffix(v.JWKSURL, "/discovery/v2.0/keys") {
		t.Fatalf("derived key set URL is %q", v.JWKSURL)
	}
	t.Setenv("DAS_ENTRA_JWKS_URL", "https://explicit.test/keys")
	if NewTokenVerifier().JWKSURL != "https://explicit.test/keys" {
		t.Fatal("an explicit key set URL must win")
	}
}
