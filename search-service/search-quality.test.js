import test from 'node:test';
import assert from 'node:assert/strict';
import { diversifyAndRank, scoreComponents, scoreModel, hasUsableThumbnail } from './search-quality.js';

test('exact title receives strong relevance score', () => {
  const exact = scoreModel({
    name: 'Birch Tree', source: 'polyhaven', sourceUrl: 'https://example.test/birch', formats: ['glb', 'fbx', 'blend'],
    tags: ['birch', 'tree', 'landscape'], description: 'Realistic birch tree with PBR materials for landscape visualization',
    thumbnailUrl: 'https://example.test/birch.jpg', license: 'CC0', metadata: { polycount: 50000, pbr: true, dimensions: { x: 2, y: 2, z: 8 } },
  }, 'birch', null, { task: 'landscape', category: 'vegetation', software: 'sketchup' });
  const weak = scoreModel({ name: 'Chair', source: 'ambientcg', sourceUrl: 'https://example.test/chair', formats: ['obj'], tags: ['furniture'], description: '' }, 'birch', null, { task: 'landscape', category: 'vegetation', software: 'sketchup' });
  assert.ok(exact > weak);
  const components = scoreComponents({ name: 'Birch Tree', source: 'polyhaven', sourceUrl: 'https://example.test/birch', formats: ['fbx'], tags: ['birch', 'tree', 'landscape'], thumbnailUrl: 'https://example.test/birch.jpg', metadata: { pbr: true } }, 'birch', null, { task: 'landscape', category: 'vegetation', software: 'sketchup' });
  assert.ok(components.taskFit > 0);
  assert.ok(components.technical > 0);
});

test('requested format is enforced', () => {
  const model = { name: 'Birch', source: 'polyhaven', sourceUrl: 'https://example.test/birch', formats: ['blend', 'fbx'], thumbnailUrl: 'https://example.test/birch.jpg' };
  assert.equal(Number.isFinite(scoreModel(model, 'birch', 'obj')), false);
  assert.ok(Number.isFinite(scoreModel(model, 'birch', 'fbx')));
});

test('strict relevance rejects unrelated models and missing previews', () => {
  const models = [
    { name: 'Birch Tree', source: 'polyhaven', sourceUrl: 'https://a.test/birch', formats: ['fbx', 'glb'], tags: ['birch', 'tree', 'landscape'], thumbnailUrl: 'https://a.test/birch.jpg', license: 'CC0' },
    { name: 'Lot with House', source: 'polyhaven', sourceUrl: 'https://b.test/lot', formats: ['blend'], tags: ['architecture', 'house', 'site'], thumbnailUrl: 'https://b.test/lot.jpg', license: 'CC0' },
    { name: 'Unknown Asset', source: 'ambientcg', sourceUrl: 'https://c.test/unknown', formats: ['obj'], tags: ['rock'], license: 'CC0' },
    { name: 'Sketchfab Birch', source: 'sketchfab', sourceUrl: 'https://sketchfab.com/x', formats: ['glb'], tags: ['birch'], thumbnailUrl: 'https://sketchfab.com/x.jpg', license: 'CC0' },
  ];
  const ranked = diversifyAndRank(models, 'birch', null, 5, { task: 'landscape', category: 'vegetation', software: 'blender' });
  assert.deepEqual(ranked.map(item => item.name), ['Birch Tree']);
});

test('thumbnail must be a valid HTTP(S) URL', () => {
  assert.equal(hasUsableThumbnail({ thumbnailUrl: 'https://example.test/model.jpg' }), true);
  assert.equal(hasUsableThumbnail({ thumbnailUrl: 'http://example.test/model.png' }), true);
  assert.equal(hasUsableThumbnail({ thumbnailUrl: '' }), false);
  assert.equal(hasUsableThumbnail({ thumbnailUrl: 'x' }), false);
  assert.equal(hasUsableThumbnail({ thumbnailUrl: 'javascript:alert(1)' }), false);
  assert.equal(hasUsableThumbnail({}), false);
});
