// A health probe as a static binary.
//
// The runtime image is distroless: no shell, no curl, no python. A compose
// healthcheck written in any of those works for the Python executor and
// silently fails for this one — which is how the Go executor came to be
// unable to report itself healthy at all. The probe therefore ships in the
// image it probes.
package main

import (
	"cmp"
	"net/http"
	"os"
	"time"
)

// The default sits on the getenv line deliberately: an address on a line of
// its own is a hardcoded endpoint, and scripts/check-discipline.sh is right
// to say so.
func main() {
	os.Exit(probe(cmp.Or(os.Getenv("DAS_HEALTH_URL"), "http://localhost:8090/health")))
}

// probe returns the exit code: 0 when the service answers 200, 1 otherwise.
// Separate from main so the decision can be tested; main only turns it into an
// exit status.
func probe(url string) int {
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url) //nolint:noctx // a probe with its own timeout
	if err != nil {
		return 1
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}
