import fs from 'node:fs/promises';
import { BENCHMARK_QUERIES } from './benchmark-queries.js';
import { scoreModel, normalizeModel } from './search-quality.js';

const API_URL = process.env.POLYHAVEN_API_URL || 'https://api.polyhaven.com/assets';
const USER_AGENT = process.env.POLYHAVEN_BENCHMARK_USER_AGENT || '3DModelFinder-Benchmark/1.0';
const TOP_K = 5;

function normalize(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function assetToModel(asset) {
  const metadata = asset || {};
  return normalizeModel({
    name: metadata.name,
    description: metadata.description,
    source: 'polyhaven',
    sourceUrl: metadata.id ? `https://polyhaven.com/a/${metadata.id}` : '',
    thumbnailUrl: metadata.thumbnail_url,
    categories: metadata.category ? [metadata.category] : [],
    tags: metadata.tags || [],
    license: 'CC0',
    formats: metadata.formats || [],
    metadata: {
      ...metadata,
      polycount: metadata.polycount,
      dimensions: metadata.dimensions,
      lods: metadata.lods,
      textures: metadata.max_resolution,
    },
  });
}

function searchableText(model) {
  return normalize([
    model.name,
    model.description,
    ...(model.tags || []),
    ...(model.categories || []),
  ].join(' '));
}

function queryMatches(model, query) {
  const haystack = searchableText(model);
  const terms = normalize(query).split(' ').filter(term => term.length >= 3);
  if (!terms.length) return false;
  return terms.every(term => haystack.includes(term));
}

function metadataCompleteness(model) {
  const m = model.metadata || {};
  const checks = [
    Boolean(model.thumbnailUrl),
    Array.isArray(m.dimensions) && m.dimensions.length === 3,
    Number.isFinite(Number(m.polycount)),
    Boolean(m.lods),
    Array.isArray(model.tags) && model.tags.length > 0,
    Boolean(model.description),
  ];
  return checks.filter(Boolean).length / checks.length;
}

async function fetchAssets() {
  const response = await fetch(API_URL, { headers: { 'User-Agent': USER_AGENT } });
  if (!response.ok) throw new Error(`Poly Haven API HTTP ${response.status}`);
  return response.json();
}

function runVertical(models, vertical) {
  const profile = vertical === 'landscape'
    ? { task: 'landscape', category: null, software: null }
    : { task: 'architecture', category: null, software: null };

  const rows = [];
  for (const query of BENCHMARK_QUERIES[vertical]) {
    const candidates = models.filter(model => queryMatches(model, query));
    const scored = candidates
      .map(model => ({ model, score: scoreModel(model, query, null, profile) }))
      .filter(item => Number.isFinite(item.score))
      .sort((a, b) => b.score - a.score)
      .slice(0, TOP_K);

    rows.push({
      query,
      resultCount: candidates.length,
      topScore: scored[0]?.score ?? 0,
      topMetadataCompleteness: scored[0] ? metadataCompleteness(scored[0].model) : 0,
      topModels: scored.map(item => ({
        name: item.model.name,
        score: Math.round(item.score * 10) / 10,
        polycount: item.model.metadata?.polycount ?? null,
        dimensions: item.model.metadata?.dimensions ?? null,
        lods: Boolean(item.model.metadata?.lods),
        url: item.model.sourceUrl,
      })),
    });
  }

  const withResults = rows.filter(row => row.resultCount > 0);
  const withThreePlus = rows.filter(row => row.resultCount >= 3);
  const strongMetadata = rows.filter(row => row.topMetadataCompleteness >= 0.66);
  return {
    vertical,
    queries: rows,
    summary: {
      totalQueries: rows.length,
      coverageRate: rows.length ? withResults.length / rows.length : 0,
      threePlusCoverageRate: rows.length ? withThreePlus.length / rows.length : 0,
      strongMetadataRate: rows.length ? strongMetadata.length / rows.length : 0,
      averageTopScore: rows.length ? rows.reduce((sum, row) => sum + row.topScore, 0) / rows.length : 0,
      averageResultsPerQuery: rows.length ? rows.reduce((sum, row) => sum + row.resultCount, 0) / rows.length : 0,
    },
  };
}

const assetsPayload = await fetchAssets();
const modelAssets = Object.entries(assetsPayload)
  .map(([id, asset]) => ({ ...asset, id }))
  .filter(asset => Number(asset.type) === 2)
  .map(assetToModel);

const result = {
  generatedAt: new Date().toISOString(),
  provider: 'polyhaven',
  modelCount: modelAssets.length,
  source: API_URL,
  verticals: {
    landscape: runVertical(modelAssets, 'landscape'),
    architecture: runVertical(modelAssets, 'architecture'),
  },
};

await fs.writeFile('benchmark-results.json', `${JSON.stringify(result, null, 2)}\n`);

for (const [vertical, report] of Object.entries(result.verticals)) {
  const s = report.summary;
  console.log(`${vertical.toUpperCase()}: coverage=${(s.coverageRate * 100).toFixed(1)}%, 3+ coverage=${(s.threePlusCoverageRate * 100).toFixed(1)}%, metadata>=66%=${(s.strongMetadataRate * 100).toFixed(1)}%, avg results=${s.averageResultsPerQuery.toFixed(1)}, avg top score=${s.averageTopScore.toFixed(1)}`);
}
