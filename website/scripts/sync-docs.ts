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

// The inventory is taken BEFORE anything is converted, because the link
// validator below reads it: a set populated later would make every early call
// see an empty set and either warn about everything or (worse) check nothing.
const chapters = readdirSync(DOCS_SRC).filter((name) => DOC_RE.test(name)).sort();
const adrDir = join(DOCS_SRC, ADR_DIR);
const adrs = existsSync(adrDir)
  ? readdirSync(adrDir).filter((name) => name.endsWith('.md')).sort()
  : [];
const KNOWN = new Set(chapters.map((n) => n.replace(/\.md$/, '')));
if (KNOWN.size === 0) {
  console.error(`sync-docs: no chapters found in ${DOCS_SRC} — refusing to publish nothing`);
  process.exit(1);
}

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
  // A slug matching no document became a silent 404: this rewrote the path
  // without ever asking whether the target existed, while `../` links one
  // function down HAVE been checked all along. Same failure, two link shapes.
  const withChapters = md.replace(
    LINK_RE,
    (_match, slug: string, anchor?: string) => {
      if (!KNOWN.has(slug)) {
        console.warn(`sync-docs: WARNING ${where}: links to ${slug}.md, which is not published`);
        warnings += 1;
      }
      return `](${BASE}${slug}/${anchor ?? ''})`;
    },
  );
  const withAdrs = withChapters.replace(
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
  titles.set(relative.replace(/\.md$/, ''), title);
  const body = rewriteLinks(lines.join('\n').replace(/^\n+/, ''), relative);
  const editUrl = `${REPO_URL}/edit/main/docs/${relative}`;
  return `---\ntitle: ${yamlEscape(title)}\neditUrl: ${yamlEscape(editUrl)}\n---\n\n${body}`;
}

// One line per document, saying why a reader would open it. This is the ONLY
// editorial content here: the grouping and order come from the sidebar in
// astro.config.ts, and the link text comes from each document's own H1.
//
// A document appears on the front door if and only if it has a gloss. That
// makes omission deliberate and visible — the run reports every sidebar entry
// without one — rather than the previous arrangement, where the front door was
// a hand-written list that quietly listed 9 of 25 because it was written when
// there were 9.
const GLOSS: Record<string, string> = {
  '01-quickstart': 'the whole stack from nothing',
  '03-architecture': 'what each component is for',
  '09-mcp-clients': 'connect Claude, Cursor or VS Code with no custom code',
  '20-ask-service': 'a ticket, then a stream, so a client can speak first',
  '05-authorization': 'how one user sees different rows than another, and which apps may ask',
  '19-classification': 'a label in OpenMetadata becomes a column the answer will not show',
  // Deliberately not "authz tier". gitleaks' generic-api-key rule reads
  // `auth` as a keyword and captures the NEXT quoted key -- the following
  // slug -- as the secret. .gitleaks.toml allowlists that shape by VALUE so
  // the history stays scannable, but keeping the prose clear of the keyword
  // means the working tree does not lean on the allowlist to be clean.
  '09-adding-a-source': 'an adapter, a dialect, and whose permissions apply',
  '07-evaluation': 'does the catalog change the answer?',
  '08-load-testing': 'and what the gateway costs',
  '13-testing': 'what each layer of the suite is for',
  '11-ci': 'every gate, and why a red job is not always the cause',
  '10-production': 'what changes, and what does not',
  '18-releases': 'two executors answering one contract, and which image to pull',
  '09-llm-governance': 'capping and billing per person, without naming the person',
  '21-llm-backends': 'protocols rather than vendors, so any of them is a base URL',
  '12-promotion': 'a recurring question becomes a dashboard, with no prose stored',
  '14-publishing': 'Power BI, Superset and Tableau from one plan',
  '15-adding-a-dashboard-target': 'one plan, another renderer',
  '15-http-sources': 'an OpenAPI document is the allow-list; GraphQL is not built',
  '16-go-parity': 'what each implementation can do, measured',
  parity: 'the ledger that separates the emulators from real Azure',
  'upstream-issues': 'what is broken beneath us, filed rather than worked around',
};

// Titles come from the documents themselves, recorded as they are converted,
// so a renamed heading cannot leave the front door calling it the old thing.
const titles = new Map<string, string>();

/** The sidebar's groups, in order, with their slugs — the single structure. */
function sidebarGroups(): [string, string[]][] {
  const config = readFileSync(join(here, '..', 'astro.config.ts'), 'utf8');
  const groups: [string, string[]][] = [];
  for (const m of config.matchAll(/label: '([^']+)',\s*items: \[(.*?)\]/gs)) {
    groups.push([m[1], [...m[2].matchAll(/slug: '([^']+)'/g)].map((s) => s[1])]);
  }
  return groups;
}

// The landing page is synthesized here rather than taken from /docs, because
// the repository's front door is README.md and duplicating it would give two
// things to keep true.
function writeIndex(): Set<string> {
  const linked = new Set<string>();
  let body =
    `Natural-language questions over governed data — grounded in the glossary, metrics and\n` +
    `schema held in OpenMetadata, fronted by Azure API Management, and answered under the\n` +
    `asking user's own Entra identity.\n\n` +
    `Everything here runs locally against the [Azure emulator family](https://github.com/calvinchengx/emulators),\n` +
    `and the same code runs against real Azure — switching is configuration, not a code path.\n\n`;

  for (const [heading, slugs] of sidebarGroups()) {
    const entries = slugs.filter((s) => s !== 'index' && GLOSS[s]);
    if (!entries.length) continue;
    body += `## ${heading}\n\n`;
    for (const slug of entries) {
      linked.add(slug);
      body += `- [${titles.get(slug) ?? slug}](${slug}.md) — ${GLOSS[slug]}\n`;
    }
    body += `\n`;
  }

  const frontmatter =
    `---\ntitle: Overview\ndescription: A governed data agent — natural-language questions over Fabric, ` +
    `PostgreSQL and more, grounded in OpenMetadata and answered as the asking user.\neditUrl: false\n---\n\n`;
  writeFileSync(join(OUT, 'index.md'), frontmatter + rewriteLinks(body, 'index'));
  return linked;
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

for (const name of chapters) {
  writeFileSync(join(OUT, name), convert(name));
}

for (const name of adrs) {
  writeFileSync(join(OUT, ADR_DIR, name), convert(join(ADR_DIR, name)));
}

const onTheFrontDoor = writeIndex();

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

// Reported, not enforced: see writeIndex. Naming them is the point — a count
// alone would not say WHICH page grew without a way in from the front door.
const offTheFrontDoor = generated.filter((slug) => !onTheFrontDoor.has(slug));
if (offTheFrontDoor.length) {
  console.log(
    `sync-docs: ${offTheFrontDoor.length} document(s) have no one-line gloss, so they are ` +
      `reachable from the sidebar only: ${offTheFrontDoor.join(', ')}`,
  );
}

console.log(
  `sync-docs: ${chapters.length} chapters, ${adrs.length} ADR(s), ` +
    `${diagrams.length} diagram(s), all reachable from the sidebar, ` +
    `${generated.length - offTheFrontDoor.length}/${generated.length} on the front door, ` +
    `${warnings} warning(s)`,
);
