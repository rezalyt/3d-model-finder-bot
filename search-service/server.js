import express from 'express';
import { Fetch3D } from '@pikal6/3dfetch';

const app = express();
const port = Number(process.env.PORT || 8787);
const LIMIT = Math.min(20, Math.max(1, Number(process.env.SEARCH_LIMIT || 12)));
const PROVIDER_LIMIT = Math.min(40, Math.max(LIMIT, Number(process.env.PROVIDER_LIMIT || 30)));
const TIMEOUT_MS = Math.min(30_000, Math.max(5_000, Number(process.env.HTTP_TIMEOUT_MS || 15_000)));
const CACHE_MS = Math.max(30_000, Number(process.env.SEARCH_CACHE_TTL_MS || 60_000));
const CACHE_MAX = Math.max(32, Number(process.env.SEARCH_CACHE_MAX_ENTRIES || 256));
const REQUEST_CONCURRENCY = Math.max(1, Number(process.env.PROVIDER_CONCURRENCY || 6));
const RATE_LIMIT_WINDOW_MS = Math.max(1_000, Number(process.env.RATE_LIMIT_WINDOW_MS || 60_000));
const RATE_LIMIT_MAX_REQUESTS = Math.max(1, Number(process.env.RATE_LIMIT_MAX_REQUESTS || 30));
const RATE_LIMIT_MAX_KEYS = Math.max(32, Number(process.env.RATE_LIMIT_MAX_KEYS || 4096));

const ALIASES = { stp: 'step', igs: 'iges', blender: 'blend' };
const FORMATS = new Set([
  'gltf', 'glb', 'obj', 'fbx', 'blend', 'usd', 'usdz', 'stl', '3mf', 'dae', 'ply', 'step', 'iges', 'off',
  'max', 'c4d', 'ma', 'mb', 'abc', '3ds', 'maya', 'fb',
]);
const FORMAT_RE = /(?<![a-z0-9])(?:gltf|glb|obj|fbx|blend|usd|usdz|stl|3mf|dae|ply|step|stp|iges|igs|off|max|c4d|abc|3ds)(?![a-z0-9])/i;

const RUSSIAN_ALIASES = {
  'кот': 'cat', 'кота': 'cat', 'кошка': 'cat', 'кошку': 'cat', 'кошки': 'cats',
  'собака': 'dog', 'собаку': 'dog', 'пес': 'dog', 'пса': 'dog',
  'лошадь': 'horse', 'лошади': 'horse', 'лошадью': 'horse', 'дерево': 'tree',
  'ёлка': 'christmas tree', 'елка': 'christmas tree', 'стул': 'chair', 'стулья': 'chairs',
  'кресло': 'armchair', 'диван': 'sofa', 'стол': 'table', 'машина': 'car', 'автомобиль': 'car',
  'авто': 'car', 'робот': 'robot', 'дом': 'house', 'здание': 'building', 'человек': 'human',
  'голова': 'head', 'лицо': 'face', 'череп': 'skull', 'дракон': 'dragon', 'динозавр': 'dinosaur',
};
const STOP_WORDS = new Set(['найди', 'найти', 'поищи', 'ищи', 'ищу', 'покажи', 'показать', 'модель', 'модели', '3d', 'мне', 'нужен', 'нужна', 'нужно', 'для']);

const PROVIDER_META = {
  polypizza: { priority: 120, formats: ['glb', 'fbx', 'obj'], mode: 'direct' },
  free3d: { priority: 115, formats: ['fbx', 'glb', 'obj', 'stl', 'max', 'blend', 'c4d', 'abc'], mode: 'direct-public' },
  polyhaven: { priority: 110, formats: ['blend', 'fbx', 'gltf', 'obj', 'usd'], mode: 'direct' },
  printables: { priority: 100, formats: ['stl'], mode: 'direct' },
  sketchfab: { priority: 90, formats: ['glb', 'gltf', 'usd', 'usdz', 'obj', 'fbx'], mode: 'catalog' },
  smithsonian: { priority: 85, formats: ['stl', 'glb', 'gltf', 'obj', 'ply', 'blend', 'f3z'], mode: 'direct' },
  nasa: { priority: 80, formats: ['3ds', 'blend', 'fb', 'glb', 'max', 'maya', 'stl'], mode: 'direct' },
};

let fetch3d = null;
try {
  const apiKeys = {};
  if (process.env.SKETCHFAB_API_TOKEN) apiKeys.sketchfab = process.env.SKETCHFAB_API_TOKEN;
  if (process.env.THINGIVERSE_API_TOKEN) apiKeys.thingiverse = process.env.THINGIVERSE_API_TOKEN;
  if (process.env.MYMINIFACTORY_API_KEY) apiKeys.myminifactory = process.env.MYMINIFACTORY_API_KEY;
  fetch3d = new Fetch3D({ timeout: TIMEOUT_MS, ...(Object.keys(apiKeys).length ? { apiKeys } : {}) });
} catch (error) {
  console.error('3dfetch initialization failed:', error.message);
}

const cache = new Map();
const inflight = new Map();
const rateLimit = new Map();
let polyhavenCache = null;
let polyhavenCacheAt = 0;
let nasaTree = null;
let nasaTreeAt = 0;

function normFormat(value) {
  const key = String(value || '').trim().toLowerCase().replace(/^\./, '');
  return ALIASES[key] || key;
}

function normalizeQuery(raw = '') {
  const limited = String(raw).trim().slice(0, 160);
  const match = limited.match(FORMAT_RE);
  const detectedFormat = match ? normFormat(match[0]) : null;
  const withoutFormat = match ? `${limited.slice(0, match.index)} ${limited.slice(match.index + match[0].length)}` : limited;
  const query = withoutFormat
    .toLowerCase()
    .replace(/[\r\n]+/g, ' ')
    .split(/\s+/)
    .filter(Boolean)
    .filter(token => !STOP_WORDS.has(token))
    .map(token => RUSSIAN_ALIASES[token] || token)
    .join(' ')
    .trim();
  return { query, format: detectedFormat };
}

function cacheGet(key) {
  const hit = cache.get(key);
  if (!hit) return null;
  if (Date.now() - hit.at >= CACHE_MS) {
    cache.delete(key);
    return null;
  }
  cache.delete(key);
  cache.set(key, hit);
  return hit.data;
}

function cacheSet(key, data) {
  cache.delete(key);
  cache.set(key, { at: Date.now(), data });
  while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
}

function consumeRateLimit(key) {
  const now = Date.now();
  const entry = rateLimit.get(key);
  if (!entry || now - entry.startedAt >= RATE_LIMIT_WINDOW_MS) {
    rateLimit.delete(key);
    rateLimit.set(key, { startedAt: now, count: 1 });
  } else {
    entry.count += 1;
    rateLimit.delete(key);
    rateLimit.set(key, entry);
  }

  for (const [entryKey, value] of rateLimit) {
    if (now - value.startedAt >= RATE_LIMIT_WINDOW_MS) rateLimit.delete(entryKey);
    else break;
  }
  while (rateLimit.size > RATE_LIMIT_MAX_KEYS) rateLimit.delete(rateLimit.keys().next().value);

  const current = rateLimit.get(key);
  if (current && current.count > RATE_LIMIT_MAX_REQUESTS) {
    const retryAfterSeconds = Math.max(1, Math.ceil((RATE_LIMIT_WINDOW_MS - (now - current.startedAt)) / 1000));
    return { allowed: false, retryAfterSeconds };
  }
  return { allowed: true, retryAfterSeconds: 0 };
}

function clientKey(req) {
  return req.ip || req.socket.remoteAddress || 'unknown';
}

app.set('trust proxy', 1);
app.disable('x-powered-by');

async function fetchJson(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
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
  const terms = query.split(/\s+/).filter(Boolean);
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
  if (title === query) total += 120;
  return total;
}

function rank(results, query, format) {
  const seen = new Set();
  return results
    .map(model => ({ ...model, score: score(model, query), providerPriority: PROVIDER_META[model.source]?.priority || 40 }))
    .filter(model => model.score >= 0)
    .filter(model => !format || model.formats.includes(format))
    .filter(model => model.sourceUrl)
    .filter(model => {
      const key = `${model.source}:${model.sourceUrl}`;
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
    query: `query SearchPrints($query: String!, $limit: Int, $ordering: SearchChoicesEnum) { searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) { items { id name slug license { id name } stls { id } } } }`,
    variables: { query, limit: PROVIDER_LIMIT, ordering: 'best_match' },
  };
  const data = await fetchJson('https://api.printables.com/graphql/', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'user-agent': '3DModelFinderBot/1.1' },
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
    headers: { 'X-Auth-Token': key, 'User-Agent': '3DModelFinderBot/1.1' },
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
  }).filter(Boolean).filter(model => model.formats.length && (!format || model.formats.includes(format)));
}

async function searchFree3D(query, format) {
  if (format && !PROVIDER_META.free3d.formats.includes(format)) return [];
  const params = new URLSearchParams({ q: query, limit: String(PROVIDER_LIMIT) });
  if (format) params.set('format', format);
  try {
    const data = await fetchJson(`https://free3d.online/api-embeddings/?${params}`, {
      headers: { Accept: 'application/json', 'User-Agent': '3DModelFinderBot/1.1' },
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
    }, 'free3d')).filter(model => model?.name && model.sourceUrl && (!format || model.formats.includes(format)));
  } catch (error) {
    console.warn('free3d provider failed:', error.message);
    return [];
  }
}

async function searchSketchfab(query, format) {
  if (format && !PROVIDER_META.sketchfab.formats.includes(format)) return [];
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
  }, 'sketchfab')).filter(Boolean).filter(model => !format || model.formats.includes(format));
}

async function searchPolyHaven(query, format) {
  if (format && !PROVIDER_META.polyhaven.formats.includes(format)) return [];
  if (!polyhavenCache || Date.now() - polyhavenCacheAt > 10 * 60_000) {
    polyhavenCache = await fetchJson('https://api.polyhaven.com/assets?type=models', { headers: { 'User-Agent': '3DModelFinderBot/1.1' } });
    polyhavenCacheAt = Date.now();
  }
  const terms = query.split(/\s+/).filter(Boolean);
  return Object.entries(polyhavenCache || {}).map(([id, item]) => normalize({
    name: item.name || id,
    sourceUrl: `https://polyhaven.com/a/${id}`,
    thumbnail: item.thumbnail_url,
    formats: PROVIDER_META.polyhaven.formats,
    license: 'CC0',
    description: item.description,
    tags: item.tags,
    category: item.category,
  }, 'polyhaven')).filter(Boolean).filter(model => {
    const text = `${model.name} ${model.text}`.toLowerCase();
    return terms.every(term => text.includes(term)) && (!format || model.formats.includes(format));
  }).slice(0, PROVIDER_LIMIT);
}

async function searchNasa(query, format) {
  if (format && !PROVIDER_META.nasa.formats.includes(format)) return [];
  if (!nasaTree || Date.now() - nasaTreeAt > 30 * 60_000) {
    const data = await fetchJson('https://api.github.com/repos/nasa/NASA-3D-Resources/git/trees/master?recursive=1', {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': '3DModelFinderBot/1.1' },
    });
    nasaTree = Array.isArray(data.tree) ? data.tree : [];
    nasaTreeAt = Date.now();
  }
  const terms = query.split(/\s+/).filter(Boolean);
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
  if (!key || (format && !PROVIDER_META.smithsonian.formats.includes(format))) return [];
  const params = new URLSearchParams({ q: query, rows: String(PROVIDER_LIMIT), api_key: key });
  if (format) params.set('model_type', format);
  const data = await fetchJson(`https://3d-api.si.edu/api/3d/v1/search?${params}`, { headers: { 'User-Agent': '3DModelFinderBot/1.1' } });
  return (data?.rows || data?.results || []).map(item => normalize({
    name: item.name || item.title,
    sourceUrl: item.url || item.sourceUrl || item._links?.self?.href || '',
    thumbnail: item.thumbnail,
    formats: item.model_type ? [normFormat(item.model_type)] : [],
    license: item.license || 'Smithsonian Open Access',
    description: item.description,
  }, 'smithsonian')).filter(Boolean).filter(model => !format || model.formats.includes(format));
}

async function searchFallback(query, format) {
  if (!fetch3d) return [];
  try {
    const result = await fetch3d.searchAll(format ? { query, formats: [format], limit: PROVIDER_LIMIT } : { query, limit: PROVIDER_LIMIT });
    const rows = Array.isArray(result) ? result : (result?.models || []);
    return rows.map(item => normalize(item, item.source || '3dfetch-fallback')).filter(Boolean).filter(model => !format || model.formats.includes(format));
  } catch (error) {
    console.warn('3dfetch fallback failed:', error.message);
    return [];
  }
}

async function runWithConcurrency(tasks, limit) {
  const results = new Array(tasks.length);
  let next = 0;
  async function worker() {
    while (true) {
      const index = next++;
      if (index >= tasks.length) return;
      try { results[index] = await tasks[index](); } catch (error) { results[index] = []; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
  return results.flat();
}

async function searchModels(query, format) {
  const cacheKey = `${query.toLowerCase()}::${format || 'any'}`;
  const cached = cacheGet(cacheKey);
  if (cached) return cached;
  if (inflight.has(cacheKey)) return inflight.get(cacheKey);

  const providers = [
    ['printables', () => searchPrintables(query, format)],
    ['polypizza', () => searchPolyPizza(query, format)],
    ['free3d', () => searchFree3D(query, format)],
    ['polyhaven', () => searchPolyHaven(query, format)],
    ['sketchfab', () => searchSketchfab(query, format)],
    ['nasa', () => searchNasa(query, format)],
    ['smithsonian', () => searchSmithsonian(query, format)],
    ['3dfetch-fallback', () => searchFallback(query, format)],
  ];
  const promise = runWithConcurrency(providers.map(([, fn]) => fn), REQUEST_CONCURRENCY)
    .then(all => {
      const ranked = rank(all, query, format);
      cacheSet(cacheKey, ranked);
      return ranked;
    })
    .finally(() => inflight.delete(cacheKey));
  inflight.set(cacheKey, promise);
  return promise;
}

app.get('/health', (_req, res) => res.json({
  ok: true,
  service: '3d-model-finder-search',
  threeDFetchLoaded: Boolean(fetch3d),
  providers: Object.keys(PROVIDER_META),
  cacheEntries: cache.size,
}));

app.get('/providers', (_req, res) => res.json({
  providers: Object.entries(PROVIDER_META).map(([name, meta]) => ({
    name, ...meta,
    configured: name === 'polypizza' ? Boolean(process.env.POLY_PIZZA_KEY) :
      name === 'smithsonian' ? Boolean(process.env.SMITHSONIAN_API_KEY) : true,
  })),
  fallbackLoaded: Boolean(fetch3d),
}));

app.get('/search', async (req, res) => {
  const limitResult = consumeRateLimit(clientKey(req));
  if (!limitResult.allowed) {
    res.set('Retry-After', String(limitResult.retryAfterSeconds));
    return res.status(429).json({ error: 'rate limit exceeded', retryAfterSeconds: limitResult.retryAfterSeconds });
  }

  const parsed = normalizeQuery(String(req.query.q || ''));
  const format = normFormat(req.query.format || parsed.format) || null;
  if (!parsed.query) return res.status(400).json({ error: 'q is required' });
  if (format && !FORMATS.has(format)) return res.status(400).json({ error: 'unsupported format' });
  try {
    const results = await searchModels(parsed.query, format);
    res.json({ query: parsed.query, format, count: results.length, results });
  } catch (error) {
    console.error('search failed:', error.message);
    res.status(502).json({ error: 'search failed' });
  }
});

app.listen(port, '0.0.0.0', () => console.log(`3D search service listening on port ${port}`));
