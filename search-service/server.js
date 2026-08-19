import express from "express";

const app = express();
const PORT = Number(process.env.PORT || 8787);
const SEARCH_LIMIT = Number(process.env.SEARCH_LIMIT || 20);
const TIMEOUT_MS = Number(process.env.HTTP_TIMEOUT_MS || 15000);
const PRINTABLES_URL = "https://api.printables.com/graphql/";

const FORMAT_ALIASES = {
  glb:"glb", gltf:"gltf", obj:"obj", stl:"stl", fbx:"fbx", blend:"blend",
  usd:"usd", usdz:"usdz", "3mf":"3mf", dae:"dae", ply:"ply", step:"step",
  stp:"step", iges:"iges", igs:"iges"
};

let threeDFetch = null;
try {
  const { Fetch3D } = await import("@pikal6/3dfetch");
  const apiKeys = {};
  if (process.env.SKETCHFAB_API_TOKEN) apiKeys.sketchfab = process.env.SKETCHFAB_API_TOKEN;
  if (process.env.THINGIVERSE_API_TOKEN) apiKeys.thingiverse = process.env.THINGIVERSE_API_TOKEN;
  if (process.env.MYMINIFACTORY_API_KEY) apiKeys.myminifactory = process.env.MYMINIFACTORY_API_KEY;
  threeDFetch = new Fetch3D({ ...(Object.keys(apiKeys).length ? { apiKeys } : {}), timeout: TIMEOUT_MS });
} catch (err) {
  console.error("3dfetch unavailable:", err.message);
}

function normalizeFormat(value) {
  const key = String(value || "").trim().toLowerCase().replace(/^\./, "");
  return FORMAT_ALIASES[key] || key;
}

function normalize(item, sourceOverride) {
  const formats = Array.isArray(item?.formats) ? item.formats.map(normalizeFormat).filter(Boolean) : [];
  return {
    name: item?.name || "Без названия",
    source: sourceOverride || item?.source || "unknown",
    sourceUrl: item?.sourceUrl || item?.viewerUrl || item?.url || "",
    thumbnailUrl: item?.thumbnailUrl || item?.previewUrl || null,
    license: item?.license || null,
    formats,
    downloadUrl: item?.downloadUrl || null,
  };
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

async function searchPrintables(query) {
  const body = {
    query: `query SearchPrints($query: String!, $limit: Int, $ordering: SearchChoicesEnum) {
      searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) {
        items { id name slug license { id name } stls { id name fileSize filePreviewPath } }
        totalCount
      }
    }`,
    variables: { query, limit: SEARCH_LIMIT, ordering: "best_match" }
  };
  const data = await fetchJson(PRINTABLES_URL, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": "3DModelFinderBot/1.0" },
    body: JSON.stringify(body)
  });
  if (data.errors?.length) throw new Error(data.errors.map(e => e.message).join("; "));
  const items = data?.data?.searchPrints2?.items || [];
  return items.filter(x => Array.isArray(x.stls) && x.stls.length).map(x => normalize({
    name: x.name,
    source: "printables",
    sourceUrl: `https://www.printables.com/model/${x.id}-${x.slug}`,
    thumbnailUrl: x.stls[0]?.filePreviewPath ? `https://media.printables.com/media/prints/${x.id}/stls/${x.stls[0].filePreviewPath.split('/').pop()}` : null,
    license: x.license?.name || null,
    formats: ["stl"]
  }, "printables"));
}

async function search3dfetch(query, format = "") {
  if (!threeDFetch) return [];
  const options = { query, limit: SEARCH_LIMIT };
  if (format) options.formats = [format];
  const response = await threeDFetch.searchAll(options);
  const models = Array.isArray(response) ? response : (response?.models || []);
  return models.map(x => normalize(x)).filter(x => !format || x.formats.includes(format));
}

async function searchModels(query, format) {
  const tasks = [];
  if (format === "stl") tasks.push(searchPrintables().catch(err => { console.error("Printables:", err.message); return []; }));
  tasks.push(search3dfetch(query, format).catch(err => { console.error("3dfetch:", err.message); return []; }));
  const groups = await Promise.all(tasks);
  const seen = new Set();
  return groups.flat().filter(item => {
    const key = `${item.source}|${item.sourceUrl}|${item.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

app.get("/health", (_req, res) => res.json({ ok: true, service: "3d-model-finder-search", threeDFetchLoaded: Boolean(threeDFetch), providers: { printables: true, threeDFetch: Boolean(threeDFetch) } }));

app.get("/providers", (_req, res) => res.json({
  providers: [
    { name: "printables", direct: true, formats: ["stl"] },
    { name: "3dfetch", direct: false, formats: "provider-dependent" }
  ]
}));

app.get("/search", async (req, res) => {
  const query = String(req.query.q || "").trim();
  const format = normalizeFormat(req.query.format);
  if (!query) return res.status(400).json({ error: "q is required" });
  try {
    const results = await searchModels(query, format);
    res.json({ query, format: format || null, count: results.length, results });
  } catch (err) {
    res.status(502).json({ error: "search failed", details: String(err?.message || err) });
  }
});

app.listen(PORT, "0.0.0.0", () => console.log(`3D search service listening on port ${PORT}`));
