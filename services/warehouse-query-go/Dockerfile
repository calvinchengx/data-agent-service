# The Go executor. Note what is NOT here: no ODBC driver, no unixODBC, no
# Kerberos libraries. go-mssqldb speaks TDS in pure Go, so the runtime image is
# a static binary on distroless — which is most of the difference between this
# image and the Python one.
#
# The one native library is DuckDB's, and it is worth being precise about why.
# go-pduckdb needs no cgo — it reaches libduckdb through purego at run time —
# so the binary is still static and CGO_ENABLED=0 still holds. But "pure Go"
# there means no C TOOLCHAIN, not no C LIBRARY: libduckdb.so has to be in the
# image, and it links against libstdc++, which a musl or distroless-static base
# does not carry. Both are copied in below. The DuckDB musl build is the right
# one for this base; the glibc build would load and then fail on a symbol.
ARG DUCKDB_VERSION=v1.5.4

FROM alpine:3.21 AS duckdb
ARG DUCKDB_VERSION
ARG TARGETARCH=amd64
RUN apk add --no-cache curl unzip libstdc++ \
 && curl -sSfL "https://github.com/duckdb/duckdb/releases/download/${DUCKDB_VERSION}/libduckdb-linux-${TARGETARCH}-musl.zip" -o /tmp/libduckdb.zip \
 && unzip -j /tmp/libduckdb.zip libduckdb.so -d /out \
 && cp /usr/lib/libstdc++.so.6 /usr/lib/libgcc_s.so.1 /out/

FROM golang:1.26-alpine AS build
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
# DuckDB and what it links against. DAS_DUCKDB_LIBRARY_PATH is not needed:
# go-pduckdb looks in /usr/local/lib among the standard places.
COPY --from=duckdb /out/libduckdb.so /usr/local/lib/libduckdb.so
COPY --from=duckdb /out/libstdc++.so.6 /usr/local/lib/libstdc++.so.6
COPY --from=duckdb /out/libgcc_s.so.1 /usr/local/lib/libgcc_s.so.1
ENV LD_LIBRARY_PATH=/usr/local/lib
EXPOSE 8090
# The image carries its own probe, so compose does not have to guess which
# implementation it started.
HEALTHCHECK --interval=10s --timeout=5s --retries=12 CMD ["/usr/local/bin/healthcheck"]
ENTRYPOINT ["/usr/local/bin/warehouse-query"]
