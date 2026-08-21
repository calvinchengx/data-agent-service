# The Go executor. Note what is NOT here: no ODBC driver, no unixODBC, no
# Kerberos libraries. go-mssqldb speaks TDS in pure Go, so the runtime image is
# a static binary on distroless — which is most of the difference between this
# image and the Python one.
FROM golang:1.24-alpine AS build
WORKDIR /src
COPY services/warehouse-query-go/go.mod services/warehouse-query-go/go.sum ./
RUN go mod download
COPY services/warehouse-query-go/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/warehouse-query .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/warehouse-query /usr/local/bin/warehouse-query
EXPOSE 8090
ENTRYPOINT ["/usr/local/bin/warehouse-query"]
