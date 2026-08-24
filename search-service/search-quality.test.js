import test from 'node:test';
import assert from 'node:assert/strict';
import { diversifyAndRank, scoreModel } from './search-quality.js';

test('exact title receives strong relevance score', () => {
  const exact = scoreModel({
    name: 'Cat',
    source: 'sketchfab',
    sourceUrl: 'https://example.test/cat',
    formats: ['glb'],
    tags: ['cat'],
    description: 'A downloadable cat model',
    thumbnailUrl: 'https://example.test/cat.jpg',
    license: 'CC BY',
  }, 'cat');
  const weak = scoreModel({
    name: 'Chair',
    source: 'free3d',
    sourceUrl: 'https://example.test/chair',
    formats: ['obj'],
    tags: ['furniture'],
    description: '',
  }, 'cat');
  assert.ok(exact > weak);
  assert.ok(exact > 100);
});

test('requested format is enforced', () => {
  const model = {
    name: 'Cat',
    source: 'polyhaven',
    sourceUrl: 'https://example.test/cat',
    formats: ['blend', 'fbx'],
  };
  assert.equal(Number.isFinite(scoreModel(model, 'cat', 'obj')), false);
  assert.ok(Number.isFinite(scoreModel(model, 'cat', 'fbx')));
});

test('ranking keeps source diversity and removes duplicate URLs', () => {
  const models = [
    { name: 'Cat A', source: 'sketchfab', sourceUrl: 'https://a.test/cat', formats: ['glb'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC BY' },
    { name: 'Cat A duplicate', source: 'sketchfab', sourceUrl: 'https://a.test/cat', formats: ['glb'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC BY' },
    { name: 'Cat B', source: 'printables', sourceUrl: 'https://b.test/cat', formats: ['stl'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC BY' },
    { name: 'Cat C', source: 'cgtrader', sourceUrl: 'https://c.test/cat', formats: ['obj'], tags: ['cat'], thumbnailUrl: 'x', license: 'Royalty Free' },
    { name: 'Cat D', source: 'polyhaven', sourceUrl: 'https://d.test/cat', formats: ['blend', 'fbx'], tags: ['cat'], thumbnailUrl: 'x', license: 'CC0' },
  ];
  const ranked = diversifyAndRank(models, 'cat', null, 4);
  assert.equal(ranked.length, 4);
  assert.equal(new Set(ranked.map(item => item.source)).size, 4);
  assert.equal(new Set(ranked.map(item => item.sourceUrl)).size, 4);
});
