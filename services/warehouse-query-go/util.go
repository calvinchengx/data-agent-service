package main

import (
	"errors"
	"io"
	"net/http"
)

const maxBody = 4 << 20

func io_ReadAll(r *http.Request) ([]byte, error) {
	defer r.Body.Close()
	return io.ReadAll(io.LimitReader(r.Body, maxBody))
}

func asNotFound(err error, target **notFoundError) bool { return errors.As(err, target) }
