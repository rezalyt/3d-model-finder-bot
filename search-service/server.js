import express from 'express';
import { Fetch3D } from '@pikal6/3dfetch';

const app = express();
const port = Number(process.env.PORT || 8787);
const SEARCH_LIMIT = Number(process.env.SEARCH_LIMIT || 12);
const TIMEOUT_MS = Number(process.env.HTTP_TIMEOUT_MS || 15000);
const NASA_TREE_TTL_MS = 30 * 60 * 1000;

const FORMAT_ALIASES = {
  gltf: 'gltf', glb: 'glb', obj: 'obj', fbx: 'fbx', blend: 'blend', usd: 'usd', usdz: 'usdz',
  stl: 'stl', '3mf': '3mf', dae: 'dae', ply: 'ply', step: 'step', stp: 'step',
  iges: 'iges', igs: 'iges', off: 'off', max: 'max', c4d: 'c4d', ma: 'ma', mb: 'mb', abc: 'abc',
};

const CAPABILITIES = [
  { name: 'printables', mode: 'direct', formats: ['stl', '3mf'], auth: 'none', formatFilter: 'metadata' },
  { name: 'sketchfab', mode: 'direct', formats: ['glb', 'gltf', 'obj', 'fbx', 'blend', 'usd', 'usdz', 'stl', '3mf', 'dae', 'ply', 'abc', 'max', 'c4d'], auth: 'token-optional', formatFilter: 'metadata' },
  { name: 'polyhaven', mode: 'direct', formats: ['blend', 'fbx', 'gltf', 'obj', 'usd'], auth: 'none', formatFilter: 'metadata' },
  { name: 'nasa', mode: 'direct', formats: ['3ds', 'blender', 'fb', 'glb', 'max', 'maya', 'stl'], auth: 'none', formatFilter: 'path-extension' },
  { name: 'smithsonian', mode: 'direct', formats: ['stl', 'glb', 'gltf', 'obj', 'ply', 'blend', 'f3z'], auth: 'api-key-optional', formatFilter: 'native' },
  { name: '3dfetch-fallback', mode: 'fallback', formats: 'provider-dependent', auth: 'provider-dependent', formatFilter: 'normalized' },
];

let fetch3d = null;
try {
  const apiKeys = {};
  if (process.env.SKETCHFAB_API_TOKEN) apiKeys.sketchfab = process.env.SKETCHFAB_API_TOKEN;
  if (process.env.THINGIVERSE_API_TOKEN) apiKeys.thingiverse = process.env.THINGIVERSE_API_TOKEN;
  if (process.env.MYMINIFACTORY_API_KEY) apiKeys.myminifactory = process.env.MYMINIFACTORY_API_KEY;
  fetch3d = new Fetch3D({
    ...(Object.keys(apiKeys).length ? { apiKeys } : {}),
    timeout: TIMEOUT_MS,
  });
} catch (error) {
  console.error('3dfetch unavailable:', error.message);
}

let nasaTreeCache = null;
let nasaTreeCacheTime = 0;
let polyhavenCache = null;
let polyhavenCacheTime = 0;

function normalizeFormat(value) {
  const key = String(value || '').trim().toLowerCase().replace(/^\./, '');
  return FORMAT_ALIASES[key] || key;
}

function parseQuery(raw = '') {
  const parts = raw.trim().split(/\s+/).filter(Boolean);
  let format = null;
  const kept = [];
  for (const part of parts) {
    const key = normalizeFormat(part);
    if (!format && FORMAT_ALIASES[key]) format = key;
    else kept.push(part);
  }
  return { query: kept.join(' ').trim(), format };
}

function normalize(item, source) {
  if (!item) return null;
  const formats = Array.isArray(item.formats)
    ? [...new Set(item.formats.map(normalizeFormat).filter(Boolean))]
    : [];
  return {
    name: item.name || item.title || 'Без названия',
    source,
    sourceUrl: item.sourceUrl || item.viewerUrl || item.url || '',
    thumbnail: item.thumbnail || item.thumbnailUrl || null,
    formats,
    license: item.license || null,
  };
}

function matchesQuery(item, query) {
  if (!query) return true;
  const haystack = [item.name, item.description, ...(item.tags || []), ...(item.categories || [])]
    .filter(Boolean).join(' ').toLowerCase();
  return query.toLowerCase().split(/\s+/).every(term => haystack.includes(term));
}

async function fetchJson(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function searchPrintables(query, format) {
  if (format && !['stl', '3mf'].includes(format)) return [];
  const body = {
    query: `query SearchPrints($query: String!, $limit: Int, $ordering: SearchChoicesEnum) {
      searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) {
        items { id name slug license { id name } stls { id name fileSize filePreviewPath } }
        totalCount
      }
    }`,
    variables: { query, limit: SEARCH_LIMIT, ordering: 'best_match' },
  };
  const data = await fetchJson('https://api.printables.com/graphql/', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'user-agent': '3DModelFinderBot/1.0' },
    body: JSON.stringify(body),
  });
  if (data.errors?.length) throw new Error(data.errors.map(e => e.message).join('; '));
  const items = data?.data?.searchPrints2?.items || [];
  return items
    .filter(x => Array.isArray(x.stls) && x.stls.length > 0)
    .map(x => normalize({
      name: x.name,
      sourceUrl: `https://www.printables.com/model/${x.id}-${x.slug}`,
      formats: ['stl'],
      license: x.license?.name || null,
    }, 'printables'));
}

async function searchSketchfab(query, format) {
  const params = new URLSearchParams({ q: query, type: 'models', limit: String(SEARCH_LIMIT), sort_by: '-relevance' });
  const headers = {};
  if (process.env.SKETCHFAB_API_TOKEN) headers.Authorization = `Token ${process.env.SKETCHFAB_API_TOKEN}`;
  const data = await fetchJson(`https://api.sketchfab.com/v3/search?${params}`, { headers });
  return (data.results || [])
    .map(item => normalize({
      name: item.name,
      sourceUrl: item.viewerUrl,
      thumbnailUrl: item.thumbnails?.images?.[0]?.url || null,
      formats: Array.isArray(item.formats) ? item.formats : [],
      license: item.license?.label || null,
    }, 'sketchfab'))
    .filter(Boolean)
    .filter(item => !format || item.formats.includes(format));
}

async function getPolyHavenAssets() {
  if (polyhavenCache && Date.now() - polyhavenCacheTime < NASA_TREE_TTL_MS) return polyhavenCache;
  polyhavenCache = await fetchJson('https://api.polyhaven.com/assets?type=models', {
    headers: { 'User-Agent': '3DModelFinderBot/1.0' },
  });
  polyhavenCacheTime = Date.now();
  return polyhavenCache;
}

async function searchPolyHaven(query, format) {
  const supported = ['blend', 'fbx', 'gltf', 'obj', 'usd'];
  if (format && !supported.includes(format)) return [];
  const assets = await getPolyHavenAssets();
  const q = query.toLowerCase();
  return Object.entries(assets)
    .filter(([id, item]) => {
      const haystack = [id, item.name, item.description, ...(item.tags || []), item.category]
        .filter(Boolean).join(' ').toLowerCase();
      return q.split(/\s+/).every(term => haystack.includes(term));
    })
    .slice(0, SEARCH_LIMIT)
    .map(([id, item]) => normalize({
      name: item.name,
      sourceUrl: `https://polyhaven.com/a/${id}`,
      thumbnailUrl: item.thumbnail_url || null,
      formats: supported,
      license: 'CC0',
    }, 'polyhaven'))
    .filter(item => !format || item.formats.includes(format));
}

async function getNasaTree() {
  if (nasaTreeCache && Date.now() - nasaTreeCacheTime < NASA_TREE_TTL_MS) return nasaTreeCache;
  const data = await fetchJson('https://api.github.com/repos/nasa/NASA-3D-Resources/git/trees/master?recursive=1', {
    headers: { 'User-Agent': '3DModelFinderBot/1.0', Accept: 'application/vnd.github+json' },
  });
  nasaTreeCache = Array.isArray(data.tree) ? data.tree : [];
  nasaTreeCacheTime = Date.now();
  return nasaTreeCache;
}

async function searchNasa(query, format) {
  const formatMap = { '3ds': '3ds', blender: 'blend', fb: 'fb', glb: 'glb', max: 'max', maya: 'maya', stl: 'stl' };
  if (format && !Object.values(formatMap).includes(format)) return [];
  const tree = await getNasaTree();
  const q = query.toLowerCase();
  const groups = new Map();
  for (const entry of tree) {
    if (entry.type !== 'blob') continue;
    const match = entry.path.match(/^3D Models\/([^/]+)\/(.+)$/i);
    if (!match) continue;
    const rel = match[2];
    const ext = rel.includes('.') ? rel.split('.').pop().toLowerCase() : '';
    const normalizedExt = formatMap[ext] || ext;
    if (!['3ds', 'blend', 'fb', 'glb', 'max', 'maya', 'stl', 'fbx', 'obj'].includes(normalizedExt)) continue;
    const haystack = rel.toLowerCase();
    if (!q.split(/\s+/).every(term => haystack.includes(term))) continue;
    const key = match[1];
    if (!groups.has(key)) groups.set(key, new Set());
    groups.get(key).add(normalizedExt);
  }
  return [...groups.entries()].slice(0, SEARCH_LIMIT).map(([name, formats]) => normalize({
    name,
    sourceUrl: `https://github.com/nasa/NASA-3D-Resources/tree/master/3D%20Models/${encodeURIComponent(name)}`,
    formats: [...formats],
    license: 'NASA Media Usage Guidelines',
  }, 'nasa')).filter(item => !format || item.formats.includes(format));
}

async function searchSmithsonian(query, format) {
  const apiKey = process.env.SMITHSONIAN_API_KEY;
  if (!apiKey) return [];
  const params = new URLSearchParams({ q: query, rows: String(SEARCH_LIMIT), api_key: apiKey });
  if (format) params.set('model_type', format);
  const data = await fetchJson(`https://3d-api.si.edu/api/3d/v1/search?${params}`, {
    headers: { 'User-Agent': '3DModelFinderBot/1.0' },
  });
  const items = data?.rows || data?.results || [];
  return items.map(item => normalize({
    name: item.name || item.title,
    sourceUrl: item.url || item.sourceUrl || item._links?.self?.href || '',
    thumbnailUrl: item.thumbnail || null,
    formats: item.model_type ? [normalizeFormat(item.model_type)] : (format ? [format] : []),
    license: 'CC0',
  }, 'smithsonian'))
    .filter(item => !format || item.formats.includes(format));
}

async function searchFallback(query, format) {
  if (!fetch3d) return [];
  try {
    const options = format ? { query, formats: [format], limit: SEARCH_LIMIT } : { query, limit: SEARCH_LIMIT };
    const result = await fetch3d.searchAll(options, { mode: 'parallel', deduplicate: true });
    return (result?.models || []).map(item => normalize(item, item.source || '3dfetch-fallback')).filter(Boolean);
  } catch (error) {
    console.error('3dfetch fallback:', error.message);
    return [];
  }
}

async function searchModels(query, format) {
  const directTasks = [
    ['printables', searchPrintables],
    ['sketchfab', searchSketchfab],
    ['polyhaven', searchPolyHaven],
    ['nasa', searchNasa],
    ...(process.env.SMITHSONIAN_API_KEY ? [['smithsonian', searchSmithsonian]] : []),
  ];
  const settled = await Promise.allSettled(directTasks.map(([, fn]) => fn(query, format)));
  const direct = settled.flatMap((result, index) => {
    if (result.status === 'fulfilled') return result.value;
    console.error(`${directTasks[index][0]}:`, result.reason?.message || result.reason);
    return [];
  });
  const fallback = await searchFallback(query, format);
  const seen = new Set();
  return [...direct, ...fallback].filter(item => {
    if (format && !item.formats.includes(format)) return false;
    const key = `${item.source}:${item.sourceUrl || item.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, SEARCH_LIMIT * 3);
}

app.get('/health', (_req, res) => {
  res.json({
    ok: true,
    service: '3d-model-finder-search',
    directProviders: CAPABILITIES.filter(x => x.mode === 'direct').map(x => x.name),
    optionalDirectProviders: CAPABILITIES.filter(x => x.mode === 'direct' && x.auth !== 'none' && x.name !== 'sketchfab').map(x => x.name),
    fallback: '3dfetch',
    threeDFetchLoaded: Boolean(fetch3d),
  });
});

app.get('/providers', (_req, res) => {
  res.json({
    providers: CAPABILITIES,
    configured: {
      sketchfab: Boolean(process.env.SKETCHFAB_API_TOKEN),
      smithsonian: Boolean(process.env.SMITHSONIAN_API_KEY),
      thingiverse: Boolean(process.env.THINGIVERSE_API_TOKEN),
      myminifactory: Boolean(process.env.MYMINIFACTORY_API_KEY),
    },
  });
});

app.get('/search', async (req, res) => {
  const parsed = parseQuery(String(req.query.q || ''));
  const requestedFormat = normalizeFormat(req.query.format || parsed.format);
  if (!parsed.query) return res.status(400).json({ error: 'q is required' });
  try {
    const results = await searchModels(parsed.query, requestedFormat || null);
    res.json({ query: parsed.query, format: requestedFormat || null, count: results.length, results });
  } catch (error) {
    res.status(502).json({ error: 'search failed', details: String(error?.message || error) });
  }
});

app.listen(port, '0.0.0.0', () => console.log(`3D search service listening on port ${port}`));
