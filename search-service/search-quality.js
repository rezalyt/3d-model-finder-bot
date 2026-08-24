const SOURCE_PRIORITY = {
  polyhaven: 130,
  sketchfab: 122,
  printables: 116,
  thangs: 112,
  ambientcg: 108,
  nasa: 104,
};

const MODERN_FORMATS = new Set(['gltf', 'glb', 'fbx']);
const OUTDATED_ONLY_FORMATS = new Set(['3ds', 'max', 'c4d', 'ma', 'mb']);
const COMMON_NOISE = new Set([
  '3d', 'model', 'models', 'free', 'download', 'object', 'asset',
  'low', 'poly', 'high', 'quality', 'realistic', 'render', 'game', 'ready',
]);

export function normalizeText(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function finiteNumber(...values) {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return 0;
}

export function normalizeModel(model) {
  if (!model) return null;
  const metadata = model.metadata && typeof model.metadata === 'object' ? model.metadata : {};
  const tags = Array.isArray(model.tags) ? model.tags : [];
  const categories = Array.isArray(model.categories) ? model.categories : [];
  const formats = Array.isArray(model.formats) ? model.formats.map(value => String(value).toLowerCase()) : [];
  const name = String(model.name || '').trim();
  const description = String(model.description || '').trim();
  const source = String(model.source || 'unknown').toLowerCase();
  const sourceUrl = String(model.sourceUrl || '').trim();
  const thumbnail = String(model.thumbnailUrl || model.thumbnail || '').trim() || null;
  const license = String(model.license || '').trim() || null;
  const rating = finiteNumber(model.ratingAvg, model.rating, metadata.ratingAvg, metadata.rating);
  const likes = finiteNumber(model.likesCount, model.likeCount, model.likes, metadata.likesCount, metadata.likes);
  const downloads = finiteNumber(model.downloadCount, model.downloads, model.makesCount, metadata.downloadCount, metadata.downloads, metadata.makesCount);
  const views = finiteNumber(model.viewCount, model.views, metadata.viewCount, metadata.views);
  const polygonCount = finiteNumber(
    model.polygonCount, model.polygons, model.triangles,
    metadata.polygonCount, metadata.polygons, metadata.triangles,
    metadata.mesh?.polygonCount, metadata.mesh?.triangles,
  );
  const textureCount = finiteNumber(
    model.textureCount, model.texturesCount,
    metadata.textureCount, metadata.texturesCount,
  );
  const hasTextures = Boolean(
    model.hasTextures ?? model.textures ?? metadata.hasTextures ?? metadata.textures ?? textureCount > 0,
  );

  return {
    ...model,
    name,
    description,
    author: model.author || { id: null, name: '', url: null },
    source,
    sourceUrl,
    downloadUrl: model.downloadUrl || null,
    thumbnailUrl: thumbnail,
    license,
    formats,
    tags: tags.map(String),
    categories: categories.map(String),
    rating: Number.isFinite(rating) ? rating : 0,
    likes: Number.isFinite(likes) ? likes : 0,
    downloads: Number.isFinite(downloads) ? downloads : 0,
    views: Number.isFinite(views) ? views : 0,
    polygonCount,
    textureCount,
    hasTextures,
    metadata,
  };
}

function log10(value) {
  return value > 0 ? Math.log10(value + 1) : 0;
}

export function scoreModel(model, query, requestedFormat = null) {
  const item = normalizeModel(model);
  if (!item || !item.name || !item.sourceUrl) return -Infinity;

  const q = normalizeText(query);
  const title = normalizeText(item.name);
  const body = normalizeText([item.name, item.description, ...item.tags, ...item.categories].join(' '));
  const qTerms = q.split(' ').filter(Boolean);
  const sourcePriority = SOURCE_PRIORITY[item.source] || 45;
  let score = sourcePriority;

  if (title === q) score += 52;
  else if (title.startsWith(q)) score += 38;
  else if (title.includes(q)) score += 28;

  let matched = 0;
  for (const term of qTerms) {
    if (COMMON_NOISE.has(term)) continue;
    if (title === term) score += 30, matched += 1;
    else if (title.startsWith(term)) score += 20, matched += 1;
    else if (title.includes(term)) score += 14, matched += 1;
    else if (body.includes(` ${term} `) || body.startsWith(`${term} `) || body.endsWith(` ${term}`)) score += 8, matched += 1;
  }
  if (qTerms.length) score += (matched / qTerms.length) * 40;

  if (requestedFormat) {
    if (!item.formats.includes(requestedFormat)) return -Infinity;
    score += 28;
  } else if (item.formats.length) {
    score += Math.min(item.formats.length, 5) * 2;
  }

  const modernCount = item.formats.filter(format => MODERN_FORMATS.has(format)).length;
  score += modernCount * 5;
  if (item.formats.length && item.formats.every(format => OUTDATED_ONLY_FORMATS.has(format))) score -= 10;

  if (item.thumbnailUrl) score += 8;
  if (item.downloadUrl) score += 7;
  if (item.license) score += 4;
  if (item.description.length >= 80) score += 3;

  if (item.hasTextures) score += 10;
  else if (item.textureCount === 0) score -= 5;

  if (item.polygonCount > 0) {
    if (item.polygonCount >= 10_000 && item.polygonCount <= 100_000) score += 10;
    else if (item.polygonCount >= 1_000 && item.polygonCount < 10_000) score += 3;
    else if (item.polygonCount > 100_000 && item.polygonCount <= 500_000) score += 5;
    else if (item.polygonCount < 1_000) score -= 8;
    else if (item.polygonCount > 1_000_000) score -= 5;
  }

  score += Math.min(log10(item.downloads) * 4, 16);
  score += Math.min(log10(item.likes) * 2.5, 10);
  score += Math.min(log10(item.views) * 1.5, 7);
  if (item.rating >= 4.5) score += 10;
  else if (item.rating >= 4.0) score += 6;
  else if (item.rating > 0 && item.rating < 3.0) score -= 10;
  if (item.metadata?.staffpickedAt || item.metadata?.featured || item.metadata?.isStaffPicked) score += 8;
  if (item.metadata?.isDownloadable === false) score -= 6;

  return score;
}

function duplicateKey(model) {
  const normalizedName = normalizeText(model.name).replace(/\b(3d|model|free|download)\b/g, '').trim();
  return `${model.source}:${model.sourceUrl}|${normalizedName}`;
}

export function diversifyAndRank(models, query, requestedFormat = null, limit = 12) {
  const candidates = [];
  const seenUrls = new Set();
  for (const raw of models) {
    const item = normalizeModel(raw);
    if (!item) continue;
    if (requestedFormat && !item.formats.includes(requestedFormat)) continue;
    const urlKey = item.sourceUrl.toLowerCase();
    if (!urlKey || seenUrls.has(urlKey)) continue;
    seenUrls.add(urlKey);
    const qualityScore = scoreModel(item, query, requestedFormat);
    if (!Number.isFinite(qualityScore) || qualityScore < 65) continue;
    candidates.push({ ...item, qualityScore });
  }

  candidates.sort((a, b) => b.qualityScore - a.qualityScore || a.name.localeCompare(b.name));
  const selected = [];
  const perSource = new Map();
  const perName = new Map();
  const keys = new Set();

  const tryAdd = (item, sourceCap) => {
    const key = duplicateKey(item);
    if (keys.has(key)) return false;
    const sourceCount = perSource.get(item.source) || 0;
    if (sourceCount >= sourceCap) return false;
    const nameKey = normalizeText(item.name).slice(0, 80);
    const nameCount = perName.get(nameKey) || 0;
    if (nameCount >= 2) return false;
    keys.add(key);
    perSource.set(item.source, sourceCount + 1);
    perName.set(nameKey, nameCount + 1);
    selected.push(item);
    return true;
  };

  for (const item of candidates) {
    if (selected.length >= limit) break;
    tryAdd(item, 3);
  }
  if (selected.length < limit) {
    for (const item of candidates) {
      if (selected.length >= limit) break;
      tryAdd(item, 6);
    }
  }
  return selected.slice(0, limit);
}

export function inferFormatFromNameOrUrl(model) {
  const haystack = `${model?.name || ''} ${model?.sourceUrl || ''}`.toLowerCase();
  const known = ['gltf', 'glb', 'obj', 'fbx', 'blend', 'usd', 'usdz', 'stl', '3mf', 'dae', 'ply', 'step', 'iges', 'off', 'max', 'c4d', 'abc', '3ds', 'maya', 'fb'];
  return known.filter(format => haystack.includes(`.${format}`));
}
