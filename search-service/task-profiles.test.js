import test from 'node:test';
import assert from 'node:assert/strict';
import { getTaskTerms, inferTaskProfile } from './task-profiles.js';

test('defaults to landscape and detects vegetation/software', () => {
  const profile = inferTaskProfile('берёза для ландшафта в SketchUp');
  assert.equal(profile.task, 'landscape');
  assert.equal(profile.category, 'vegetation');
  assert.equal(profile.software, 'sketchup');
  assert.deepEqual(getTaskTerms(profile), ['landscape', 'vegetation', 'sketchup']);
});

test('detects landscape furniture and lighting', () => {
  assert.equal(inferTaskProfile('уличная скамейка', null, null).category, 'furniture');
  assert.equal(inferTaskProfile('садовый фонарь', null, null).category, 'lighting');
});
