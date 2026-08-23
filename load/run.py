"""`make load` — the load runs, their thresholds, and the gateway's cost.

    python -m load.run                    # every scenario
    python -m load.run --only query       # one of them
    python -m load.run --vus 50 --stage 60s

Scenarios run in a k6 container on the same network as the stack, so they reach
the gateway and the executor exactly as the agent does.

Three things are measured, and the third is the reason the first two are run
twice:

  * **query** — MCP tool calls through the gateway to the executor: sign-in
    on-behalf-of, guard, TDS, back again.
  * **catalog** — OpenMetadata through the gateway and the executor, as the role bot.
  * **the gateway's cost** — the same query scenario aimed straight at the
    executor. The difference between the two p95s is what API Management costs
    on this path, which is a number worth knowing before choosing where to put
    a policy.

And one that is not about throughput at all: **ratelimit** deliberately exceeds
the configured allowance and asserts the excess is refused. A limit nobody has
watched fire is a comment, not a control.

The absolute numbers describe a laptop running SQL Server in a container, not
Fabric capacity. What transfers is the SHAPE: the gateway's overhead, whether
the executor's token cache holds under concurrency, and regressions between two
commits of this repo.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
REPO = ROOT.parent
K6_IMAGE = os.environ.get("DAS_K6_IMAGE", "grafana/k6:latest")
NETWORK = os.environ.get("DAS_COMPOSE_NETWORK", "data-agent-service_default")

SCENARIOS = {
    "query": {
        "script": "query.js",
        "env": {"DAS_LOAD_TARGET": "gateway"},
        "what": "MCP tool calls through the gateway",
    },
    "query-direct": {
        "script": "query.js",
        "env": {"DAS_LOAD_TARGET": "direct"},
        "what": "the same calls straight at the executor (gateway cost)",
    },
    "catalog": {"script": "catalog.js", "env": {}, "what": "catalog search through the gateway"},
    "ratelimit": {
        "script": "ratelimit.js",
        "env": {},
        "what": "the gateway's rate limit refusing the excess",
    },
}

PASSTHROUGH = (
    "DAS_APIM_BASE",
    "DAS_EXECUTOR_URL",
    "DAS_AGENT_CLIENT_ID",
    "DAS_AGENT_AUDIENCE",
    "DAS_WAREHOUSE_MCP_PATH",
    "DAS_OM_MCP_PATH",
    "DAS_OM_SUBSCRIPTION_KEY",
    "DAS_TEST_PASSWORD",
    "DAS_RATE_CALLS",
)


def compose(*args: str, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside the stack's network (the tools container).

    The driver itself runs on the host because it starts k6 in a container;
    anything that must SPEAK to the stack goes through here, so there is one
    answer to "where does this run" rather than two.
    """
    envfile = os.environ.get("ENVFILE", ".env")
    cmd = [
        "docker",
        "compose",
        "--env-file",
        envfile,
        "--profile",
        "tools",
        "run",
        "--rm",
        "-T",
        "tools",
        *args,
    ]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=quiet, text=True, check=False)
    if proc.returncode != 0 and quiet:
        print((proc.stdout or "") + (proc.stderr or ""))
    return proc


def resolved_passthrough() -> dict[str, str]:
    """The settings k6 needs, with `keyvault:` references already expanded.

    One call rather than one per setting: the container start dominates, and
    a loop would pay it for every value.
    """
    script = (
        "import json;from seed import common as c;"
        f"print(json.dumps({{k: c.setting(k) for k in {list(PASSTHROUGH)!r} if k in c.CFG}}))"
    )
    proc = compose("python", "-c", script)
    if proc.returncode != 0:
        raise SystemExit("could not resolve the load settings inside the stack")
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit(f"no settings came back from the stack: {(proc.stdout or '')[:200]}")


def set_rate_limit(calls: int) -> None:
    compose("python", "-m", "seed.apim", "--rate-calls", str(calls))


def env_for(scenario: dict, args) -> dict[str, str]:
    from seed import common as c

    # Resolved rather than copied: k6 runs in its own container with no
    # identity, so a `keyvault:` reference would reach it unresolved and be
    # sent as a credential.
    #
    # Resolved IN THE STACK, which is the rule this module already states and
    # which I broke: the driver runs on the host, and the host has neither a
    # managed identity nor a route to the vault. Doing it here worked on a
    # laptop whose environment happened to carry the value and failed in CI
    # from a clean checkout -- the shape of every ambient-state bug.
    env = resolved_passthrough()
    env["DAS_AUTHORITY"] = c.AUTHORITY
    env["DAS_LOAD_USER"] = args.user
    # k6 cannot sign a person in. Where the tenant forbids the password grant,
    # the token is obtained here — by the same means every other harness uses —
    # and handed to the generator.
    if os.environ.get("DAS_HARNESS_AUTH", "password").lower() != "password":
        from agent import identity

        env["DAS_LOAD_TOKEN"] = identity.token_for(args.user)
    env["DAS_LOAD_VUS_LOW"] = str(max(1, args.vus // 4))
    env["DAS_LOAD_VUS_HIGH"] = str(args.vus)
    env["DAS_LOAD_STAGE"] = args.stage
    env["DAS_LOAD_P95_MS"] = str(args.p95)
    env.update(scenario["env"])
    return env


def _int(value) -> int:
    """A metric k6 did not emit is absent, not zero-shaped: treat a missing
    or non-numeric counter as zero rather than adding it to a string."""
    return int(value) if isinstance(value, (int, float)) else 0


def run_scenario(name: str, args) -> dict:
    scenario = SCENARIOS[name]
    env = env_for(scenario, args)
    summary = REPORTS / f"{name}.summary.json"
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        NETWORK,
        "-v",
        f"{REPO}/load/k6:/scripts:ro",
        "-v",
        f"{REPORTS}:/out",
    ]
    # k6's image runs as its own uid, which cannot write into a host directory
    # owned by someone else. Docker Desktop's VM maps uids permissively so this
    # is invisible on macOS; on Linux the summary is silently never written and
    # the run reports "no summary" with every threshold green. Write as the
    # caller instead.
    if hasattr(os, "getuid"):
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [
        K6_IMAGE,
        "run",
        "--quiet",
        "--summary-export",
        f"/out/{name}.summary.json",
        f"/scripts/{scenario['script']}",
    ]

    print(f"\n{name}: {scenario['what']}", flush=True)
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    took = time.time() - started
    if not summary.exists():
        print(proc.stdout[-2000:] or proc.stderr[-2000:])
        raise SystemExit(f"{name}: k6 produced no summary")
    data = json.loads(summary.read_text())
    metrics = data.get("metrics", {})

    def stat(metric: str, field: str):
        value = metrics.get(metric, {}).get(field)
        return round(value, 1) if isinstance(value, (int, float)) else value

    out = {
        "scenario": name,
        "what": scenario["what"],
        "passed": proc.returncode == 0,
        "seconds": round(took, 1),
        "requests": stat("http_reqs", "count"),
        "rps": stat("http_reqs", "rate"),
        "http_failed_rate": stat("http_req_failed", "rate"),
        "p50_ms": stat("http_req_duration", "med"),
        "p95_ms": stat("http_req_duration", "p(95)"),
        "p99_ms": stat("http_req_duration", "p(99)"),
        "checks_passed": stat("checks", "passes"),
        "checks_failed": stat("checks", "fails"),
    }
    for extra in ("query_ms", "describe_ms", "search_ms"):
        if extra in metrics:
            out[f"{extra}_p95"] = stat(extra, "p(95)")
    for counter in ("throttled", "served"):
        if counter in metrics:
            out[counter] = stat(counter, "count")
    if "refusals" in metrics:
        out["refusal_rate"] = stat("refusals", "rate")

    mark = "\033[32mok\033[0m" if out["passed"] else "\033[31mFAIL\033[0m"
    print(
        f"  {mark}  {out['requests']} requests at {out['rps']}/s · "
        f"p50 {out['p50_ms']}ms · p95 {out['p95_ms']}ms · p99 {out['p99_ms']}ms"
        + (
            f" · throttled {out.get('throttled')}/{_int(out.get('served')) + _int(out.get('throttled'))}"
            if "throttled" in out
            else ""
        ),
        flush=True,
    )
    if not out["passed"]:
        for line in (proc.stdout or "").splitlines():
            if "threshold" in line.lower() or "✗" in line:
                print("      " + line.strip())
    return out


EXECUTOR_LABEL = "com.calvinx.das.executor"


def _labels_of(container_ids: list[str]) -> dict[str, str]:
    """container id -> executor label, for every container given."""
    out: dict[str, str] = {}
    for cid in container_ids:
        image = subprocess.run(
            ["docker", "inspect", "-f", "{{.Image}}", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not image:
            continue
        label = subprocess.run(
            ["docker", "inspect", "-f", f'{{{{index .Config.Labels "{EXECUTOR_LABEL}"}}}}', image],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if label:
            out[cid] = label
    return out


def _service_containers() -> list[str]:
    """Every container compose currently has for the service, newest first.

    Newest first because the interesting moment is just after a swap: during
    `--force-recreate` compose can still be tearing the outgoing container
    down, and asking for one id can hand back the one on its way out.
    """
    listed = subprocess.run(
        ["docker", "compose", "ps", "-q", "warehouse-query"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    ).stdout.split()
    if not listed:
        return []
    dated = []
    for cid in listed:
        created = subprocess.run(
            ["docker", "inspect", "-f", "{{.Created}}", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        dated.append((created, cid))
    return [cid for _, cid in sorted(dated, reverse=True)]


def running_executor(settle_seconds: float = 15.0) -> str | None:
    """Which implementation the stack is actually running, from the image label.

    A comparison is only worth having if each half measured what it claims to.
    Nothing in the HTTP surface says which implementation answered — by design,
    since the two are meant to be indistinguishable to a client — so the answer
    comes from the image the container was built from.

    Reading one container is not enough. `docker compose up --wait` returns
    once the NEW container is healthy, which is not the same as the old one
    being gone: for a short window both exist and `ps -q` lists both. Taking
    the first id then reports whichever compose happened to name first, and on
    a slower machine that is the container being torn down — the swap looks
    like it never happened. It passed on a laptop and failed in CI for exactly
    that reason.

    So: read every container compose has, and if they disagree, wait for the
    old one to go rather than believe either. Disagreement is transient by
    definition; a caller that cannot wait it out gets None and the guard
    refuses, which is still the safe direction.
    """
    try:
        deadline = time.time() + settle_seconds
        newest: str | None = None
        while True:
            containers = _service_containers()
            if not containers:
                return None
            labels = _labels_of(containers)
            if not labels:
                return None
            newest = labels.get(containers[0])
            if len(set(labels.values())) == 1:
                return newest
            if time.time() >= deadline:
                # Say what was seen rather than picking one: a comparison that
                # measured the wrong binary is the failure this exists to stop.
                print(
                    f"the stack has {len(labels)} warehouse-query containers and they "
                    f"disagree ({sorted(set(labels.values()))}); "
                    "a swap is still in progress."
                )
                return newest
            time.sleep(0.5)
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    ap.add_argument("--vus", type=int, default=int(os.environ.get("DAS_LOAD_VUS", "20")))
    ap.add_argument("--stage", default=os.environ.get("DAS_LOAD_STAGE", "20s"))
    ap.add_argument("--p95", type=int, default=int(os.environ.get("DAS_LOAD_P95_MS", "1500")))
    ap.add_argument("--user", default=os.environ.get("DAS_LOAD_USER", "carol@entraemulator.dev"))
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    ap.add_argument(
        "--expect-executor",
        choices=("py", "go"),
        help="refuse to measure unless the stack is running this implementation",
    )
    # The same check with no load run behind it, for callers that only need the
    # question answered. It lives here because `running_executor` does, and it
    # has to run on the HOST: the answer comes from `docker compose ps` and an
    # image label, and the tools container has neither the docker CLI nor the
    # socket. The conformance harness asked from inside that container and got
    # "unrecognised" every time -- a gate that could not pass, reporting the
    # wrong cause. See `conformance-one` in the Makefile.
    ap.add_argument(
        "--assert-executor",
        choices=("py", "go"),
        help="check which implementation the stack is running, then exit; no load is generated",
    )
    args = ap.parse_args()

    if args.assert_executor:
        actual = running_executor()
        if actual != args.assert_executor:
            print(
                f"refusing to continue: expected the {args.assert_executor} executor, "
                f"the stack is running {actual or 'an image with no executor label'}.\n"
                f"Rebuild with DAS_EXECUTOR={args.assert_executor} first."
            )
            return 2
        print(f"executor: {actual} (verified from the image label)")
        return 0

    REPORTS.mkdir(exist_ok=True)

    # Refuse rather than measure the wrong binary. A py-vs-go comparison that
    # silently measured one implementation twice once reported go as three
    # times SLOWER than py, inverting the ADR's own finding, and the witness
    # accepted it because it only asserted that a difference existed.
    if args.expect_executor:
        actual = running_executor()
        if actual != args.expect_executor:
            print(
                f"refusing to run: expected the {args.expect_executor} executor, "
                f"the stack is running {actual or 'an unlabelled image'}.\n"
                f"Rebuild with DAS_EXECUTOR={args.expect_executor} before measuring."
            )
            return 2
        print(f"executor: {actual} (verified from the image label)")

    names = args.only or list(SCENARIOS)

    # The gateway's allowance is part of the experiment rather than a fixed
    # fact: a throughput run must not be measuring the rate limiter, and the
    # run that proves the limiter needs it low enough to hit in seconds.
    from seed import common as c

    configured = int(c.CFG.get("DAS_RATE_CALLS", "60"))
    results = []
    for name in names:
        set_rate_limit(configured if name == "ratelimit" else 1_000_000)
        results.append(run_scenario(name, args))
    set_rate_limit(configured)
    by_name = {r["scenario"]: r for r in results}

    report = {
        "vus": args.vus,
        "stage": args.stage,
        "p95_threshold_ms": args.p95,
        "scenarios": results,
    }

    if "query" in by_name and "query-direct" in by_name:
        gateway, direct = by_name["query"], by_name["query-direct"]
        tax = {
            "p50_ms": round((gateway["p50_ms"] or 0) - (direct["p50_ms"] or 0), 1),
            "p95_ms": round((gateway["p95_ms"] or 0) - (direct["p95_ms"] or 0), 1),
            "rps_change_pct": round(
                100 * ((gateway["rps"] or 0) - (direct["rps"] or 1)) / (direct["rps"] or 1), 1
            ),
        }
        report["gateway_cost"] = tax
        print(
            f"\ngateway cost (through APIM − direct): "
            f"p50 +{tax['p50_ms']}ms · p95 +{tax['p95_ms']}ms · "
            f"throughput {tax['rps_change_pct']}%"
        )

    stamp = os.environ.get("DAS_REPORT_STAMP") or str(int(time.time()))
    out = REPORTS / f"load-{stamp}.json"
    out.write_text(json.dumps(report, indent=1))
    print(f"\nreport: {out}")
    print("Numbers describe this machine, not Fabric capacity; what transfers is the shape.")

    failed = [r["scenario"] for r in results if not r["passed"]]
    if failed:
        print(f"thresholds not met: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
