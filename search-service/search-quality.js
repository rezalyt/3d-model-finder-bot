const SOURCE_PRIORITY = { polyhaven: 12, ambientcg: 10, nasa: 6, smithsonian: 6, sketchfab: 8 };
const COMMON_NOISE = new Set(['3d','model','models','free','download','object','asset','mesh','low','poly','high','quality','realistic','render','game','ready']);
const MODERN_FORMATS = new Set(['glb','gltf','fbx','usd','usdz','blend']);
const LANDSCAPE_FORMATS = new Set(['fbx','glb','gltf','blend','usd','usdz','obj','3ds','max']);
const LANDSCAPE_TERMS = new Set(['landscape','garden','outdoor','plant','plants','tree','trees','shrub','flower','grass','rock','rocks','boulder','furniture','bench','lamp','lighting','fence','pergola','gazebo','path','paving','planter','pond','pool','sculpture']);

export function normalizeText(value) {
  return String(value || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').replace(/\s+/g, ' ').trim();
}

export function normalizeModel(model) {
  if (!model) return null;
  const metadata = model.metadata && typeof model.metadata === 'object' ? model.metadata : {};
  const tags = Array.isArray(model.tags) ? model.tags : [];
  const categories = Array.isArray(model.categories) ? model.categories : [];
  const formats = Array.isArray(model.formats) ? model.formats.map(x => String(x).toLowerCase().replace(/^\./, '')) : [];
  const name = String(model.name || '').trim();
  const description = String(model.description || '').trim();
  const source = String(model.source || 'unknown').toLowerCase();
  const sourceUrl = String(model.sourceUrl || model.viewerUrl || '').trim();
  const thumbnailUrl = String(model.thumbnailUrl || model.thumbnail || '').trim() || null;
  const license = String(model.license || '').trim() || null;
  const rating = Number(model.ratingAvg ?? model.rating ?? metadata.ratingAvg ?? metadata.rating ?? 0);
  const likes = Number(model.likesCount ?? model.likeCount ?? model.likes ?? metadata.likesCount ?? metadata.likes ?? 0);
  const downloads = Number(model.downloadCount ?? model.downloads ?? model.makesCount ?? metadata.downloadCount ?? metadata.downloads ?? metadata.makesCount ?? 0);
  const views = Number(model.viewCount ?? model.views ?? metadata.viewCount ?? metadata.views ?? 0);
  return { ...model, name, description, source, sourceUrl, thumbnailUrl, license, formats, tags: tags.map(String), categories: categories.map(String), rating: Number.isFinite(rating) ? rating : 0, likes: Number.isFinite(likes) ? likes : 0, downloads: Number.isFinite(downloads) ? downloads : 0, views: Number.isFinite(views) ? views : 0, metadata };
}

function log10(value) { return value > 0 ? Math.log10(value + 1) : 0; }
function textBlob(item) { return normalizeText([item.name, item.description, ...item.tags, ...item.categories].join(' ')); }
function hasLandscapeSignal(item) { return [...LANDSCAPE_TERMS].some(term => textBlob(item).includes(term)); }
function hasPbr(item) {
  const metadata = item.metadata || {};
  if (metadata.pbr || metadata.PBR || metadata.isPBR) return true;
  const text = normalizeText([metadata, item.description, ...item.tags].join(' '));
  return ['albedo','normal','roughness','metallic','pbr'].some(term => text.includes(term));
}
function hasModernFormat(item) { return item.formats.some(format => MODERN_FORMATS.has(format)); }
function polygonFit(item, profile) {
  const raw = item.metadata?.polycount ?? item.metadata?.polygonCount ?? item.polycount ?? item.polygonCount;
  const poly = Number(raw);
  if (!Number.isFinite(poly) || poly <= 0) return 0;
  const task = profile?.task || 'landscape';
  const category = profile?.category || '';
  const target = task === 'landscape' && category === 'vegetation' ? 150000 : 100000;
  const distance = Math.abs(Math.log10(poly) - Math.log10(target));
  return Math.max(0, 6 - distance * 2.5);
}

export function scoreComponents(model, query, requestedFormat = null, profile = {}) {
  const item = normalizeModel(model);
  if (!item || !item.name || !item.sourceUrl) return null;
  const q = normalizeText(query);
  const title = normalizeText(item.name);
  const body = textBlob(item);
  const qTerms = q.split(' ').filter(Boolean).filter(term => !COMMON_NOISE.has(term));
  let relevance = 0;
  if (title === q) relevance += 36;
  else if (title.startsWith(q)) relevance += 28;
  else if (title.includes(q)) relevance += 20;
  let matched = 0;
  for (const term of qTerms) {
    if (title === term) { relevance += 18; matched += 1; }
    else if (title.includes(term)) { relevance += 11; matched += 1; }
    else if (body.includes(` ${term} `) || body.startsWith(`${term} `) || body.endsWith(` ${term}`)) { relevance += 6; matched += 1; }
  }
  if (qTerms.length) relevance += (matched / qTerms.length) * 24;

  const taskText = normalizeText(profile?.task || 'landscape');
  const categoryText = normalizeText(profile?.category || '');
  const softwareText = normalizeText(profile?.software || '');
  let taskFit = taskText === 'landscape' ? (hasLandscapeSignal(item) ? 15 : 0) : 0;
  if (categoryText && body.includes(categoryText)) taskFit += 7;
  if (softwareText && item.formats.some(format => format === softwareText || (softwareText === 'sketchup' && format === 'skp'))) taskFit += 7;

  let formatFit = 0;
  if (requestedFormat) {
    if (!item.formats.includes(requestedFormat)) return null;
    formatFit = 18;
  } else {
    formatFit = Math.min(item.formats.length, 4) * 2 + (hasModernFormat(item) ? 3 : 0);
    if ((profile?.task || 'landscape') === 'landscape' && item.formats.some(format => LANDSCAPE_FORMATS.has(format))) formatFit += 3;
  }

  const technical = (hasPbr(item) ? 8 : 0) + (item.thumbnailUrl ? 4 : 0) + (item.metadata?.dimensions || item.metadata?.width || item.metadata?.height ? 3 : 0) + polygonFit(item, profile);
  const popularity = Math.min(log10(item.downloads) * 1.8, 7) + Math.min(log10(item.likes) * 1.3, 4) + Math.min(log10(item.views) * 0.8, 3) + (item.rating >= 4.5 ? 4 : item.rating >= 4 ? 2 : item.rating > 0 && item.rating < 3 ? -4 : 0);
  const source = SOURCE_PRIORITY[item.source] || 2;

  return { relevance, taskFit, formatFit, technical, popularity, source };
}

export function scoreModel(model, query, requestedFormat = null, profile = {}) {
  const c = scoreComponents(model, query, requestedFormat, profile);
  if (!c) return -Infinity;
  return c.relevance + c.taskFit + c.formatFit + c.technical + c.popularity + c.source;
}

function duplicateKey(model) {
  const normalizedName = normalizeText(model.name).replace(/\b(3d|model|free|download)\b/g, '').trim();
  const providerId = model.providerModelId || model.id || '';
  return `${normalizedName}|${providerId}|${model.source}`;
}

function nearDuplicateKey(model) {
  return normalizeText(model.name).replace(/\b(3d|model|free|download)\b/g, '').trim().replace(/\s+/g, ' ');
}

export function diversifyAndRank(models, query, requestedFormat = null, limit = 12, profile = {}) {
  const candidates = [];
  const seenUrls = new Set();
  for (const raw of models) {
    const item = normalizeModel(raw);
    if (!item) continue;
    if (requestedFormat && !item.formats.includes(requestedFormat)) continue;
    const urlKey = item.sourceUrl.toLowerCase();
    if (!urlKey || seenUrls.has(urlKey)) continue;
    seenUrls.add(urlKey);
    const components = scoreComponents(item, query, requestedFormat, profile);
    if (!components) continue;
    const qualityScore = Math.round((components.relevance + components.taskFit + components.formatFit + components.technical + components.popularity + components.source) * 10) / 10;
    if (qualityScore < 35) continue;
    candidates.push({ ...item, qualityScore, scoreBreakdown: components });
  }
  candidates.sort((a, b) => b.qualityScore - a.qualityScore || a.name.localeCompare(b.name));

  const selected = [];
  const perSource = new Map();
  const duplicateNames = new Set();
  for (const item of candidates) {
    if (selected.length >= limit) break;
    const sourceCount = perSource.get(item.source) || 0;
    if (sourceCount >= 5) continue;
    const key = duplicateKey(item);
    const nearKey = nearDuplicateKey(item);
    if (selected.some(existing => duplicateKey(existing) === key)) continue;
    if (duplicateNames.has(nearKey) && selected.length < Math.min(limit, 6)) continue;
    perSource.set(item.source, sourceCount + 1);
    duplicateNames.add(nearKey);
    selected.push(item);
  }
  return selected.slice(0, limit);
}

export function inferFormatFromNameOrUrl(model) {
  const haystack = `${model?.name || ''} ${model?.sourceUrl || ''}`.toLowerCase();
  const known = ['gltf','glb','obj','fbx','blend','usd','usdz','stl','3mf','dae','ply','step','iges','off','max','c4d','abc','3ds','maya'];
  return known.filter(fmt => haystack.includes(`.${fmt}`));
}
