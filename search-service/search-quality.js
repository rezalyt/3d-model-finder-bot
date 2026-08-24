const LANDSCAPE_TERMS = {
  vegetation: ['tree', 'trees', 'birch', 'pine', 'fir', 'shrub', 'plant', 'flower', 'grass', 'hedge', 'берёза', 'береза', 'сосна', 'ель', 'куст', 'растение', 'цветок', 'трава'],
  hardscape: ['rock', 'rocks', 'boulder', 'gravel', 'stone', 'path', 'paving', 'камень', 'валун', 'гравий', 'дорожка', 'плитка'],
  furniture: ['bench', 'table', 'chair', 'lounger', 'furniture', 'скамейка', 'стол', 'стул', 'мебель', 'шезлонг'],
  lighting: ['lamp', 'light', 'lighting', 'фонарь', 'светильник', 'освещение'],
  structures: ['gazebo', 'pergola', 'fence', 'gate', 'беседка', 'пергола', 'забор', 'ворота'],
  site: ['fountain', 'pool', 'pond', 'planter', 'кашпо', 'вазон', 'фонтан', 'бассейн', 'пруд'],
  props: ['person', 'people', 'human', 'car', 'vehicle', 'bike', 'человек', 'автомобиль', 'машина', 'велосипед'],
};

const SOFTWARE_FORMATS = {
  sketchup: new Set(['skp']),
  blender: new Set(['blend', 'gltf', 'glb', 'fbx', 'obj', 'usd']),
  '3dsmax': new Set(['max', 'fbx', 'obj', 'gltf', 'glb']),
  'd5-render': new Set(['fbx', 'gltf', 'glb', 'obj']),
  lumion: new Set(['fbx', 'obj', 'dae']),
  enscape: new Set(['fbx', 'obj', 'gltf', 'glb']),
  twinmotion: new Set(['fbx', 'gltf', 'glb', 'obj']),
};

const GREEN_SOURCES = new Set(['polyhaven', 'ambientcg']);
const COMMON_NOISE = new Set(['3d', 'model', 'models', 'free', 'download', 'object', 'asset', 'mesh', 'low', 'poly', 'high', 'quality', 'realistic', 'render', 'game', 'ready']);

export function normalizeText(value) {
  return String(value || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').replace(/\s+/g, ' ').trim();
}

export function normalizeModel(model) {
  if (!model) return null;
  const metadata = model.metadata && typeof model.metadata === 'object' ? model.metadata : {};
  const formats = Array.isArray(model.formats) ? [...new Set(model.formats.map(x => String(x).toLowerCase()))] : [];
  const tags = Array.isArray(model.tags) ? model.tags.map(String) : [];
  const categories = Array.isArray(model.categories) ? model.categories.map(String) : [];
  const name = String(model.name || '').trim();
  const description = String(model.description || '').trim();
  const source = String(model.source || 'unknown').toLowerCase();
  const sourceUrl = String(model.sourceUrl || '').trim();
  const thumbnailUrl = String(model.thumbnailUrl || model.thumbnail || '').trim() || null;
  const rating = Number(model.ratingAvg ?? model.rating ?? metadata.ratingAvg ?? metadata.rating ?? 0);
  const likes = Number(model.likesCount ?? model.likeCount ?? model.likes ?? metadata.likesCount ?? metadata.likes ?? 0);
  const downloads = Number(model.downloadCount ?? model.downloads ?? metadata.downloadCount ?? metadata.downloads ?? 0);
  const views = Number(model.viewCount ?? model.views ?? metadata.viewCount ?? metadata.views ?? 0);
  return {
    ...model,
    name,
    description,
    source,
    sourceUrl,
    thumbnailUrl,
    formats,
    tags,
    categories,
    license: String(model.license || '').trim() || null,
    rating: Number.isFinite(rating) ? rating : 0,
    likes: Number.isFinite(likes) ? likes : 0,
    downloads: Number.isFinite(downloads) ? downloads : 0,
    views: Number.isFinite(views) ? views : 0,
    metadata,
  };
}

function log10(value) {
  return value > 0 ? Math.log10(value + 1) : 0;
}

function taskTerms(profile = {}) {
  return [profile.task || '', profile.category || '', profile.software || '']
    .filter(Boolean).map(normalizeText);
}

function categoryTerms(category) {
  return LANDSCAPE_TERMS[category] || [];
}

function hasAny(haystack, terms) {
  return terms.some(term => haystack.includes(normalizeText(term)));
}

function technicalSignals(item) {
  const metadata = item.metadata || {};
  const textureFlags = [metadata.pbr, metadata.hasPbr, metadata.textures, metadata.hasTextures, metadata.materials].filter(Boolean).length;
  const hasDimensions = Array.isArray(metadata.dimensions)
    ? metadata.dimensions.length === 3
    : Boolean(metadata.dimensions && typeof metadata.dimensions === 'object');
  const hasPolycount = Number.isFinite(Number(metadata.polycount ?? metadata.triangles ?? metadata.polygons));
  const hasLods = Boolean(metadata.lods || metadata.lodCount || metadata.hasLod);
  return { textureFlags, hasDimensions, hasPolycount, hasLods };
}

export function scoreComponents(model, query, requestedFormat = null, profile = {}) {
  const item = normalizeModel(model);
  if (!item || !item.name || !item.sourceUrl) return null;
  const q = normalizeText(query);
  const title = normalizeText(item.name);
  const body = normalizeText([item.name, item.description, ...item.tags, ...item.categories].join(' '));
  const qTerms = q.split(' ').filter(term => term && !COMMON_NOISE.has(term));

  let relevance = 0;
  if (title === q) relevance += 50;
  else if (title.startsWith(q)) relevance += 36;
  else if (title.includes(q)) relevance += 26;
  let matched = 0;
  for (const term of qTerms) {
    if (title.includes(term)) { relevance += 16; matched += 1; }
    else if (body.includes(term)) { relevance += 8; matched += 1; }
  }
  if (qTerms.length) relevance += (matched / qTerms.length) * 30;

  let taskFit = 0;
  if ((profile.task || 'landscape') === 'landscape') taskFit += 10;
  if (profile.category && hasAny(body, categoryTerms(profile.category))) taskFit += 24;
  if (profile.software) {
    const compatibleFormats = SOFTWARE_FORMATS[profile.software];
    if (compatibleFormats && item.formats.some(format => compatibleFormats.has(format))) taskFit += 12;
    if (body.includes(normalizeText(profile.software))) taskFit += 5;
  }
  for (const term of taskTerms(profile)) {
    if (term && body.includes(term)) taskFit += 3;
  }

  let formatFit = 0;
  if (requestedFormat) {
    if (!item.formats.includes(requestedFormat)) return null;
    formatFit += 25;
  } else if (profile.software && SOFTWARE_FORMATS[profile.software]) {
    if (item.formats.some(format => SOFTWARE_FORMATS[profile.software].has(format))) formatFit += 10;
  }

  const technicalSignalsData = technicalSignals(item);
  let technical = 0;
  if (item.thumbnailUrl) technical += 5;
  if (technicalSignalsData.textureFlags > 0) technical += Math.min(8, technicalSignalsData.textureFlags * 2);
  if (technicalSignalsData.hasDimensions) technical += 4;
  if (technicalSignalsData.hasPolycount) technical += 4;
  if (technicalSignalsData.hasLods) technical += 4;
  if (item.formats.length >= 2) technical += 3;
  if (item.description.length >= 80) technical += 2;

  let popularity = Math.min(log10(item.downloads) * 2.2, 8)
    + Math.min(log10(item.likes) * 1.5, 5)
    + Math.min(log10(item.views) * 0.8, 4);
  if (item.rating >= 4.5) popularity += 5;
  else if (item.rating >= 4) popularity += 3;
  else if (item.rating > 0 && item.rating < 3) popularity -= 3;

  const source = GREEN_SOURCES.has(item.source) ? 4 : 0;
  return { relevance, taskFit, formatFit, technical, popularity, source };
}

export function scoreModel(model, query, requestedFormat = null, profile = {}) {
  const components = scoreComponents(model, query, requestedFormat, profile);
  if (!components) return -Infinity;
  return Object.values(components).reduce((sum, value) => sum + value, 0);
}

function duplicateKey(model) {
  return `${model.source}|${model.sourceUrl}`;
}

export function diversifyAndRank(models, query, requestedFormat = null, limit = 20, profile = {}) {
  const candidates = [];
  const seenUrls = new Set();
  for (const raw of models) {
    const item = normalizeModel(raw);
    if (!item || !item.sourceUrl) continue;
    const urlKey = item.sourceUrl.toLowerCase();
    if (seenUrls.has(urlKey)) continue;
    seenUrls.add(urlKey);
    const components = scoreComponents(item, query, requestedFormat, profile);
    if (!components) continue;
    candidates.push({
      ...item,
      qualityScore: Math.round(Object.values(components).reduce((sum, value) => sum + value, 0) * 10) / 10,
      scoreComponents: components,
      licenseStatus: item.license || 'unknown',
    });
  }
  candidates.sort((a, b) => b.qualityScore - a.qualityScore || a.name.localeCompare(b.name));

  const selected = [];
  const perSource = new Map();
  const perName = new Map();
  const seen = new Set();
  const add = (item, sourceCap) => {
    const key = duplicateKey(item);
    if (seen.has(key)) return false;
    const sourceCount = perSource.get(item.source) || 0;
    if (sourceCount >= sourceCap) return false;
    const nameKey = normalizeText(item.name);
    const nameCount = perName.get(nameKey) || 0;
    if (nameCount >= 2) return false;
    seen.add(key);
    perSource.set(item.source, sourceCount + 1);
    perName.set(nameKey, nameCount + 1);
    selected.push(item);
    return true;
  };
  for (const item of candidates) {
    if (selected.length >= limit) break;
    add(item, 4);
  }
  if (selected.length < limit) {
    for (const item of candidates) {
      if (selected.length >= limit) break;
      add(item, 8);
    }
  }
  return selected.slice(0, limit);
}

export function inferFormatFromNameOrUrl(model) {
  const haystack = `${model?.name || ''} ${model?.sourceUrl || ''}`.toLowerCase();
  const known = ['gltf', 'glb', 'obj', 'fbx', 'blend', 'usd', 'usdz', 'stl', '3mf', 'dae', 'ply', 'step', 'iges', 'off', 'max', 'c4d', 'abc', '3ds'];
  return known.filter(fmt => haystack.includes(`.${fmt}`));
}
