import test from 'node:test';
import assert from 'node:assert/strict';
import { diversifyAndRank, scoreComponents, scoreModel } from './search-quality.js';

test('exact title receives strong relevance score', () => {
  const exact = scoreModel({
    name: 'Birch Tree',
    source: 'polyhaven',
    sourceUrl: 'https://example.test/birch',
    formats: ['glb', 'fbx', 'blend'],
    tags: ['birch', 'tree', 'landscape'],
    description: 'Realistic birch tree with PBR materials for landscape visualization',
    thumbnailUrl: 'https://example.test/birch.jpg',
    license: 'CC0',
    metadata: { polycount: 50000, pbr: true, dimensions: { x: 2, y: 2, z: 8 } },
  }, 'birch', null, { task: 'landscape', category: 'vegetation', software: 'sketchup' });
  const weak = scoreModel({
    name: 'Chair',
    source: 'ambientcg',
    sourceUrl: 'https://example.test/chair',
    formats: ['obj'],
    tags: ['furniture'],
    description: '',
  }, 'birch', null, { task: 'landscape', category: 'vegetation', software: 'sketchup' });
  assert.ok(exact > weak);
  const components = scoreComponents({ name: 'Birch Tree', source: 'polyhaven', sourceUrl: 'x', formats: ['fbx'], tags: ['birch', 'tree', 'landscape'], thumbnailUrl: 'x', metadata: { pbr: true } }, 'birch', null, { task: 'landscape', category: 'vegetation', software: 'sketchup' });
  assert.ok(components.taskFit > 0);
  assert.ok(components.technical > 0);
});

test('requested format is enforced', () => {
  const model = {
    name: 'Birch',
    source: 'polyhaven',
    sourceUrl: 'https://example.test/birch',
    formats: ['blend', 'fbx'],
  };
  assert.equal(Number.isFinite(scoreModel(model, 'birch', 'obj')), false);
  assert.ok(Number.isFinite(scoreModel(model, 'birch', 'fbx')));
});

test('ranking favors task-fit and removes duplicate URLs', () => {
  const models = [
    { name: 'Birch Tree', source: 'polyhaven', sourceUrl: 'https://a.test/birch', formats: ['fbx', 'glb'], tags: ['birch', 'tree', 'landscape'], thumbnailUrl: 'x', license: 'CC0', metadata: { pbr: true, polycount: 50000 } },
    { name: 'Birch Tree duplicate', source: 'polyhaven', sourceUrl: 'https://a.test/birch', formats: ['fbx'], tags: ['birch'], thumbnailUrl: 'x', license: 'CC0' },
    { name: 'Unrelated Model', source: 'polyhaven', sourceUrl: 'https://b.test/unrelated', formats: ['obj'], tags: ['furniture'], thumbnailUrl: 'x' },
    { name: 'Garden Bench', source: 'ambientcg', sourceUrl: 'https://c.test/bench', formats: ['glb'], tags: ['bench', 'garden'], thumbnailUrl: 'x', license: 'CC0' },
  ];
  const ranked = diversifyAndRank(models, 'birch', null, 3, { task: 'landscape', category: 'vegetation', software: 'sketchup' });
  assert.equal(ranked.length, 3);
  assert.equal(ranked[0].name, 'Birch Tree');
  assert.equal(new Set(ranked.map(item => item.sourceUrl)).size, 3);
});
