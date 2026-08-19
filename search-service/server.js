import express from 'express';
import { Fetch3D } from '@pikal6/3dfetch';

const app = express();
const port = Number(process.env.PORT || 8787);
const fetch3d = new Fetch3D();

const FORMAT_ALIASES = {
  gltf: 'gltf', 'glb': 'glb', obj: 'obj', fbx: 'fbx', blend: 'blend',
  usd: 'usd', usdz: 'usdz', stl: 'stl', '3mf': '3mf', dae: 'dae', ply: 'ply',
  step: 'step', stp: 'step', iges: 'iges', igs: 'iges', off: 'off',
  max: 'max', c4d: 'c4d', ma: 'ma', mb: 'mb', abc: 'abc',
};

function parseQuery(raw = '') {
  const parts = raw.trim().split(/\s+/).filter(Boolean);
  let format = null;
  const kept = [];
  for (const part of parts) {
    const key = part.toLowerCase().replace(/^\./, '');
    if (!format && FORMAT_ALIASES[key]) format = FORMAT_ALIASES[key];
    else kept.push(part);
  }
  return { query: kept.join(' ').trim(), format };
}

function normalize(item, source) {
  if (!item) return null;
  const formats = Array.isArray(item.formats) ? item.formats.map(String).map(x => x.toLowerCase()) : [];
  return {
    name: item.name || item.title || 'Без названия',
    source,
    sourceUrl: item.sourceUrl || item.viewerUrl || item.url || '',
    thumbnail: item.thumbnail || item.thumbnailUrl || null,
    formats: [...new Set(formats)],
    license: item.license || null,
  };
}

async function searchPrintables(query, format) {
  // Printables is queried directly because the current 3dfetch adapter returns HTTP 400.
  if (format && format !== 'stl') return [];
  const body = {
    query: `query SearchPrints($query: String!, $limit: Int, $ordering: SearchChoicesEnum) { searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) { items { id name slug license { id name } } totalCount } }`,
    variables: { query, limit: 12, ordering: 'best_match' },
  };
  const response = await fetch('https://api.printables.com/graphql/', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'user-agent': '3DModelFinderBot/0.2' },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`Printables HTTP ${response.status}`);
  const json = await response.json();
  const items = json?.data?.searchPrints2?.items || [];
  return items.map(x => ({
    name: x.name,
    source: 'printables',
    sourceUrl: `https://www.printables.com/model/${x.id}-${x.slug}`,
    formats: ['stl'],
    license: x.license?.name || null,
  }));
}

async function searchDirect(query, format) {
  const tasks = [
    ['printables', () => searchPrintables(query, format)],
  ];
  const settled = await Promise.allSettled(tasks.map(([, fn]) => fn()));
  return settled.flatMap((r, i) => r.status === 'fulfilled' ? r.value : []);
}

async function searchFallback(query, format) {
  try {
    const options = format ? { query, formats: [format], limit: 20 } : { query, limit: 20 };
    const result = await fetch3d.searchAll(options);
    return (result?.models || []).map(item => normalize(item, item.source || '3dfetch')).filter(Boolean);
  } catch (error) {
    return [];
  }
}

app.get('/health', (_req, res) => {
  res.json({ ok: true, service: '3d-model-finder-search', directProviders: ['printables'], fallback: '3dfetch' });
});

app.get('/providers', (_req, res) => {
  res.json({
    direct: [
      { name: 'printables', formats: ['stl'], status: 'enabled' },
    ],
    fallback: { name: '3dfetch', status: 'enabled' },
  });
});

app.get('/search', async (req, res) => {
  const parsed = parseQuery(String(req.query.q || ''));
  const requestedFormat = String(req.query.format || parsed.format || '').toLowerCase() || null;
  if (!parsed.query) return res.status(400).json({ error: 'q is required' });

  const [direct, fallback] = await Promise.all([
    searchDirect(parsed.query, requestedFormat),
    searchFallback(parsed.query, requestedFormat),
  ]);

  const seen = new Set();
  const results = [...direct, ...fallback].filter(item => {
    const key = `${item.source}:${item.sourceUrl || item.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    if (requestedFormat && !item.formats.includes(requestedFormat)) return false;
    return true;
  });

  res.json({ query: parsed.query, format: requestedFormat, count: results.length, results });
});

app.listen(port, () => console.log(`3D search service listening on port ${port}`));
