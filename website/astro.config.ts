import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';

// The site is generated from /docs by scripts/sync-docs.ts, which runs before
// dev and build. /docs stays the single source of truth; nothing here is
// written twice.
export default defineConfig({
  site: 'https://calvinchengx.github.io',
  base: '/data-agent-service/docs/',
  integrations: [
    starlight({
      title: 'Data Agent Service',
      description:
        'A governed data agent: natural-language questions over Fabric, PostgreSQL and more, ' +
        'grounded in OpenMetadata and answered as the asking user.',
      components: {
        // A back-link beside the site title; the component explains why it
        // cannot live in the header icon row or the sidebar.
        SiteTitle: './src/components/SiteTitle.astro',
      },
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/calvinchengx/data-agent-service',
        },
      ],
      editLink: {
        baseUrl: 'https://github.com/calvinchengx/data-agent-service/edit/main/docs/',
      },
      sidebar: [
        {
          label: 'Getting started',
          items: [{ slug: 'index' }, { slug: '01-quickstart' }, { slug: '03-architecture' }],
        },
        {
          label: 'Using it',
          items: [
            { slug: '09-mcp-clients' },
            { slug: '20-ask-service' },
            { slug: '05-authorization' },
            { slug: '19-classification' },
            { slug: '09-adding-a-source' },
          ],
        },
        {
          label: 'Proving it',
          items: [
            { slug: '07-evaluation' },
            { slug: '08-load-testing' },
            { slug: '13-testing' },
            { slug: '11-ci' },
          ],
        },
        {
          label: 'Operating it',
          items: [
            { slug: '10-production' },
            { slug: '18-releases' },
            { slug: '09-llm-governance' },
            { slug: '21-llm-backends' },
            { slug: '12-promotion' },
            { slug: '14-publishing' },
            { slug: '15-adding-a-dashboard-target' },
          ],
        },
        {
          // The plan is the long-form argument; parity and upstream-issues are
          // living ledgers rather than chapters, so they sit at the end where
          // a reader looks things up rather than reads through.
          label: 'Reference',
          items: [
            { slug: '00-plan' },
            { slug: '15-http-sources' },
            { slug: '16-go-parity' },
            { slug: '17-sqlglot-go' },
            { slug: 'parity' },
            { slug: 'upstream-issues' },
            { slug: 'adr/0001-two-executors' },
          ],
        },
      ],
    }),
  ],
});
