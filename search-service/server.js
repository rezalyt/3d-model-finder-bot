import express from 'express';
import { Fetch3D } from '@pikal6/3dfetch';

const app = express();
const port = Number(process.env.PORT || 8787);
const LIMIT = Number(process.env.SEARCH_LIMIT || 12);
const PROVIDER_LIMIT = Math.max(LIMIT, Number(process.env.PROVIDER_LIMIT || 30));
const TIMEOUT_MS = Number(process.env.HTTP_TIMEOUT_MS || 15000);
const CACHE_MS = 60_000;

const ALIASES = { stp: 'step', igs: 'iges', blender: 'blend' };
const FORMATS = new Set([
  'gltf','glb','obj','fbx','blend','usd','usdz','stl','3mf','dae','ply','step','iges','off',
  'max','c4d','ma','mb','abc','3ds','maya','fb'
]);

const PROVIDER_META = {
  polypizza: { priority: 120, formats: ['glb','fbx','obj'], mode: 'direct', auth: 'api-key' },
  free3d: { priority: 115, formats: ['fbx','glb','obj','stl','max','blend','c4d','abc'], mode: 'direct-public' },
  polyhaven: { priority: 110, formats: ['blend','fbx','gltf','obj','usd'], mode: 'direct' },
  printables: { priority: 100, formats: ['stl'], mode: 'direct' },
  sketchfab: { priority: 90, formats: ['glb','gltf','usd','usdz','obj','fbx'], mode: 'catalog' },
  smithsonian: { priority: 85, formats: ['stl','glb','gltf','obj','ply','blend','f3z'], mode: 'direct', auth: 'api-key' },
  nasa: { priority: 80, formats: ['3ds','blend','fb','glb','max','maya','stl'], mode: 'direct' },
};

let fetch3d = null;
try { fetch3d = new Fetch3D({ timeout: TIMEOUT_MS }); }
catch (error) { console.error('3dfetch unavailable:', error.message); }

const cache = new Map();
let polyhavenCache = null;
let polyhavenCacheAt = 0;
let nasaTree = null;
let nasaTreeAt = 0;
let threeDDDLatest = null;
let threeDDDAt = 0;

function normFormat(value) {
  const key = String(value || '').trim().toLowerCase().replace(/^\./, '');
  return ALIASES[key] || key;
}

function parseQuery(raw = '') {
  const parts = raw.trim().split(/\s+/).filter(Boolean);
  let format = null;
  const terms = [];
  for (const part of parts) {
    const f = normFormat(part);
    if (!format && FORMATS.has(f)) format = f;
    else terms.push(part);
  }
  return { query: terms.join(' ').trim(), format };
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

function normalizeFormats(value) {
  if (Array.isArray(value)) {
    return [...new Set(value.flatMap(item => {
      if (typeof item === 'string') return [normFormat(item)];
      if (item && typeof item === 'object') return [item.format, item.extension, item.name].filter(Boolean).map(normFormat);
      return [];
    }).filter(Boolean))];
  }
  if (typeof value === 'string') return [...new Set(value.split(/[\s,|;/]+/).map(normFormat).filter(Boolean))];
  return [];
}

function normalize(item, source, forcedFormats = null) {
  if (!item) return null;
  return {
    name: String(item.name || item.title || item.nameText || 'Без названия').trim(),
    source,
    sourceUrl: item.sourceUrl || item.viewerUrl || item.modelPageUrl || item.modelUrl || item.url || '',
    thumbnail: item.thumbnail || item.thumbnailUrl || item.previewMediumUrl || item.preview || null,
    formats: forcedFormats ? normalizeFormats(forcedFormats) : normalizeFormats(item.formats || item.available_formats || item.extensions),
    license: item.license || item.license_type || item.licence || null,
    access: item.access || null,
    text: [item.description, item.category, item.subcategory, item.tags, item.keywords].flat().filter(Boolean).join(' '),
  };
}

function score(model, query) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  const title = model.name.toLowerCase();
  const text = `${model.name} ${model.text}`.toLowerCase();
  let total = 0;
  for (const term of terms) {
    if (title === term) total += 160;
    else if (title.startsWith(term)) total += 125;
    else if (title.includes(term)) total += 90;
    else if (text.includes(term)) total += 35;
    else return -1;
  }
  if (title === query.toLowerCase()) total += 120;
  return total;
}

function rank(results, query, format) {
  const seen = new Set();
  return results.map(model => ({
    ...model,
    score: score(model, query),
    providerPriority: PROVIDER_META[model.source]?.priority || 40,
  }))
  .filter(model => model.score >= 0)
  .filter(model => !format || model.formats.includes(format))
  .filter(model => {
    const key = `${model.source}:${model.sourceUrl || model.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  })
  .sort((a, b) => b.score - a.score || b.providerPriority - a.providerPriority || a.name.localeCompare(b.name))
  .slice(0, LIMIT);
}

async function searchPrintables(query, format) {
  if (format && format !== 'stl') return [];
  const body = {
    query: `query SearchPrints($query: String!, $limit: Int, $ordering: SearchChoicesEnum) { searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) { items { id name slug license { id name } stls { id name fileSize filePreviewPath } } } }`,
    variables: { query, limit: PROVIDER_LIMIT, ordering: 'best_match' },
  };
  const data = await fetchJson('https://api.printables.com/graphql/', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'user-agent': '3DModelFinderBot/1.0' },
    body: JSON.stringify(body),
  });
  if (data.errors?.length) throw new Error(data.errors.map(e => e.message).join('; '));
  return (data?.data?.searchPrints2?.items || []).filter(x => x.stls?.length).map(x => normalize({
    name: x.name,
    sourceUrl: `https://www.printables.com/model/${x.id}-${x.slug}`,
    formats: ['stl'],
    license: x.license?.name || null,
  }, 'printables'));
}

async function searchPolyPizza(query, format) {
  const key = process.env.POLY_PIZZA_KEY;
  if (!key || (format && !PROVIDER_META.polypizza.formats.includes(format))) return [];
  const params = new URLSearchParams({ limit: String(PROVIDER_LIMIT) });
  if (format) params.set('format', format);
  const data = await fetchJson(`https://api.poly.pizza/v1/search/${encodeURIComponent(query)}?${params}`, {
    headers: { 'X-Auth-Token': key, 'User-Agent': '3DModelFinderBot/1.0' },
  });
  const rows = data?.results || data?.items || data?.data || [];
  return rows.map(item => {
    const formats = [];
    if (item.Download || item.download) formats.push('glb');
    if (item.DownloadFBX || item.downloadFbx || item.fbx) formats.push('fbx');
    if (item.DownloadOBJ || item.downloadObj || item.obj) formats.push('obj');
    return normalize({
      name: item.Title || item.title || item.name,
      sourceUrl: item.URL || item.url || `https://poly.pizza/m/${item.ID || item.id}`,
      thumbnail: item.Thumbnail || item.thumbnail,
      formats,
      license: item.Licence || item.license,
      description: item.Description,
      tags: item.Tags || item.tags,
    }, 'polypizza');
  }).filter(model => model?.name && model.formats.length && (!format || model.formats.includes(format)));
}

async function searchFree3D(query, format) {
  if (format && !PROVIDER_META.free3d.formats.includes(format)) return [];
  const params = new URLSearchParams({ q: query, limit: String(PROVIDER_LIMIT) });
  if (format) params.set('format', format);
  try {
    const data = await fetchJson(`https://free3d.online/api-embeddings/?${params}`, {
      headers: { Accept: 'application/json', 'User-Agent': '3DModelFinderBot/1.0' },
    });
    const rows = Array.isArray(data) ? data : (data.results || data.items || data.data || data.models || []);
    return rows.map(item => normalize({
      name: item.name || item.title || item.nameText,
      sourceUrl: item.modelPageUrl || item.modelUrl || item.canonicalUrl || item.url || (item.slug ? `https://free3d.online/model/${item.slug}` : ''),
      thumbnail: item.thumbnail || item.thumbnailUrl || item.previewMediumUrl || item.preview,
      formats: item.formats || item.available_formats || item.fileFormats || item.extensions,
      license: item.license || item.license_type,
      description: item.description,
      category: item.category,
      subcategory: item.subcategory,
      keywords: item.keywords,
    }, 'free3d'))
    .filter(model => model?.name && model.sourceUrl && (!format || model.formats.includes(format)));
  } catch (error) {
    console.error('free3d search:', error.message);
    return [];
  }
}

async function searchSketchfab(query, format) {
  const params = new URLSearchParams({ q: query, type: 'models', limit: String(PROVIDER_LIMIT), sort_by: '-relevance' });
  const headers = process.env.SKETCHFAB_API_TOKEN ? { Authorization: `Token ${process.env.SKETCHFAB_API_TOKEN}` } : {};
  const data = await fetchJson(`https://api.sketchfab.com/v3/search?${params}`, { headers });
  return (data.results || []).map(item => normalize({
    name: item.name,
    sourceUrl: item.viewerUrl,
    thumbnail: item.thumbnails?.images?.[0]?.url,
    formats: item.formats,
    license: item.license?.label || null,
    description: item.description,
    tags: item.tags,
  }, 'sketchfab')).filter(model => !format || model.formats.includes(format));
}

async function searchPolyHaven(query, format) {
  if (format && !PROVIDER_META.polyhaven.formats.includes(format)) return [];
  if (!polyhavenCache || Date.now() - polyhavenCacheAt > CACHE_MS) {
    polyhavenCache = await fetchJson('https://api.polyhaven.com/assets?type=models', {
      headers: { 'User-Agent': '3DModelFinderBot/1.0' },
    });
    polyhavenCacheAt = Date.now();
  }
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  return Object.entries(polyhavenCache).map(([id, item]) => normalize({
    name: item.name || id,
    sourceUrl: `https://polyhaven.com/a/${id}`,
    thumbnail: item.thumbnail_url,
    formats: PROVIDER_META.polyhaven.formats,
    license: 'CC0',
    description: item.description,
    tags: item.tags,
    category: item.category,
  }, 'polyhaven')).filter(model => {
    const text = `${model.name} ${model.text}`.toLowerCase();
    return terms.every(term => text.includes(term)) && (!format || model.formats.includes(format));
  }).slice(0, PROVIDER_LIMIT);
}

async function searchNasa(query, format) {
  if (format && !PROVIDER_META.nasa.formats.includes(format)) return [];
  if (!nasaTree || Date.now() - nasaTreeAt > 1800000) {
    const data = await fetchJson('https://api.github.com/repos/nasa/NASA-3D-Resources/git/trees/master?recursive=1', {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': '3DModelFinderBot/1.0' },
    });
    nasaTree = Array.isArray(data.tree) ? data.tree : [];
    nasaTreeAt = Date.now();
  }
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  const groups = new Map();
  for (const entry of nasaTree) {
    if (entry.type !== 'blob') continue;
    const match = entry.path.match(/^3D Models\/([^/]+)\/(.+)$/i);
    if (!match) continue;
    const rel = match[2];
    const ext = rel.includes('.') ? rel.split('.').pop().toLowerCase() : '';
    const fmt = ext === 'blender' ? 'blend' : ext;
    if (!PROVIDER_META.nasa.formats.includes(fmt)) continue;
    if (!terms.every(term => `${match[1]} ${rel}`.toLowerCase().includes(term))) continue;
    if (!groups.has(match[1])) groups.set(match[1], new Set());
    groups.get(match[1]).add(fmt);
  }
  return [...groups.entries()].slice(0, PROVIDER_LIMIT).map(([name, formats]) => normalize({
    name,
    sourceUrl: `https://github.com/nasa/NASA-3D-Resources/tree/master/3D%20Models/${encodeURIComponent(name)}`,
    formats: [...formats],
    license: 'NASA Media Usage Guidelines',
  }, 'nasa')).filter(model => !format || model.formats.includes(format));
}

async function searchSmithsonian(query, format) {
  const key = process.env.SMITHSONIAN_API_KEY;
  if (!key) return [];
  const params = new URLSearchParams({ q: query, rows: String(PROVIDER_LIMIT), api_key: key });
  if (format) params.set('model_type', format);
  const data = await fetchJson(`https://3d-api.si.edu/api/3d/v1/search?${params}`, { headers: { 'User-Agent': '3DModelFinderBot/1.0' } });
  return (data?.rows || data?.results || []).map(item => normalize({
    name: item.name || item.title,
    sourceUrl: item.url || item.sourceUrl || item._links?.self?.href || '',
    thumbnail: item.thumbnail,
    formats: item.model_type ? [normFormat(item.model_type)] : [],
    license: item.license || 'Smithsonian Open Access',
    description: item.description,
  }, 'smithsonian')).filter(model => !format || model.formats.includes(format));
}

async function searchFallback(query, format) {
  if (!fetch3d) return [];
  try {
    const result = await fetch3d.searchAll(format ? { query, formats: [format], limit: PROVIDER_LIMIT } : { query, limit: PROVIDER_LIMIT });
    return (result?.models || []).map(item => normalize(item, item.source || '3dfetch-fallback')).filter(model => !format || model.formats.includes(format));
  } catch (error) {
    console.error('3dfetch fallback:', error.message);
    return [];
  }
}

async function searchModels(query, format) {
  const cacheKey = `${query.toLowerCase()}::${format || 'any'}`;
  const cached = cache.get(cacheKey);
  if (cached && Date.now() - cached.at < CACHE_MS) return cached.data;

  const providers = [
    ['printables', searchPrintables],
    ['polypizza', searchPolyPizza],
    ['free3d', searchFree3D],
    ['polyhaven', searchPolyHaven],
    ['sketchfab', searchSketchfab],
    ['nasa', searchNasa],
    ['smithsonian', searchSmithsonian],
  ];

  const settled = await Promise.allSettled(providers.map(([, fn]) => fn(query, format)));
  let all = [];
  for (let i = 0; i < settled.length; i++) {
    if (settled[i].status === 'fulfilled') all.push(...settled[i].value);
    else console.error(`${providers[i][0]} failed:`, settled[i].reason?.message || settled[i].reason);
  }

  // Fallback is additive, never a replacement for direct providers.
  all.push(...await searchFallback(query, format));
  const ranked = rank(all, query, format);
  cache.set(cacheKey, { at: Date.now(), data: ranked });
  return ranked;
}

async function get3DDDLatest() {
  if (threeDDDLatest && Date.now() - threeDDDAt < 1800000) return threeDDDLatest;
  const data = await fetchJson('https://models.3ddd.ru/api/models/last', {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json', 'user-agent': '3DModelFinderBot/1.0 (+link-only)' },
    body: '{}',
  });
  if (!data?.success || !Array.isArray(data.data)) throw new Error('Unexpected 3DDD response');
  threeDDDLatest = data.data.map(item => normalize({
    name: item.title || item.titleEn || item.slug,
    sourceUrl: `https://3ddd.ru/3dmodels/show/${item.slug}`,
    thumbnail: item.image ? `https://3ddd.ru/${String(item.image).replace(/^\//, '')}` : null,
    formats: [],
    license: item.typeText === 'pro' ? 'PRO on 3DDD' : item.typeText === 'free' ? 'FREE on 3DDD' : null,
    access: 'link-only',
  }, '3ddd'));
  threeDDDAt = Date.now();
  return threeDDDLatest;
}

app.get('/health', (_req, res) => res.json({
  ok: true,
  service: '3d-model-finder-search',
  directProviders: Object.keys(PROVIDER_META),
  specialProviders: ['3ddd'],
  fallback: '3dfetch',
  configured: {
    polypizza: Boolean(process.env.POLY_PIZZA_KEY),
    sketchfab: Boolean(process.env.SKETCHFAB_API_TOKEN),
    smithsonian: Boolean(process.env.SMITHSONIAN_API_KEY),
  },
  threeDFetchLoaded: Boolean(fetch3d),
}));

app.get('/providers', (_req, res) => res.json({
  providers: Object.entries(PROVIDER_META).map(([name, meta]) => ({
    name, ...meta,
    configured: name === 'polypizza' ? Boolean(process.env.POLY_PIZZA_KEY) : name === 'smithsonian' ? Boolean(process.env.SMITHSONIAN_API_KEY) : true,
  })),
  special: { name: '3ddd', mode: 'latest-link-only', formats: 'not exposed by current /api/models/last' },
  fallback: { name: '3dfetch', mode: 'fallback' },
}));

app.get('/latest/3ddd', async (_req, res) => {
  try { const results = await get3DDDLatest(); res.json({ source: '3ddd', count: results.length, results }); }
  catch (error) { res.status(502).json({ error: '3ddd latest failed', details: String(error?.message || error) }); }
});

app.get('/diagnostics/search', async (req, res) => {
  const parsed = parseQuery(String(req.query.q || ''));
  const format = normFormat(req.query.format || parsed.format) || null;
  if (!parsed.query) return res.status(400).json({ error: 'q is required' });

  const providers = [
    ['printables', searchPrintables], ['polypizza', searchPolyPizza], ['free3d', searchFree3D],
    ['polyhaven', searchPolyHaven], ['sketchfab', searchSketchfab], ['nasa', searchNasa],
    ['smithsonian', searchSmithsonian], ['3dfetch-fallback', searchFallback],
  ];
  const result = {};
  for (const [name, fn] of providers) {
    try {
      const rows = await fn(parsed.query, format);
      result[name] = { ok: true, count: rows.length, examples: rows.slice(0, 5).map(x => ({ name: x.name, formats: x.formats, url: x.sourceUrl })) };
    } catch (error) {
      result[name] = { ok: false, error: String(error?.message || error) };
    }
  }
  res.json({ query: parsed.query, format, providers: result });
});

app.get('/search', async (req, res) => {
  const parsed = parseQuery(String(req.query.q || ''));
  const format = normFormat(req.query.format || parsed.format) || null;
  if (!parsed.query) return res.status(400).json({ error: 'q is required' });
  try {
    const results = await searchModels(parsed.query, format);
    res.json({ query: parsed.query, format, count: results.length, results });
  } catch (error) {
    res.status(502).json({ error: 'search failed', details: String(error?.message || error) });
  }
});

app.listen(port, '0.0.0.0', () => console.log(`3D search service listening on port ${port}`));
