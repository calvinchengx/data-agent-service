# Load testing

```sh
make load                                  # every scenario
make load ARGS="--only query --vus 50"     # one of them, harder
make load ARGS="--vus 50 --stage 60s --p95 800"
```

k6 runs in a container **on the stack's network**, so it reaches the gateway
and the executor exactly as the agent does. The driver runs on the host
(it starts that container) and speaks to the stack through the tools container,
so there is one answer to "where does this run" rather than two.

## The scenarios

| Scenario | What it drives | Gate |
|---|---|---|
| `query` | MCP `describe_table` + `run_query` through the gateway — sign-in on-behalf-of, guard, TDS, back | p95 under the threshold, <1% HTTP failures, <1% tool refusals |
| `query-direct` | the same calls straight at the executor | same |
| `catalog` | catalog search through the gateway (passthrough + bot swap) | p95 under threshold |
| `ratelimit` | one caller deliberately exceeding the allowance | the excess **must** be refused (`throttled > 0`) |

A tool call answers HTTP 200 with `isError` inside the payload, so the scripts
check the payload: an HTTP-only check would score a refusal as a success and
report a broken system as a fast one.

## Measured on this machine (20 VUs, 20s ramp)

| Path | Requests | Throughput | p50 | p95 | p99 |
|---|---|---|---|---|---|
| through the gateway | 13,047 | 261/s | 23.1ms | 69.5ms | 78.2ms |
| straight at the executor | 13,349 | 267/s | 22.8ms | 67.9ms | 77.1ms |
| catalog search | 5,498 | 110/s | 60.8ms | 147.0ms | 179.3ms |

**The gateway costs +0.3ms at p50, +1.6ms at p95, and about 2% of throughput.**
That is the number worth having before arguing about where a policy belongs: on
this path, putting one at the gateway is close to free, and the argument should
be about correctness rather than latency.

The rate-limit run throttled 36 of 96 requests from a single caller — the limit
is a control that has been watched firing, not a comment in a policy document.

## What these numbers do and do not mean

They describe a laptop running SQL Server in a container next to five
emulators. They are **not** Fabric capacity, and no conclusion about production
throughput should be drawn from them.

What transfers is the shape: the gateway's overhead as a proportion, that the
executor's on-behalf-of token cache holds under concurrency (13k requests, one
sign-in), that nothing leaks refusals under load, and — most usefully —
regressions between two commits of this repo. The same scripts run against real
Azure with `ENV=prod`, and only that run says anything about production.
