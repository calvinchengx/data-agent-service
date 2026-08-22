// What a caller is told about an engine failure.
//
// The contract both executors satisfy: the engine's own sentence survives,
// the driver's layers do not, and every client-facing string goes through one
// function. Mirrors tests/test_executor_app.py so the two implementations
// cannot drift on it again.
package main

import (
	"errors"
	"strings"
	"testing"
)

func TestEngineMessageStripsPackedDriverLayers(t *testing.T) {
	// The common form: no space between the layers. The earlier version cut at
	// the last "] " and so stripped nothing here.
	err := errors.New("[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]" +
		"The SELECT permission was denied on the object 'dim_customer'")
	got := engineMessage(err)
	if strings.Contains(got, "ODBC Driver 18") || strings.Contains(got, "[Microsoft]") {
		t.Fatalf("driver noise survived: %q", got)
	}
	if !strings.HasPrefix(got, "The SELECT permission was denied") {
		t.Fatalf("the engine's own sentence did not survive: %q", got)
	}
}

func TestEngineMessageStripsSpacedLayersToo(t *testing.T) {
	err := errors.New("[42000] [Microsoft] The login failed")
	if got := engineMessage(err); strings.Contains(got, "42000") {
		t.Fatalf("spaced layers survived: %q", got)
	}
}

func TestEngineMessageKeepsAMessageThatHasNoLayers(t *testing.T) {
	if got := engineMessage(errors.New("connection refused")); got != "connection refused" {
		t.Fatalf("a plain message was altered: %q", got)
	}
}

func TestEngineMessageDropsTheDdbcPrefix(t *testing.T) {
	err := errors.New("driver noise DDBC Error: the real problem")
	if got := engineMessage(err); got != "the real problem" {
		t.Fatalf("got %q", got)
	}
}

func TestEngineMessageCapsWhatItReturns(t *testing.T) {
	if got := engineMessage(errors.New(strings.Repeat("x", 5000))); len(got) > 400 {
		t.Fatalf("returned %d characters", len(got))
	}
}

func TestEngineMessageLeavesNothingBehindWhenTheMessageIsOnlyLayers(t *testing.T) {
	// Degenerate, but it must not panic or return the layers.
	if got := engineMessage(errors.New("[a][b][c]")); strings.Contains(got, "[a]") {
		t.Fatalf("got %q", got)
	}
}

func TestAPermissionRefusalKeepsTheEnginesWords(t *testing.T) {
	denial := errors.New("The SELECT permission was denied on the object 'dim_customer'")
	if !isDenial(denial) {
		t.Fatal("a permission refusal was not recognised as one")
	}
	if got := clientError(denial); !strings.Contains(got, "SELECT permission was denied") {
		t.Fatalf("the refusal did not survive: %q", got)
	}
}

func TestAnUnrecognisedFailureTellsTheCallerNothing(t *testing.T) {
	// Whatever a driver puts in an error, none of it reaches a caller. The
	// audit line keeps it for whoever has to debug this.
	leaky := errors.New(
		"connect failed: /opt/app/secrets/conn.ini server=contoso.internal;Pwd=hunter2")
	got := clientError(leaky)
	for _, secret := range []string{"hunter2", "/opt/app", "contoso.internal"} {
		if strings.Contains(got, secret) {
			t.Fatalf("%q leaked in %q", secret, got)
		}
	}
	if got != "the source could not complete this query" {
		t.Fatalf("unexpected message: %q", got)
	}
}
