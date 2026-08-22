package main

import (
	"errors"
	"io"
	"net/http"
)

const maxBody = 4 << 20

// readLimitedBody reads at most maxBody bytes, so a large or endless request
// cannot exhaust memory. The close error is dropped deliberately: the request
// is finished either way and there is nothing a caller could do with it.
func readLimitedBody(r *http.Request) ([]byte, error) {
	defer func() { _ = r.Body.Close() }()
	return io.ReadAll(io.LimitReader(r.Body, maxBody))
}

func asNotFound(err error, target **notFoundError) bool { return errors.As(err, target) }
