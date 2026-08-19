import express from 'express';
import { Fetch3D } from '@pikal6/3dfetch';

const app = express();
const port = Number(process.env.PORT || 8787);
const LIMIT = Number(process.env.SEARCH_LIMIT || 12);
const TIMEOUT_MS = Number(process.env.HTTP_TIMEOUT_MS || 15000);

const FORMATS = new Set([
  'gltf','glb','obj','fbx','blend','usd','usdz','stl','3mf','dae','ply','step','iges','off',
  'max','c4d','ma','mb','abc','3ds','maya','fb'
]);
const ALIAS = { stp:'step', igs:'iges', blender:'blend' };

function normFormat(value) {
  const key = String(value || '').trim().toLowerCase().replace(/^\./, '');
  return ALIAS[key] || key;
}

function parseQuery(raw = '') {
  const parts = raw.trim().split(/\s+/).filter(Boolean);
  let format = null;
  const kept = [];
  for (const part of parts) {
    const key = normFormat(part);
    if (!format && FORMATS.has(key)) format = key;
    else kept.push(part);
  }
  return { query: kept.join(' ').trim(), format };
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

function normalize(item, source) {
  if (!item) return null;
  const formats = Array.isArray(item.formats)
    ? [...new Set(item.formats.map(normFormat).filter(Boolean))]
    : [];
  return {
    name: item.name || item.title || 'Без названия',
    source,
    sourceUrl: item.sourceUrl || item.viewerUrl || item.url || '',
    thumbnail: item.thumbnail || item.thumbnailUrl || null,
    formats,
    license: item.license || null,
    access: item.access,
  };
}

function relevanceScore(item, query) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  const name = String(item.name || '').toLowerCase();
  const source = String(item.source || '').toLowerCase();
  let score = 0;
  for (const term of terms) {
    if (name === term) score += 100;
    else if (name.includes(term)) score += 40;
    else score += 0;
  }
  if (terms.length && terms.every(term => name.includes(term))) score += 80;
  if (source === '3dfetch-fallback') score -= 15;
  return score;
}

async function searchPrintables(query, format) {
  if (format && format !== 'stl') return [];
  const body = {
    query: `query SearchPrints($query: String!, $limit: Int, $ordering: SearchChoicesEnum) {
      searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) {
        items { id name slug license { id name } stls { id name fileSize filePreviewPath } }
      }
    }`,
    variables: { query, limit: LIMIT, ordering: 'best_match' },
  };
  const data = await fetchJson('https://api.printables.com/graphql/', {
    method: 'POST', headers: { 'content-type': 'application/json', 'user-agent': '3DModelFinderBot/1.0' }, body: JSON.stringify(body),
  });
  if (data.errors?.length) throw new Error(data.errors.map(e => e.message).join('; '));
  return (data?.data?.searchPrints2?.items || [])
    .filter(x => Array.isArray(x.stls) && x.stls.length)
    .map(x => normalize({ name:x.name, sourceUrl:`https://www.printables.com/model/${x.id}-${x.slug}`, formats:['stl'], license:x.license?.name || null }, 'printables'));
}

async function searchSketchfab(query, format) {
  const params = new URLSearchParams({ q: query, type: 'models', limit: String(Math.max(LIMIT, 24)), sort_by: '-relevance' });
  const headers = process.env.SKETCHFAB_API_TOKEN ? { Authorization: `Token ${process.env.SKETCHFAB_API_TOKEN}` } : {};
  const data = await fetchJson(`https://api.sketchfab.com/v3/search?${params}`, { headers });
  const raw = data.results || [];
  return raw.map(item => normalize({
    name:item.name, sourceUrl:item.viewerUrl, thumbnailUrl:item.thumbnails?.images?.[0]?.url,
    formats:Array.isArray(item.formats) ? item.formats : [], license:item.license?.label || null,
  }, 'sketchfab')).filter(item => {
    if (!format) return true;
    return item.formats.includes(format);
  });
}

let polyhavenCache = null;
let polyhavenCacheAt = 0;
async function searchPolyHaven(query, format) {
  const supported = ['blend','fbx','gltf','obj','usd'];
  if (format && !supported.includes(format)) return [];
  if (!polyhavenCache || Date.now() - polyhavenCacheAt > 1800000) {
    polyhavenCache = await fetchJson('https://api.polyhaven.com/assets?type=models', { headers:{'User-Agent':'3DModelFinderBot/1.0'} });
    polyhavenCacheAt = Date.now();
  }
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  return Object.entries(polyhavenCache)
    .filter(([id,item]) => {
      const haystack = [id, item.name, item.description, ...(item.tags || []), item.category].filter(Boolean).join(' ').toLowerCase();
      return terms.every(term => haystack.includes(term));
    })
    .slice(0, Math.max(LIMIT, 24))
    .map(([id,item]) => normalize({ name:item.name || id, sourceUrl:`https://polyhaven.com/a/${id}`, thumbnailUrl:item.thumbnail_url || null, formats:supported, license:'CC0' }, 'polyhaven'))
    .filter(item => !format || item.formats.includes(format));
}

let nasaTree = null;
let nasaTreeAt = 0;
async function searchNasa(query, format) {
  const supported = ['3ds','blend','fb','glb','max','maya','stl'];
  if (format && !supported.includes(format)) return [];
  if (!nasaTree || Date.now() - nasaTreeAt > 1800000) {
    const data = await fetchJson('https://api.github.com/repos/nasa/NASA-3D-Resources/git/trees/master?recursive=1', { headers:{Accept:'application/vnd.github+json','User-Agent':'3DModelFinderBot/1.0'} });
    nasaTree = Array.isArray(data.tree) ? data.tree : [];
    nasaTreeAt = Date.now();
  }
  const q = query.toLowerCase();
  const groups = new Map();
  for (const entry of nasaTree) {
    if (entry.type !== 'blob') continue;
    const m = entry.path.match(/^3D Models\/([^/]+)\/(.+)$/i);
    if (!m) continue;
    const rel = m[2];
    const ext = rel.includes('.') ? rel.split('.').pop().toLowerCase() : '';
    const fmt = ({ blender:'blend', ['3ds-max']:'max' })[ext] || ext;
    if (!supported.includes(fmt)) continue;
    if (!q.split(/\s+/).every(term => rel.toLowerCase().includes(term) || m[1].toLowerCase().includes(term))) continue;
    if (!groups.has(m[1])) groups.set(m[1], new Set());
    groups.get(m[1]).add(fmt);
  }
  return [...groups.entries()].slice(0,LIMIT).map(([name,formats]) => normalize({ name, sourceUrl:`https://github.com/nasa/NASA-3D-Resources/tree/master/3D%20Models/${encodeURIComponent(name)}`, formats:[...formats], license:'NASA Media Usage Guidelines' }, 'nasa')).filter(item => !format || item.formats.includes(format));
}

async function searchSmithsonian(query, format) {
  const apiKey = process.env.SMITHSONIAN_API_KEY;
  if (!apiKey) return [];
  const params = new URLSearchParams({ q:query, rows:String(LIMIT), api_key:apiKey });
  if (format) params.set('model_type', format);
  const data = await fetchJson(`https://3d-api.si.edu/api/3d/v1/search?${params}`, { headers:{'User-Agent':'3DModelFinderBot/1.0'} });
  return (data?.rows || data?.results || []).map(item => normalize({ name:item.name || item.title, sourceUrl:item.url || item.sourceUrl || item._links?.self?.href || '', thumbnailUrl:item.thumbnail, formats:item.model_type ? [normFormat(item.model_type)] : (format ? [format] : []), license:item.license || 'Smithsonian Open Access' }, 'smithsonian')).filter(item => !format || item.formats.includes(format));
}

let threeDDDLatest = null;
let threeDDDAt = 0;
async function get3DDDLatest() {
  if (threeDDDLatest && Date.now() - threeDDDAt < 1800000) return threeDDDLatest;
  const data = await fetchJson('https://models.3ddd.ru/api/models/last', { method:'POST', headers:{'content-type':'application/json',accept:'application/json','user-agent':'3DModelFinderBot/1.0 (+link-only)'}, body:'{}' });
  if (!data?.success || !Array.isArray(data.data)) throw new Error('Unexpected 3DDD response');
  threeDDDLatest = data.data.map(item => normalize({ name:item.title || item.titleEn || item.slug, sourceUrl:`https://3ddd.ru/3dmodels/show/${item.slug}`, thumbnailUrl:item.image ? `https://3ddd.ru/${String(item.image).replace(/^\//,'')}` : null, formats:[], license:item.typeText === 'pro' ? 'PRO on 3DDD' : item.typeText === 'free' ? 'FREE on 3DDD' : null, access:'link-only' }, '3ddd'));
  threeDDDAt = Date.now();
  return threeDDDLatest;
}

let fetch3d = null;
try { fetch3d = new Fetch3D({ timeout: TIMEOUT_MS }); } catch (error) { console.error('3dfetch unavailable:', error.message); }

async function searchFallback(query, format) {
  if (!fetch3d) return [];
  try {
    const result = await fetch3d.searchAll(format ? { query, formats:[format], limit:Math.max(LIMIT,24) } : { query, limit:Math.max(LIMIT,24) });
    return (result?.models || []).map(item => normalize(item, item.source || '3dfetch-fallback')).filter(item => !format || item.formats.includes(format));
  } catch (error) { console.error('3dfetch fallback:', error.message); return []; }
}

async function searchModels(query, format) {
  const tasks = [searchPrintables(query,format), searchSketchfab(query,format), searchPolyHaven(query,format), searchNasa(query,format), process.env.SMITHSONIAN_API_KEY ? searchSmithsonian(query,format) : Promise.resolve([])];
  const settled = await Promise.allSettled(tasks);
  const direct = settled.flatMap(x => x.status === 'fulfilled' ? x.value : []);
  const fallback = await searchFallback(query,format);
  const all = [...direct,...fallback];
  const seen = new Set();
  const unique = all.filter(item => {
    const key = `${item.source}:${item.sourceUrl || item.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return unique.sort((a,b) => relevanceScore(b,query) - relevanceScore(a,query)).slice(0,LIMIT * 3);
}

app.get('/health', (_req,res) => res.json({ ok:true, service:'3d-model-finder-search', directProviders:['printables','sketchfab','polyhaven','nasa','3ddd',...(process.env.SMITHSONIAN_API_KEY ? ['smithsonian']:[])], fallback:'3dfetch', threeDFetchLoaded:Boolean(fetch3d) }));
app.get('/providers', (_req,res) => res.json({ providers:[
  {name:'printables',mode:'direct',formats:['stl'],auth:'none',formatFilter:'metadata'},
  {name:'sketchfab',mode:'direct',formats:['glb','gltf','obj','fbx','blend','usd','usdz','stl','3mf','dae','ply','abc','max','c4d'],auth:'token-optional',formatFilter:'metadata'},
  {name:'polyhaven',mode:'direct',formats:['blend','fbx','gltf','obj','usd'],auth:'none',formatFilter:'metadata'},
  {name:'nasa',mode:'direct',formats:['3ds','blend','fb','glb','max','maya','stl'],auth:'none',formatFilter:'path-extension'},
  {name:'smithsonian',mode:'direct',formats:['stl','glb','gltf','obj','ply','blend','f3z'],auth:'api-key-optional',formatFilter:'native'},
  {name:'3ddd',mode:'direct-latest',formats:['unknown-from-last-endpoint'],auth:'none',formatFilter:'not-yet-available',access:'link-only'},
  {name:'3dfetch-fallback',mode:'fallback',formats:'provider-dependent',auth:'provider-dependent',formatFilter:'normalized'}
],configured:{sketchfab:Boolean(process.env.SKETCHFAB_API_TOKEN),smithsonian:Boolean(process.env.SMITHSONIAN_API_KEY),threeddd:true}}));
app.get('/latest/3ddd', async (_req,res) => { try { const results=await get3DDDLatest(); res.json({source:'3ddd',count:results.length,results}); } catch(error) { res.status(502).json({error:'3ddd latest failed',details:String(error?.message||error)}); } });
app.get('/search', async (req,res) => {
  const parsed=parseQuery(String(req.query.q || ''));
  const requestedFormat=normFormat(req.query.format || parsed.format);
  if (!parsed.query) return res.status(400).json({error:'q is required'});
  try { const results=await searchModels(parsed.query,requestedFormat || null); res.json({query:parsed.query,format:requestedFormat || null,count:results.length,results}); }
  catch(error) { res.status(502).json({error:'search failed',details:String(error?.message || error)}); }
});
app.listen(port,'0.0.0.0',() => console.log(`3D search service listening on port ${port}`));
