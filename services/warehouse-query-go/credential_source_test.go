// A stored credential on a `service` source, mirroring the Python cases in
// tests/test_executor_sources.py and tests/test_executor_http_routes.py: the
// decision is the source's tier and credential, never its engine.
package main

import (
	"errors"
	"net/http"
	"strings"
	"testing"
)

func TestAStoredCredentialOnAUserSourceIsRefusedAtStartup(t *testing.T) {
	t.Setenv("DAS_SOURCES", `[{"name":"x","kind":"postgres","authz_tier":"user",`+
		`"dsn":"postgresql://u@host/db","credential":"keyvault:pg"}]`)
	if _, err := LoadSources(); err == nil || !strings.Contains(err.Error(), "cannot carry the caller's permissions") {
		t.Fatalf("err %v", err)
	}
}

func TestACredentialAndADSNPasswordTogetherAreRefused(t *testing.T) {
	for _, dsn := range []string{
		"postgresql://u:secret@host/db",
		"postgresql://u@host/db?password=secret",
		"host=host user=u password=secret dbname=db",
	} {
		t.Setenv("DAS_SOURCES", `[{"name":"x","kind":"postgres","authz_tier":"service",`+
			`"dsn":"`+dsn+`","credential":"keyvault:pg"}]`)
		if _, err := LoadSources(); err == nil || !strings.Contains(err.Error(), "one home") {
			t.Fatalf("%q: err %v", dsn, err)
		}
	}
}

func TestACredentialOnAnyServiceSourceLoads(t *testing.T) {
	for _, kind := range []string{"postgres", "fabric"} {
		t.Setenv("DAS_SOURCES", `[{"name":"x","kind":"`+kind+`","authz_tier":"service",`+
			`"dsn":"postgresql://u@host/db","credential":"keyvault:s"}]`)
		loaded, err := LoadSources()
		if err != nil || loaded["x"].Credential != "keyvault:s" {
			t.Fatalf("%s: %v %+v", kind, err, loaded["x"])
		}
	}
}

func TestDSNHasPasswordReadsBothForms(t *testing.T) {
	for dsn, want := range map[string]bool{
		"":                                  false,
		"postgresql://u@host/db":            false,
		"host=h user=u dbname=d":            false,
		"postgresql://u:p@host/db":          true,
		"postgresql://u@host/db?password=p": true,
		"host=h password=p":                 true,
		"host=h application_name=password_rotator": false,
	} {
		if got := dsnHasPassword(dsn); got != want {
			t.Fatalf("%q: %v", dsn, got)
		}
	}
}

// withVault points vault references at a fake for the duration of a test.
func withVault(t *testing.T, value string) {
	t.Helper()
	vaultRefMu.Lock()
	vaultRefCache["c"] = value
	vaultRefMu.Unlock()
	t.Cleanup(func() {
		vaultRefMu.Lock()
		delete(vaultRefCache, "c")
		vaultRefMu.Unlock()
	})
}

func TestTheTokenDecisionDoesNotDependOnTheEngine(t *testing.T) {
	harness(t)
	withVault(t, "the-secret")
	p := &Principal{Token: "caller", Name: "alice"}
	for _, kind := range []string{"fabric", "postgres", "databricks"} {
		stored := Source{Name: "s", Kind: kind, AuthzTier: "service", Credential: "keyvault:c"}
		if tok, err := principalToken(stored, p); err != nil || tok != "the-secret" {
			t.Fatalf("%s stored: %q %v", kind, tok, err)
		}
		if credentialKind(stored) != "stored" {
			t.Fatal("audit kind")
		}

		identity := Source{Name: "s", Kind: kind, AuthzTier: "service"}
		if tok, err := principalToken(identity, p); err != nil || !strings.HasPrefix(tok, "mi-token-for-") {
			t.Fatalf("%s identity: %q %v", kind, tok, err)
		}
		if credentialKind(identity) != "identity" {
			t.Fatal("audit kind")
		}

		user := Source{Name: "s", Kind: kind, AuthzTier: "user"}
		if credentialKind(user) != "user" {
			t.Fatal("audit kind")
		}

		wrong := Source{Name: "s", Kind: kind, AuthzTier: "user", Credential: "k"}
		_, err := principalToken(wrong, p)
		var cfg *configError
		if !errors.As(err, &cfg) {
			t.Fatalf("%s user+credential: %v", kind, err)
		}
		if status, _ := tokenFailure(err); status != http.StatusInternalServerError {
			t.Fatalf("status %d", status)
		}
	}
}

func TestAMistypedCredentialSchemeIsRefusedRatherThanSent(t *testing.T) {
	src := Source{Name: "s", Kind: "postgres", AuthzTier: "service", Credential: "vault:c"}
	_, err := principalToken(src, &Principal{})
	if err == nil || !strings.Contains(err.Error(), "not recognised") {
		t.Fatalf("err %v", err)
	}
}

func TestALiteralWithNoSchemeIsPassedThrough(t *testing.T) {
	src := Source{Name: "s", Kind: "postgres", AuthzTier: "service", Credential: "a-pasted-key"}
	tok, err := principalToken(src, &Principal{})
	if err != nil || tok != "a-pasted-key" {
		t.Fatalf("%q %v", tok, err)
	}
}

func TestAnUnresolvableReferenceIsReportedAs502NotSent(t *testing.T) {
	t.Setenv("DAS_KEYVAULT_URL", "")
	src := Source{Name: "s", Kind: "postgres", AuthzTier: "service", Credential: "keyvault:absent"}
	_, err := principalToken(src, &Principal{})
	if err == nil {
		t.Fatal("resolved nothing into something")
	}
	if status, msg := tokenFailure(err); status != http.StatusBadGateway || !strings.Contains(msg, "source s") {
		t.Fatalf("%d %s", status, msg)
	}
}

func TestAServiceSourceWithACredentialSendsItAsThePostgresPassword(t *testing.T) {
	src := pgSource()
	src.DSN = "postgresql://u@host/db"
	src.Credential = "keyvault:c"
	dsn, err := dsnFor(src, "the-resolved-secret")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(dsn, "u:the-resolved-secret@") {
		t.Fatalf("the credential did not become the password: %s", dsn)
	}
}
