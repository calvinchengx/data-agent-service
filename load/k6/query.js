// The hot path: MCP tool calls through the gateway to the executor, which
// signs the caller in on-behalf-of and runs SQL. Mixed workload, because a
// real agent describes a table before it queries one.
import { check, group } from 'k6';
import { Trend, Rate } from 'k6/metrics';
import { signIn, toolCall, toolOk, GATEWAY, EXECUTOR } from './lib.js';

const TARGET = (__ENV.DAS_LOAD_TARGET || 'gateway') === 'gateway';
const BASE = TARGET ? GATEWAY : EXECUTOR;
const PATH = TARGET ? (__ENV.DAS_WAREHOUSE_MCP_PATH || '/warehouse/mcp') : '/mcp';

const describeMs = new Trend('describe_ms', true);
const queryMs = new Trend('query_ms', true);
const refusals = new Rate('refusals');

const QUERIES = [
  "SELECT fiscal_year_label, SUM(revenue_usd) AS net FROM dbo.fct_revenue_summary GROUP BY fiscal_year_label ORDER BY 1",
  "SELECT country, SUM(revenue_usd) AS net FROM dbo.fct_revenue_summary GROUP BY country",
  "SELECT TOP 20 product_name, list_price_usd FROM dbo.dim_product ORDER BY list_price_usd DESC",
  "SELECT channel_system, SUM(units) AS units FROM dbo.fct_revenue_summary GROUP BY channel_system",
  "SELECT COUNT(*) AS n FROM dbo.fct_sales",
];

export const options = {
  insecureSkipTLSVerify: true,
  // p99 is not in k6's default export; a tail this run cannot see is a tail
  // nobody will notice regressing.
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    ramp: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: __ENV.DAS_LOAD_STAGE || '20s', target: Number(__ENV.DAS_LOAD_VUS_LOW || 5) },
        { duration: __ENV.DAS_LOAD_STAGE || '20s', target: Number(__ENV.DAS_LOAD_VUS_HIGH || 20) },
        { duration: '10s', target: 0 },
      ],
      gracefulRampDown: '5s',
    },
  },
  thresholds: {
    'http_req_failed': ['rate<0.01'],
    'refusals': ['rate<0.01'],
    'query_ms': [`p(95)<${__ENV.DAS_LOAD_P95_MS || 1500}`],
    'describe_ms': [`p(95)<${__ENV.DAS_LOAD_P95_MS || 1500}`],
  },
};

export function setup() {
  return { token: signIn() };
}

export default function (data) {
  group('describe', () => {
    const res = toolCall(BASE, PATH, data.token, 'describe_table',
                         { table: 'dbo.fct_revenue_summary' });
    describeMs.add(res.timings.duration);
    const ok = toolOk(res);
    refusals.add(!ok);
    check(res, { 'describe ok': () => ok });
  });
  group('query', () => {
    const sql = QUERIES[Math.floor(Math.random() * QUERIES.length)];
    const res = toolCall(BASE, PATH, data.token, 'run_query', { sql });
    queryMs.add(res.timings.duration);
    const ok = toolOk(res);
    refusals.add(!ok);
    check(res, { 'query ok': () => ok });
  });
}
