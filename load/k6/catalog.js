// The catalog path: gateway passthrough to OpenMetadata's own MCP server, with
// the read-only bot swapped in at the gateway. Separate from the query path
// because it is a different backend with a different cost.
import { check } from 'k6';
import { Trend } from 'k6/metrics';
import { signIn, toolCall, toolOk, GATEWAY } from './lib.js';

const searchMs = new Trend('search_ms', true);
const PATH = __ENV.DAS_OM_MCP_PATH || '/om/mcp';
const KEY = __ENV.DAS_OM_SUBSCRIPTION_KEY || '';
const TERMS = ['revenue', 'customer', 'fiscal', 'product', 'segment', 'orders'];

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
    'search_ms': [`p(95)<${__ENV.DAS_LOAD_P95_MS || 2000}`],
  },
};

export function setup() {
  return { token: signIn() };
}

export default function (data) {
  const term = TERMS[Math.floor(Math.random() * TERMS.length)];
  const res = toolCall(GATEWAY, PATH, data.token, 'search_metadata',
                       { query: term, entity_type: 'table' },
                       KEY ? { 'Ocp-Apim-Subscription-Key': KEY } : {});
  searchMs.add(res.timings.duration);
  check(res, { 'search ok': () => toolOk(res) });
}
