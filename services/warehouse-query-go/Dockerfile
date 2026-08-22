# The Go executor. Note what is NOT here: no ODBC driver, no unixODBC, no
# Kerberos libraries. go-mssqldb speaks TDS in pure Go, so the runtime image is
# a static binary on distroless — which is most of the difference between this
# image and the Python one.
FROM golang:1.26-alpine AS build
WORKDIR /src
COPY services/warehouse-query-go/go.mod services/warehouse-query-go/go.sum ./
RUN go mod download
COPY services/warehouse-query-go/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/warehouse-query . \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/healthcheck ./healthcheck

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/warehouse-query /usr/local/bin/warehouse-query
COPY --from=build /out/healthcheck /usr/local/bin/healthcheck
EXPOSE 8090
# The image carries its own probe, so compose does not have to guess which
# implementation it started.
HEALTHCHECK --interval=10s --timeout=5s --retries=12 CMD ["/usr/local/bin/healthcheck"]
ENTRYPOINT ["/usr/local/bin/warehouse-query"]
