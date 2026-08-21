// Does the gateway's rate limit actually bite? Deliberately exceeds the
// configured allowance from one caller and asserts that the excess is refused
// with 429 rather than served. A limit nobody has watched fire is a comment.
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { signIn, toolCall, GATEWAY } from './lib.js';

const throttled = new Counter('throttled');
const served = new Counter('served');
const PATH = __ENV.DAS_WAREHOUSE_MCP_PATH || '/warehouse/mcp';
const ALLOWANCE = Number(__ENV.DAS_RATE_CALLS || 60);

export const options = {
  insecureSkipTLSVerify: true,
  // p99 is not in k6's default export; a tail this run cannot see is a tail
  // nobody will notice regressing.
  summaryTrendStats: ['avg', 'min', 'med', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    burst: { executor: 'shared-iterations', vus: 5,
             iterations: Math.ceil(ALLOWANCE * 1.6), maxDuration: '40s' },
  },
  thresholds: { 'throttled': ['count>0'] },
};

export function setup() {
  return { token: signIn() };
}

export default function (data) {
  const res = toolCall(GATEWAY, PATH, data.token, 'list_sources', {});
  if (res.status === 429) throttled.add(1);
  else served.add(1);
  check(res, { 'served or throttled': (r) => r.status === 200 || r.status === 429 });
}
