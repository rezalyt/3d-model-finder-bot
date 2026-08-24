import test from 'node:test';
import assert from 'node:assert/strict';
import { diversifyAndRank, scoreModel } from './search-quality.js';

test('exact title with textures and reasonable polygon count scores strongly', () => {
  const exact = scoreModel({
    name: 'Cat',
    source: 'sketchfab',
    sourceUrl: 'https://example.test/cat',
    formats: ['glb', 'fbx'],
    tags: ['cat'],
    description: 'A downloadable textured cat model',
    thumbnailUrl: 'https://example.test/cat.jpg',
    downloadUrl: 'https://example.test/cat.glb',
    license: 'CC BY',
    hasTextures: true,
    polygonCount: 45000,
    rating: 4.7,
    downloads: 5000,
  }, 'cat');
  const weak = scoreModel({
    name: 'Chair',
    source: 'nasa',
    sourceUrl: 'https://example.test/chair',
    formats: ['3ds'],
    tags: ['furniture'],
    description: '',
    polygonCount: 300,
  }, 'cat');
  assert.ok(exact > weak);
  assert.ok(exact > 130);
});

test('very low polygon and outdated-only assets are penalized', () => {
  const low = scoreModel({
    name: 'Cat', source: 'polyhaven', sourceUrl: 'https://x/low', formats: ['3ds'], tags: ['cat'], polygonCount: 500,
  }, 'cat');
  const good = scoreModel({
    name: 'Cat', source: 'polyhaven', sourceUrl: 'https://x/good', formats: ['glb'], tags: ['cat'], polygonCount: 30000, hasTextures: true,
  }, 'cat');
  assert.ok(good > low);
});

test('requested format is enforced', () => {
  const model = {
    name: 'Cat', source: 'polyhaven', sourceUrl: 'https://example.test/cat', formats: ['blend', 'fbx'],
  };
  assert.equal(Number.isFinite(scoreModel(model, 'cat', 'obj')), false);
  assert.ok(Number.isFinite(scoreModel(model, 'cat', 'fbx')));
});

test('ranking keeps source diversity and removes duplicate URLs', () => {
  const models = [
    { name: 'Cat A', source: 'sketchfab', sourceUrl: 'https://a.test/cat', formats: ['glb'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC BY' },
    { name: 'Cat A duplicate', source: 'sketchfab', sourceUrl: 'https://a.test/cat', formats: ['glb'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC BY' },
    { name: 'Cat B', source: 'printables', sourceUrl: 'https://b.test/cat', formats: ['stl'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC BY' },
    { name: 'Cat C', source: 'ambientcg', sourceUrl: 'https://c.test/cat', formats: ['obj'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC0' },
    { name: 'Cat D', source: 'polyhaven', sourceUrl: 'https://d.test/cat', formats: ['blend', 'fbx'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC0' },
  ];
  const ranked = diversifyAndRank(models, 'cat', null, 4);
  assert.equal(ranked.length, 4);
  assert.equal(new Set(ranked.map(item => item.source)).size, 4);
  assert.equal(new Set(ranked.map(item => item.sourceUrl)).size, 4);
});
