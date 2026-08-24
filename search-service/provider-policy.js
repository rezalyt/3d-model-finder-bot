export const PROVIDER_POLICY = {
  polyhaven: {
    tier: 'green',
    search: true,
    metadata: true,
    thumbnail: true,
    cache: true,
    sourceLink: true,
    download: false,
    redistribution: false,
  },
  ambientcg: {
    tier: 'green',
    search: true,
    metadata: true,
    thumbnail: true,
    cache: true,
    sourceLink: true,
    download: false,
    redistribution: false,
  },
  nasa: {
    tier: 'yellow',
    search: true,
    metadata: true,
    thumbnail: true,
    cache: true,
    sourceLink: true,
    download: false,
    redistribution: false,
  },
  smithsonian: {
    tier: 'yellow',
    search: true,
    metadata: true,
    thumbnail: true,
    cache: true,
    sourceLink: true,
    download: false,
    redistribution: false,
  },
  sketchfab: {
    tier: 'yellow',
    search: true,
    metadata: true,
    thumbnail: true,
    cache: true,
    sourceLink: true,
    download: false,
    redistribution: false,
    authForDownload: true,
  },
};

// Only providers explicitly enabled here are allowed into the MVP search path.
// Yellow providers remain opt-in so provider-specific terms can be reviewed first.
export const MVP_SEARCH_PROVIDERS = Object.entries(PROVIDER_POLICY)
  .filter(([, policy]) => policy.tier === 'green')
  .map(([name]) => name);

export function providerAllowed(name, operation = 'search') {
  const policy = PROVIDER_POLICY[String(name || '').toLowerCase()];
  return Boolean(policy && policy[operation] === true);
}
