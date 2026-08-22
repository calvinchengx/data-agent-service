package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestProbeSucceedsOnlyOnA200(t *testing.T) {
	ok := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer ok.Close()
	if code := probe(ok.URL); code != 0 {
		t.Fatalf("a healthy service probed as %d", code)
	}

	sick := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer sick.Close()
	if code := probe(sick.URL); code != 1 {
		t.Fatalf("an unhealthy service probed as %d", code)
	}
}

func TestProbeFailsWhenNothingIsListening(t *testing.T) {
	if code := probe("http://127.0.0.1:1/health"); code != 1 {
		t.Fatalf("an unreachable service probed as %d", code)
	}
}
