export const PROVIDER_POLICY = {
  polyhaven: { tier: 'green', search: true, metadata: true, thumbnail: true, cache: true, sourceLink: true, download: false },
  ambientcg: { tier: 'green', search: true, metadata: true, thumbnail: true, cache: true, sourceLink: true, download: false },
  nasa: { tier: 'yellow', search: true, metadata: true, thumbnail: true, cache: true, sourceLink: true, download: false },
  smithsonian: { tier: 'yellow', search: true, metadata: true, thumbnail: true, cache: true, sourceLink: true, download: false },
  sketchfab: { tier: 'yellow', search: true, metadata: true, thumbnail: true, cache: true, sourceLink: true, download: false, authForDownload: true },
};

export const MVP_SEARCH_PROVIDERS = Object.entries(PROVIDER_POLICY)
  .filter(([, policy]) => policy.tier === 'green' && policy.search)
  .map(([name]) => name);

export function providerAllowed(name, operation = 'search') {
  const policy = PROVIDER_POLICY[String(name || '').toLowerCase()];
  return Boolean(policy && policy[operation] === true);
}
