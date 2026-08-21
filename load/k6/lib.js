// Shared setup for every scenario: a real user token, and the two ways to
// reach the executor — through the gateway and straight at it. Measuring both
// with the same script is what makes the gateway's cost a number rather than
// an opinion.
import http from 'k6/http';
import { check } from 'k6';

export const GATEWAY = __ENV.DAS_APIM_BASE || 'https://apim-emulator:8445';
export const EXECUTOR = __ENV.DAS_EXECUTOR_URL || 'http://warehouse-query:8090';
export const AUTHORITY = __ENV.DAS_AUTHORITY;
export const CLIENT_ID = __ENV.DAS_AGENT_CLIENT_ID;
export const AUDIENCE = __ENV.DAS_AGENT_AUDIENCE;
export const USER = __ENV.DAS_LOAD_USER || 'carol@entraemulator.dev';
export const PASSWORD = __ENV.DAS_TEST_PASSWORD || 'Password1!';

// One sign-in for the whole run, in setup(): the token is what a client holds
// for an hour, so re-minting per iteration would measure the token endpoint
// rather than the thing under test.
export function signIn() {
  const res = http.post(`${AUTHORITY}/oauth2/v2.0/token`, {
    grant_type: 'password', client_id: CLIENT_ID, username: USER,
    password: PASSWORD, scope: `${AUDIENCE}/access_as_user`,
  });
  check(res, { 'signed in': (r) => r.status === 200 });
  if (res.status !== 200) throw new Error(`sign-in failed: ${res.status} ${res.body}`);
  return JSON.parse(res.body).access_token;
}

let counter = 0;

export function rpc(base, path, token, method, params, extraHeaders) {
  const headers = Object.assign({
    'Content-Type': 'application/json',
    Accept: 'application/json, text/event-stream',
    Authorization: `Bearer ${token}`,
  }, extraHeaders || {});
  const body = JSON.stringify({ jsonrpc: '2.0', id: ++counter, method, params });
  return http.post(`${base}${path}`, body, { headers, tags: { rpc: method } });
}

export function toolCall(base, path, token, name, args, extraHeaders) {
  return rpc(base, path, token, 'tools/call', { name, arguments: args }, extraHeaders);
}

// A tool call answers 200 with `isError` inside the payload, so an HTTP-only
// check would score a refusal as a success.
export function toolOk(res) {
  if (res.status !== 200) return false;
  try {
    const result = JSON.parse(res.body).result || {};
    return result.isError !== true;
  } catch (e) {
    return false;
  }
}
