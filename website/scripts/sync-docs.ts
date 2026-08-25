// Generates Starlight content from the canonical Markdown in /docs, keeping
// /docs as the single source of truth: those files stay pristine and their
// GitHub-relative links keep working, while the site gets its own routes.
//
// For each doc it derives the title from the leading H1, injects Starlight
// frontmatter pointing "Edit this page" at the real file, drops the duplicate
// H1, and rewrites intra-doc links to site routes.
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const REPO = join(here, '..', '..');
const DOCS_SRC = join(REPO, 'docs');
const OUT = join(here, '..', 'src', 'content', 'docs');
const BASE = '/data-agent-service/docs/';
// Diagrams live beside the docs so `img/x.svg` resolves on GitHub. On the site
// the pages are flat `<base>/<slug>/` routes, so that relative path would look
// one level too deep -- the files are copied into public/ and the references
// rewritten to absolute. Same single source, both renderings.
const IMG_SRC = join(REPO, 'docs', 'img');
const IMG_OUT = join(here, '..', 'public', 'img');
const IMG_RE = /(src|srcset)="img\/([^"]+)"/g;
const REPO_URL = 'https://github.com/calvinchengx/data-agent-service';

// The docs worth publishing: `NN-name.md` chapters, plus the two living
// references that carry no reading-order number.
const DOC_RE = /^(\d{2}-[a-z0-9-]+|parity|upstream-issues)\.md$/;
// ADRs live one level down and keep that shape in the URL.
const ADR_DIR = 'adr';

// `](./|docs/ NN-slug.md#anchor)` -> `](/data-agent-service/NN-slug/#anchor)`.
const LINK_RE =
  /\]\((?:\.\/|docs\/)?(\d{2}-[a-z0-9-]+|parity|upstream-issues)\.md(#[^)]*)?\)/g;
// `](adr/0001-x.md)` and `](../adr/0001-x.md)` from a chapter or an ADR sibling.
const ADR_LINK_RE = /\]\((?:\.\.\/)?adr\/([a-z0-9-]+)\.md(#[^)]*)?\)/g;
// Repo-relative links (`../seed`, `../services/...`) are correct on GitHub,
// where /docs sits one level under the root — but they are dead on a site
// whose pages are flat `/<base>/<slug>/` routes with nothing above them.
// Rewriting them to absolute GitHub URLs is what keeps ONE source working in
// both renderings; the alternative is editing /docs into something that no
// longer resolves on GitHub. A path matching nothing is reported rather than
// silently linked into a 404.
const REPO_LINK_RE = /\]\(\.\.\/([^)#]+)(#[^)]*)?\)/g;

let warnings = 0;

function rewriteRepoLinks(md: string, where: string): string {
  return md.replace(REPO_LINK_RE, (_match, path: string, anchor?: string) => {
    const clean = path.replace(/\/+$/, '');
    if (clean.startsWith(`${ADR_DIR}/`)) return `](${BASE}${clean.replace(/\.md$/, '')}/${anchor ?? ''})`;
    const target = join(REPO, clean);
    const exists = existsSync(target);
    if (!exists) {
      console.warn(`sync-docs: WARNING ${where}: ../${path} matches nothing in the repo`);
      warnings += 1;
    }
    const kind = exists && statSync(target).isDirectory() ? 'tree' : 'blob';
    return `](${REPO_URL}/${kind}/main/${clean}${anchor ?? ''})`;
  });
}

function rewriteLinks(md: string, where: string): string {
  const chapters = md.replace(
    LINK_RE,
    (_match, slug: string, anchor?: string) => `](${BASE}${slug}/${anchor ?? ''})`,
  );
  const withAdrs = chapters.replace(
    ADR_LINK_RE,
    (_match, slug: string, anchor?: string) => `](${BASE}${ADR_DIR}/${slug}/${anchor ?? ''})`,
  );
  const withImages = withAdrs.replace(
    IMG_RE,
    (_match, attr: string, file: string) => `${attr}="${BASE}img/${file}"`,
  );
  return rewriteRepoLinks(withImages, where);
}

// "ADR 0001 — Two executor implementations" keeps its number; a chapter's
// leading "07 — " does not, because the sidebar already orders it.
function cleanTitle(h1: string): string {
  return h1.replace(/^\d{2}\s*[—:-]\s*/, '').trim();
}

function yamlEscape(s: string): string {
  // Backslashes first, then quotes — otherwise a literal backslash in a title
  // leaks through and corrupts the double-quoted YAML scalar.
  return `"${s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
}

function convert(relative: string): string {
  const raw = readFileSync(join(DOCS_SRC, relative), 'utf8');
  const h1 = raw.split('\n').find((line) => /^#\s+/.test(line));
  const title = h1 ? cleanTitle(h1.replace(/^#\s+/, '')) : relative.replace(/\.md$/, '');
  const lines = raw.split('\n');
  const h1Index = lines.findIndex((line) => /^#\s+/.test(line));
  if (h1Index >= 0) {
    // Starlight renders the frontmatter title, so the H1 would be a duplicate.
    lines.splice(h1Index, lines[h1Index + 1]?.trim() === '' ? 2 : 1);
  }
  const body = rewriteLinks(lines.join('\n').replace(/^\n+/, ''), relative);
  const editUrl = `${REPO_URL}/edit/main/docs/${relative}`;
  return `---\ntitle: ${yamlEscape(title)}\neditUrl: ${yamlEscape(editUrl)}\n---\n\n${body}`;
}

// The landing page is synthesized here rather than taken from /docs, because
// the repository's front door is README.md and duplicating it would give two
// things to keep true.
function writeIndex(): void {
  const body =
    `Natural-language questions over governed data — grounded in the glossary, metrics and\n` +
    `schema held in OpenMetadata, fronted by Azure API Management, and answered under the\n` +
    `asking user's own Entra identity.\n\n` +
    `Everything here runs locally against the [Azure emulator family](https://github.com/calvinchengx/emulators),\n` +
    `and the same code runs against real Azure — switching is configuration, not a code path.\n\n` +
    `## Start here\n\n` +
    `- [Quick start](01-quickstart.md) — the whole stack from nothing\n` +
    `- [Architecture](03-architecture.md) — what each component is for\n` +
    `- [MCP clients](09-mcp-clients.md) — connect Claude, Cursor or VS Code with no custom code\n` +
    `- [Authorization](05-authorization.md) — how one user sees different rows than another\n` +
    `- [Classification](19-classification.md) — how a sensitivity label in OpenMetadata becomes a withheld column\n` +
    `- [Running against real Azure](10-production.md) — what changes, and what does not\n\n` +
    `## How claims are checked\n\n` +
    `- [Evaluation](07-evaluation.md) — does the catalog change the answer?\n` +
    `- [Load testing](08-load-testing.md) — and what the gateway costs\n` +
    `- [Parity](parity.md) — what is witnessed, and what is not yet\n`;
  const frontmatter =
    `---\ntitle: Overview\ndescription: A governed data agent — natural-language questions over Fabric, ` +
    `PostgreSQL and more, grounded in OpenMetadata and answered as the asking user.\neditUrl: false\n---\n\n`;
  writeFileSync(join(OUT, 'index.md'), frontmatter + rewriteLinks(body, 'index'));
}

rmSync(OUT, { recursive: true, force: true });
mkdirSync(join(OUT, ADR_DIR), { recursive: true });

// Only the generated pair is published; docs/img/src is the authored source
// and belongs in the repository, not on the site.
rmSync(IMG_OUT, { recursive: true, force: true });
mkdirSync(IMG_OUT, { recursive: true });
const diagrams = existsSync(IMG_SRC)
  ? readdirSync(IMG_SRC).filter((n) => n.endsWith('.svg'))
  : [];
for (const name of diagrams) {
  cpSync(join(IMG_SRC, name), join(IMG_OUT, name));
}

const chapters = readdirSync(DOCS_SRC).filter((name) => DOC_RE.test(name)).sort();
for (const name of chapters) {
  writeFileSync(join(OUT, name), convert(name));
}

const adrDir = join(DOCS_SRC, ADR_DIR);
const adrs = existsSync(adrDir)
  ? readdirSync(adrDir).filter((name) => name.endsWith('.md')).sort()
  : [];
for (const name of adrs) {
  writeFileSync(join(OUT, ADR_DIR, name), convert(join(ADR_DIR, name)));
}

writeIndex();

// A page nobody can navigate to is a page nobody reads. The sidebar is
// curated rather than generated -- reading order is an editorial decision --
// so the risk is a doc being added and silently never appearing in it. That
// happened once already: docs/13-testing.md was published, linked from the
// README's coverage badges, and absent from every menu.
const config = readFileSync(join(here, '..', 'astro.config.ts'), 'utf8');
const listed = new Set(
  [...config.matchAll(/slug: '([^']+)'/g)].map((m) => m[1]),
);
const generated = [
  ...chapters.map((n) => n.replace(/\.md$/, '')),
  ...adrs.map((n) => `${ADR_DIR}/${n.replace(/\.md$/, '')}`),
];
const unreachable = generated.filter((slug) => !listed.has(slug));
if (unreachable.length) {
  console.error(
    `sync-docs: these documents are published but absent from the sidebar in ` +
      `astro.config.ts, so nothing links to them: ${unreachable.join(', ')}`,
  );
  process.exit(1);
}

console.log(
  `sync-docs: ${chapters.length} chapters, ${adrs.length} ADR(s), ` +
    `${diagrams.length} diagram(s), all reachable from the sidebar, ` +
    `${warnings} warning(s)`,
);
