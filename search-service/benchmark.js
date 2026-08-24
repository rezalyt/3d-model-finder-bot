import fs from 'node:fs/promises';
import { BENCHMARK_QUERIES } from './benchmark-queries.js';
import { scoreModel, normalizeModel } from './search-quality.js';

const POLYHAVEN_API_URL = process.env.POLYHAVEN_API_URL || 'https://api.polyhaven.com/assets';
const POLYHAVEN_FILES_URL = process.env.POLYHAVEN_FILES_URL || 'https://api.polyhaven.com/files';
const AMBIENTCG_API_URL = process.env.AMBIENTCG_API_URL || 'https://ambientcg.com/api/v2/full_json';
const USER_AGENT = process.env.POLYHAVEN_BENCHMARK_USER_AGENT || '3DModelFinder-Benchmark/1.1';
const TOP_K = 5;
const PAGE_SIZE = 100;
const MAX_PAGES = 200;
const POLYHAVEN_FILE_CONCURRENCY = 8;

function normalize(value) {
  return String(value || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').replace(/\s+/g, ' ').trim();
}

function normalizeFormat(value) {
  return String(value || '').toLowerCase().replace(/^\./, '');
}

function assetToPolyHavenModel(asset, formats = []) {
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
    formats,
    metadata: {
      ...metadata,
      polycount: metadata.polycount,
      dimensions: metadata.dimensions,
      lods: metadata.lods,
      textures: metadata.max_resolution,
      assetType: 'model',
    },
  });
}

function extractPolyHavenFormats(filesPayload) {
  const formats = new Set();
  for (const file of Object.values(filesPayload || {})) {
    const formatsByType = file?.formats || file?.format || {};
    if (typeof formatsByType === 'string') formats.add(normalizeFormat(formatsByType));
    for (const key of Object.keys(formatsByType || {})) formats.add(normalizeFormat(key));
    if (file?.name) {
      const match = String(file.name).match(/\.([a-z0-9]+)$/i);
      if (match) formats.add(normalizeFormat(match[1]));
    }
  }
  return [...formats].filter(Boolean);
}

function extractAmbientFormats(asset) {
  const formats = new Set();
  const folders = asset?.downloadFolders?.default?.downloadFiletypeCategories || {};
  for (const key of Object.keys(folders)) formats.add(normalizeFormat(key));
  const fileFormats = asset?.fileFormats || asset?.formats || asset?.downloadFormats || [];
  for (const format of Array.isArray(fileFormats) ? fileFormats : []) formats.add(normalizeFormat(format));
  return [...formats].filter(Boolean);
}

function classifyAmbientType(asset) {
  const category = normalize([asset?.displayCategory, asset?.category].filter(Boolean).join(' '));
  if (/hdr|hdri|environment|sky/.test(category)) return 'environment';
  if (/material|texture|surface/.test(category)) return 'material';
  return 'model';
}

function assetToAmbientCGModel(asset) {
  const metadata = asset || {};
  const tags = Array.isArray(metadata.tags)
    ? metadata.tags
    : (metadata.tagData?.tags || metadata.tagData || []);
  const dimensions = [metadata.dimensionX, metadata.dimensionY, metadata.dimensionZ]
    .map(Number)
    .filter(Number.isFinite);
  const assetType = classifyAmbientType(metadata);
  return normalizeModel({
    name: metadata.displayName || metadata.assetName || metadata.assetId,
    description: metadata.description || metadata.displayCategory || '',
    source: 'ambientcg',
    sourceUrl: metadata.assetId ? `https://ambientcg.com/view?id=${metadata.assetId}` : '',
    thumbnailUrl: metadata.previewImage || metadata.previewData?.previewImage || metadata.previewData?.image,
    categories: [metadata.displayCategory, metadata.category].filter(Boolean),
    tags: Array.isArray(tags) ? tags : [],
    license: 'CC0',
    formats: extractAmbientFormats(metadata),
    metadata: {
      ...metadata,
      dimensions: dimensions.length === 3 ? dimensions : undefined,
      polycount: metadata.polycount ?? metadata.triangles ?? metadata.polygons,
      lods: metadata.lods,
      textures: metadata.maxResolution || metadata.max_resolution || metadata.resolutions,
      pbr: metadata.pbr ?? metadata.hasPbr,
      hasTextures: metadata.hasTextures,
      assetType,
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
    model.formats?.length > 0,
  ];
  return checks.filter(Boolean).length / checks.length;
}

async function fetchJson(url) {
  const response = await fetch(url, {
    headers: { 'User-Agent': USER_AGENT, Accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
  return response.json();
}

async function fetchPolyHavenAssets() {
  const payload = await fetchJson(POLYHAVEN_API_URL);
  const base = Object.entries(payload)
    .map(([id, asset]) => ({ ...asset, id }))
    .filter(asset => Number(asset.type) === 2);

  const models = [];
  let cursor = 0;
  async function worker() {
    while (true) {
      const index = cursor++;
      if (index >= base.length) return;
      const asset = base[index];
      let formats = [];
      try {
        const files = await fetchJson(`${POLYHAVEN_FILES_URL}/${encodeURIComponent(asset.id)}`);
        formats = extractPolyHavenFormats(files);
      } catch (error) {
        console.warn(`Poly Haven files metadata unavailable for ${asset.id}: ${error.message}`);
      }
      models.push(assetToPolyHavenModel(asset, formats));
    }
  }
  await Promise.all(Array.from({ length: Math.min(POLYHAVEN_FILE_CONCURRENCY, base.length) }, worker));
  return models.filter(Boolean);
}

async function fetchAmbientCGAssets() {
  const assets = [];
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const offset = page * PAGE_SIZE;
    const url = new URL(AMBIENTCG_API_URL);
    url.searchParams.set('type', 'Model');
    url.searchParams.set('limit', String(PAGE_SIZE));
    url.searchParams.set('offset', String(offset));
    url.searchParams.set('include', 'tagData,previewData,dimensionsData,downloadData');

    const payload = await fetchJson(url.toString());
    const found = Array.isArray(payload?.foundAssets) ? payload.foundAssets : [];
    assets.push(...found.map(assetToAmbientCGModel).filter(Boolean));
    if (!found.length || !payload?.nextPageHttp) break;
  }
  return assets;
}

function runVertical(models, vertical) {
  const profile = vertical === 'landscape'
    ? { task: 'landscape', category: null, software: null }
    : { task: 'architecture', category: null, software: null };

  const rows = [];
  for (const query of BENCHMARK_QUERIES[vertical]) {
    const candidates = models.filter(model => model.metadata?.assetType === 'model' && queryMatches(model, query));
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
      formats: [...new Set(scored.flatMap(item => item.model.formats || []))],
      sources: [...new Set(scored.map(item => item.model.source))],
      topModels: scored.map(item => ({
        name: item.model.name,
        source: item.model.source,
        score: Math.round(item.score * 10) / 10,
        polycount: item.model.metadata?.polycount ?? null,
        dimensions: item.model.metadata?.dimensions ?? null,
        lods: Boolean(item.model.metadata?.lods),
        formats: item.model.formats || [],
        url: item.model.sourceUrl,
      })),
    });
  }

  const withResults = rows.filter(row => row.resultCount > 0);
  const withThreePlus = rows.filter(row => row.resultCount >= 3);
  const strongMetadata = rows.filter(row => row.topMetadataCompleteness >= 0.66);
  const formatCounts = rows.flatMap(row => row.formats).reduce((acc, format) => {
    acc[format] = (acc[format] || 0) + 1;
    return acc;
  }, {});
  const sourceCounts = rows.flatMap(row => row.sources).reduce((acc, source) => {
    acc[source] = (acc[source] || 0) + 1;
    return acc;
  }, {});

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
      formatCounts,
      sourceCounts,
    },
  };
}

function runAssetLayerSummary(models) {
  return models.reduce((acc, model) => {
    const type = model.metadata?.assetType || 'unknown';
    acc[type] = (acc[type] || 0) + 1;
    return acc;
  }, {});
}

const [polyhavenAssets, ambientcgAssets] = await Promise.all([
  fetchPolyHavenAssets(),
  fetchAmbientCGAssets(),
]);

const models = [...polyhavenAssets, ...ambientcgAssets];
const providerSummary = {
  polyhaven: { modelCount: polyhavenAssets.length, status: 'ok' },
  ambientcg: { modelCount: ambientcgAssets.length, status: 'ok' },
};

const result = {
  generatedAt: new Date().toISOString(),
  providers: providerSummary,
  assetLayers: runAssetLayerSummary(models),
  modelCount: models.length,
  verticals: {
    landscape: runVertical(models, 'landscape'),
    architecture: runVertical(models, 'architecture'),
  },
};

await fs.writeFile('benchmark-results.json', `${JSON.stringify(result, null, 2)}\n`);

console.log(`ASSET LAYERS: ${JSON.stringify(result.assetLayers)}`);
for (const [vertical, report] of Object.entries(result.verticals)) {
  const s = report.summary;
  console.log(`${vertical.toUpperCase()}: coverage=${(s.coverageRate * 100).toFixed(1)}%, 3+ coverage=${(s.threePlusCoverageRate * 100).toFixed(1)}%, metadata>=66%=${(s.strongMetadataRate * 100).toFixed(1)}%, avg results=${s.averageResultsPerQuery.toFixed(1)}, avg top score=${s.averageTopScore.toFixed(1)}`);
  console.log(`${vertical.toUpperCase()} formats: ${JSON.stringify(s.formatCounts)}`);
  console.log(`${vertical.toUpperCase()} sources: ${JSON.stringify(s.sourceCounts)}`);
}
