// The same connection with the reference TypeScript SDK. A second, independent
// implementation of the protocol is what turns "our client works" into "the
// protocol works": a Python server that only a Python client can drive would
// pass the other test and fail this one.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const [url, token] = process.argv.slice(2);

const transport = new StreamableHTTPClientTransport(new URL(url), {
  requestInit: { headers: { Authorization: `Bearer ${token}` } },
});
const client = new Client({ name: "conformance-ts", version: "1.0.0" }, { capabilities: {} });

await client.connect(transport);
const info = client.getServerVersion();
console.log(`  server: ${info.name} ${info.version}`);

const { tools } = await client.listTools();
const names = tools.map((t) => t.name).sort();
console.log(`  tools: ${JSON.stringify(names)}`);

const ok = await client.callTool({
  name: "describe_table",
  arguments: { table: "dbo.fct_revenue_summary" },
});
const described = JSON.parse(ok.content[0].text);
console.log(`  describe_table: ${described.columns.length} columns, isError=${ok.isError ?? false}`);

const refused = await client.callTool({
  name: "run_query",
  arguments: { sql: "SELECT 1; DROP TABLE dbo.fct_sales" },
});
const message = refused.content[0].text;
console.log(`  guard reaches this client as a tool error: ${refused.isError === true}`);

await client.close();
// The four core tools must be there. Not a COUNT: an executor that also
// serves HTTP sources publishes list_operations, describe_operation and
// call_operation, and a client that fails because a server offers more than it
// expected is testing the server's inventory rather than its own compatibility.
const core = ["describe_table", "list_sources", "list_tables", "run_query"];
const hasCore = core.every((t) => names.includes(t));
const good = hasCore && described.columns.length > 0 &&
  refused.isError === true && message.includes("one statement");
process.exit(good ? 0 : 1);
