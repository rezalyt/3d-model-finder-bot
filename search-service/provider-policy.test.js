import test from 'node:test';
import assert from 'node:assert/strict';
import { MVP_SEARCH_PROVIDERS, providerAllowed } from './provider-policy.js';

test('MVP provider policy is explicit and green-only', () => {
  assert.deepEqual(MVP_SEARCH_PROVIDERS, ['polyhaven', 'ambientcg']);
  assert.equal(providerAllowed('polyhaven', 'search'), true);
  assert.equal(providerAllowed('ambientcg', 'search'), true);
  assert.equal(providerAllowed('sketchfab', 'search'), false);
  assert.equal(providerAllowed('polyhaven', 'download'), false);
});
