import { Fetch3D } from "@pikal6/3dfetch";

const client = new Fetch3D({
  apiKeys: {
    ...(process.env.SKETCHFAB_API_TOKEN ? { sketchfab: process.env.SKETCHFAB_API_TOKEN } : {}),
    ...(process.env.THINGIVERSE_API_TOKEN ? { thingiverse: process.env.THINGIVERSE_API_TOKEN } : {}),
    ...(process.env.MYMINIFACTORY_API_KEY ? { myminifactory: process.env.MYMINIFACTORY_API_KEY } : {}),
  },
  timeout: Number(process.env.THREEDFETСH_TIMEOUT_MS || 15000),
});

const queries = {
  polyhaven: "chair",
  sketchfab: "chair",
  thingiverse: "chair",
  myminifactory: "chair",
  printables: "chair",
  thangs: "chair",
  polypizza: "chair",
  nasa: "mars",
  smithsonian: "chair",
  nih: "bone",
  grabcad: "chair",
  cgtrader: "chair",
  ambientcg: "chair",
  blendswap: "chair",
  cults3d: "chair",
  free3d: "chair",
};

const formatSet = (models) => [...new Set(models.flatMap((m) => Array.isArray(m.formats) ? m.formats : []))].sort();

console.log("3dfetch provider diagnostic");
console.log("Node:", process.version);
console.log("Providers:", client.listProviders().join(", "));
console.log("");

for (const provider of client.listProviders()) {
  const query = queries[provider] || "chair";
  const started = Date.now();
  try {
    const models = await client.search(provider, { query, limit: 5 });
    const duration = Date.now() - started;
    const formats = formatSet(models);
    const stl = await client.search(provider, { query, limit: 20, formats: ["stl"] });

    console.log(JSON.stringify({
      provider,
      query,
      status: "OK",
      durationMs: duration,
      results: models.length,
      formats,
      stlResults: stl.length,
      examples: models.slice(0, 3).map((m) => ({
        name: m.name,
        sourceUrl: m.sourceUrl,
        formats: m.formats,
      })),
    }, null, 2));
  } catch (error) {
    console.log(JSON.stringify({
      provider,
      query,
      status: "ERROR",
      durationMs: Date.now() - started,
      error: String(error?.message || error),
    }, null, 2));
  }
  console.log("---");
}
