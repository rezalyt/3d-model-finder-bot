import express from 'express';
import { Fetch3D } from '@pikal6/3dfetch';
import { diversifyAndRank, inferFormatFromNameOrUrl, normalizeModel, normalizeText } from './search-quality.js';
import { MVP_SEARCH_PROVIDERS } from './provider-policy.js';
import { inferTaskProfile } from './task-profiles.js';

const app = express();
const port = Number(process.env.PORT || 8787);
const LIMIT = Math.min(20, Math.max(8, Number(process.env.SEARCH_LIMIT || 12)));
const PROVIDER_LIMIT = Math.min(50, Math.max(LIMIT, Number(process.env.PROVIDER_LIMIT || 24)));
const TIMEOUT_MS = Math.min(30_000, Math.max(5_000, Number(process.env.HTTP_TIMEOUT_MS || 15_000)));
const CACHE_MS = Math.max(30_000, Number(process.env.SEARCH_CACHE_TTL_MS || 120_000));
const CACHE_MAX = Math.max(32, Number(process.env.SEARCH_CACHE_MAX_ENTRIES || 256));
const RATE_LIMIT_WINDOW_MS = Math.max(1_000, Number(process.env.RATE_LIMIT_WINDOW_MS || 60_000));
const RATE_LIMIT_MAX_REQUESTS = Math.max(1, Number(process.env.RATE_LIMIT_MAX_REQUESTS || 30));
const RATE_LIMIT_MAX_KEYS = Math.max(32, Number(process.env.RATE_LIMIT_MAX_KEYS || 4096));
const MIN_BROAD_RESULTS = 8;

const ALIASES = { stp: 'step', igs: 'iges', blender: 'blend' };
const FORMATS = new Set(['gltf','glb','obj','fbx','blend','usd','usdz','stl','3mf','dae','ply','step','iges','off','max','c4d','ma','mb','abc','3ds','maya','fb']);
const FORMAT_RE = /(?<![a-z0-9])(?:gltf|glb|obj|fbx|blend|usd|usdz|stl|3mf|dae|ply|step|stp|iges|igs|off|max|c4d|abc|3ds)(?![a-z0-9])/i;
const RUSSIAN_ALIASES = {
  'кот':'cat','кота':'cat','кошку':'cat','кошка':'cat','кошки':'cats','котенок':'kitten','котёнок':'kitten','собака':'dog','собаку':'dog','пес':'dog','пса':'dog','щенок':'puppy','лошадь':'horse','лошади':'horse','лошадью':'horse','дерево':'tree','ёлка':'christmas tree','елка':'christmas tree','стул':'chair','стулья':'chairs','кресло':'armchair','диван':'sofa','стол':'table','машина':'car','автомобиль':'car','авто':'car','робот':'robot','дом':'house','здание':'building','человек':'human','голова':'head','лицо':'face','череп':'skull','дракон':'dragon','динозавр':'dinosaur','самолет':'airplane','самолёт':'airplane','корабль':'ship','лодка':'boat','статуя':'statue','скульптура':'sculpture','лампа':'lamp','кровать':'bed','шкаф':'cabinet','мотоцикл':'motorcycle','берёза':'birch','береза':'birch','сосна':'pine','ель':'fir','ёлка':'spruce','туя':'thuja','кустарник':'shrub','трава':'grass','камень':'rock','камни':'rocks','валун':'boulder','скамейка':'bench','светильник':'lamp','фонарь':'lamp','беседка':'gazebo','пергола':'pergola','забор':'fence','кашпо':'planter','вазон':'planter'
};
const STOP_WORDS = new Set(['найди','найти','поищи','ищи','ищу','поиск','покажи','показать','модель','модели','3d','мне','нужен','нужна','нужно','для','сделай','скачай','скачать','файл','файлы','бесплатный','бесплатная','бесплатные']);

let fetch3d = null;
try {
  const apiKeys = {};
  if (process.env.SKETCHFAB_API_TOKEN) apiKeys.sketchfab = process.env.SKETCHFAB_API_TOKEN;
  fetch3d = new Fetch3D({ timeout: TIMEOUT_MS, ...(Object.keys(apiKeys).length ? { apiKeys } : {}) });
} catch (error) {
  console.error('3dfetch initialization failed:', error.message);
}

const cache = new Map();
const inflight = new Map();
const rateLimit = new Map();

function normFormat(value) { const key = String(value || '').trim().toLowerCase().replace(/^\./, ''); return ALIASES[key] || key; }

export function normalizeQuery(raw = '') {
  const limited = String(raw).trim().slice(0, 160);
  const match = limited.match(FORMAT_RE);
  const detectedFormat = match ? normFormat(match[0]) : null;
  const withoutFormat = match ? `${limited.slice(0, match.index)} ${limited.slice(match.index + match[0].length)}` : limited;
  const query = withoutFormat.toLowerCase().replace(/[\r\n]+/g, ' ').split(/\s+/).filter(Boolean)
    .filter(token => !STOP_WORDS.has(token)).map(token => RUSSIAN_ALIASES[token] || token).join(' ').trim();
  return { query, format: detectedFormat };
}

function cacheGet(key) {
  const hit = cache.get(key);
  if (!hit) return null;
  if (Date.now() - hit.at >= CACHE_MS) { cache.delete(key); return null; }
  cache.delete(key); cache.set(key, hit); return hit.data;
}
function cacheSet(key, data) {
  cache.delete(key); cache.set(key, { at: Date.now(), data });
  while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
}
function consumeRateLimit(key) {
  const now = Date.now(); const entry = rateLimit.get(key);
  if (!entry || now - entry.startedAt >= RATE_LIMIT_WINDOW_MS) { rateLimit.delete(key); rateLimit.set(key, { startedAt: now, count: 1 }); }
  else { entry.count += 1; rateLimit.delete(key); rateLimit.set(key, entry); }
  for (const [entryKey, value] of rateLimit) { if (now - value.startedAt >= RATE_LIMIT_WINDOW_MS) rateLimit.delete(entryKey); else break; }
  while (rateLimit.size > RATE_LIMIT_MAX_KEYS) rateLimit.delete(rateLimit.keys().next().value);
  const current = rateLimit.get(key);
  return current && current.count > RATE_LIMIT_MAX_REQUESTS
    ? { allowed: false, retryAfterSeconds: Math.max(1, Math.ceil((RATE_LIMIT_WINDOW_MS - (now - current.startedAt)) / 1000)) }
    : { allowed: true, retryAfterSeconds: 0 };
}
function clientKey(req) { return req.ip || req.socket.remoteAddress || 'unknown'; }
function normalizeResultSet(models) { return (models || []).map(normalizeModel).filter(Boolean).map(model => ({ ...model, formats: [...new Set(model.formats.map(normFormat).filter(format => FORMATS.has(format)))] })); }
function mergeUnique(models) {
  const out = []; const seen = new Set();
  for (const model of normalizeResultSet(models)) {
    const key = String(model.sourceUrl || '').toLowerCase() || `${model.source}:${normalizeText(model.name)}`;
    if (seen.has(key)) continue; seen.add(key); out.push(model);
  }
  return out;
}

async function runProviderSearch(query, format = null) {
  if (!fetch3d) return { models: [], errors: { '3dfetch': 'not initialized' } };
  const options = { query, limit: PROVIDER_LIMIT };
  if (format) options.formats = [format];
  try {
    const response = await fetch3d.searchAll(options, { providers: MVP_SEARCH_PROVIDERS, mode: 'parallel', deduplicate: true });
    return { models: mergeUnique(response?.models || []), errors: response?.errors || {} };
  } catch (error) {
    return { models: [], errors: { '3dfetch': error.message } };
  }
}

function buildQueryVariants(query) {
  const variants = [query];
  const terms = normalizeText(query).split(' ').filter(Boolean);
  const meaningful = terms.filter(term => term.length >= 3);
  if (meaningful.length > 1) variants.push(meaningful.slice(-2).join(' '));
  return [...new Set(variants)].slice(0, 2);
}

async function searchModels(query, format, profile) {
  const cacheKey = `${normalizeText(query)}::${format || 'any'}::${profile?.task || 'landscape'}::${profile?.category || ''}::${profile?.software || ''}::v4`;
  const cached = cacheGet(cacheKey);
  if (cached) return cached;
  if (inflight.has(cacheKey)) return inflight.get(cacheKey);

  const promise = (async () => {
    const all = []; const providerErrors = {}; const variants = buildQueryVariants(query);
    const strictResults = await Promise.all(variants.map(variant => runProviderSearch(variant, format)));
    for (const result of strictResults) { all.push(...result.models); Object.assign(providerErrors, result.errors); }

    if (format && all.length < MIN_BROAD_RESULTS) {
      const broadResults = await Promise.all(variants.map(variant => runProviderSearch(variant, null)));
      for (const result of broadResults) {
        for (const model of result.models) {
          const formats = model.formats.length ? model.formats : inferFormatFromNameOrUrl(model);
          if (formats.includes(format)) all.push({ ...model, formats });
        }
        Object.assign(providerErrors, result.errors);
      }
    }

    const ranked = diversifyAndRank(mergeUnique(all), query, format, LIMIT, profile);
    const data = { results: ranked, errors: providerErrors, profile };
    cacheSet(cacheKey, data);
    return data;
  })().finally(() => inflight.delete(cacheKey));

  inflight.set(cacheKey, promise); return promise;
}

app.set('trust proxy', 1);
app.disable('x-powered-by');
app.get('/health', (_req, res) => res.json({ ok: true, service: '3d-model-finder-search', threeDFetchLoaded: Boolean(fetch3d), mvpProviders: MVP_SEARCH_PROVIDERS, cacheEntries: cache.size }));
app.get('/providers', (_req, res) => res.json({ providers: MVP_SEARCH_PROVIDERS }));
app.get('/search', async (req, res) => {
  const limitResult = consumeRateLimit(clientKey(req));
  if (!limitResult.allowed) { res.set('Retry-After', String(limitResult.retryAfterSeconds)); return res.status(429).json({ error: 'rate limit exceeded', retryAfterSeconds: limitResult.retryAfterSeconds }); }
  const parsed = normalizeQuery(req.query.q || '');
  const format = normFormat(req.query.format || parsed.format) || null;
  if (!parsed.query) return res.status(400).json({ error: 'q is required' });
  if (format && !FORMATS.has(format)) return res.status(400).json({ error: 'unsupported format' });
  const profile = inferTaskProfile(parsed.query, req.query.task || null, req.query.category || null);
  if (req.query.software && !profile.software) profile.software = String(req.query.software).toLowerCase();
  try {
    const { results, errors } = await searchModels(parsed.query, format, profile);
    const response = { query: parsed.query, format, profile, count: results.length, results };
    if (process.env.SEARCH_DEBUG === 'true' && Object.keys(errors).length) response.providerErrors = errors;
    res.json(response);
  } catch (error) {
    console.error('search failed:', error.message); res.status(502).json({ error: 'search failed' });
  }
});

app.listen(port, '0.0.0.0', () => console.log(`3D search service listening on port ${port}`));
