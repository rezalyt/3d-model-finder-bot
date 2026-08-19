import express from "express";

const app = express();
const PORT = Number(process.env.PORT || 8787);

let threeDFetch;
try {
  threeDFetch = await import("@pikal6/3dfetch");
} catch (err) {
  console.error("Failed to load @pikal6/3dfetch:", err);
}

function normalizeFormat(value) {
  return String(value || "").trim().toLowerCase().replace(/^\./, "");
}

function normalizeResult(item) {
  const formats = Array.isArray(item?.formats)
    ? item.formats.map(normalizeFormat).filter(Boolean)
    : [];

  return {
    name: item?.name || "Без названия",
    source: item?.source || "unknown",
    sourceUrl: item?.sourceUrl || item?.viewerUrl || item?.url || "",
    thumbnailUrl: item?.thumbnailUrl || item?.previewUrl || null,
    license: item?.license || null,
    formats,
    downloadUrl: item?.downloadUrl || null,
  };
}

async function searchModels(query, format) {
  if (!threeDFetch) {
    throw new Error("3dfetch is not installed or could not be loaded");
  }

  const searchAll = threeDFetch.searchAll || threeDFetch.default?.searchAll;
  if (typeof searchAll !== "function") {
    throw new Error("searchAll() was not found in the installed 3dfetch package");
  }

  const options = { query };
  if (format) options.formats = [format];

  const response = await searchAll(options);
  const rawResults = Array.isArray(response) ? response : (response?.results || []);
  let results = rawResults.map(normalizeResult);

  // Final local guard: only return models that explicitly advertise the requested format.
  if (format) {
    results = results.filter((item) => item.formats.includes(format));
  }

  return results;
}

app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    service: "3d-model-finder-search",
    threeDFetchLoaded: Boolean(threeDFetch),
  });
});

app.get("/search", async (req, res) => {
  const query = String(req.query.q || "").trim();
  const format = normalizeFormat(req.query.format);

  if (!query) {
    return res.status(400).json({ error: "q is required" });
  }

  try {
    const results = await searchModels(query, format);
    res.json({ query, format: format || null, count: results.length, results });
  } catch (err) {
    console.error("Search failed:", err);
    res.status(502).json({ error: "3dfetch search failed", details: String(err.message || err) });
  }
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`3D search service listening on port ${PORT}`);
});
