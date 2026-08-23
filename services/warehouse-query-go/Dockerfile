# The Go executor. Note what is NOT here: no ODBC driver, no unixODBC, no
# Kerberos libraries. go-mssqldb speaks TDS in pure Go, so the runtime image is
# a static binary on distroless -- which is most of the difference between this
# image and the Python one.
#
# DuckDB is deliberately NOT in this build, and the reason is worth stating
# because an earlier version of this file got it wrong. go-pduckdb needs no
# cgo, so CGO_ENABLED=0 holds -- but "pure Go" there means no C TOOLCHAIN, not
# a static binary: purego reaches libduckdb through dlopen, and a binary that
# calls dlopen is dynamically linked and needs a loader in the image.
# distroless/static has no loader, so it cannot host that binary AT ALL,
# whichever libduckdb is copied alongside it. The choice is a package deal --
# DuckDB, a dynamic binary, a loader-carrying base and ~78MB of libduckdb and
# libstdc++ -- and no source in this stack is a DuckDB source.
#
# So it is opt-in. `go build -tags duckdb` on an alpine base (musl libduckdb,
# loader at /lib/ld-musl-*.so.1) is the coherent pairing; see
# docs/16-go-parity.md. The adapter and its tests are unchanged and still run
# under the tag in CI.
# Base images come from a pull-through mirror, not Docker Hub directly.
#
# Docker Hub rate-limits anonymous pulls, and a release runner is anonymous.
# That is not hypothetical here: cutting v0.2.0 failed on
# `python:3.12-slim: failed to resolve source metadata` while nothing in the
# repository had changed -- the kind of failure that wastes an afternoon
# because it looks like a code problem and is a quota.
#
# mirror.gcr.io is a cache, not a fork: the manifests it serves for these tags
# are byte-identical to Docker Hub's, checked before switching. Override
# BASE_REGISTRY to pull from anywhere else, including Docker Hub itself.
ARG BASE_REGISTRY=mirror.gcr.io/library
FROM ${BASE_REGISTRY}/golang:1.26-alpine AS build
WORKDIR /src
COPY services/warehouse-query-go/go.mod services/warehouse-query-go/go.sum ./
RUN go mod download
COPY services/warehouse-query-go/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/warehouse-query . \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/healthcheck ./healthcheck

FROM gcr.io/distroless/static-debian12:nonroot
# Which implementation this image is, so a comparison harness can verify it
# measured the executor it believes it measured.
LABEL com.calvinx.das.executor="go"
COPY --from=build /out/warehouse-query /usr/local/bin/warehouse-query
COPY --from=build /out/healthcheck /usr/local/bin/healthcheck
EXPOSE 8090
# The image carries its own probe, so compose does not have to guess which
# implementation it started.
HEALTHCHECK --interval=10s --timeout=5s --retries=12 CMD ["/usr/local/bin/healthcheck"]
ENTRYPOINT ["/usr/local/bin/warehouse-query"]
