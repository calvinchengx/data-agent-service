import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';
import { defineCollection } from 'astro:content';

// The docs collection is generated into src/content/docs by
// scripts/sync-docs.ts, from the canonical Markdown in /docs.
export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
};
