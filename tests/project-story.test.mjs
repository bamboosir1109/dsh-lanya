import test from 'node:test';
import assert from 'node:assert/strict';
import {sampleStory, joint, DURATION} from '../docs/assets/project-story-motion.js';

test('the timeline clamps seeks and holds its final shot', () => {
  assert.equal(sampleStory(-10).scene, 0);
  assert.equal(sampleStory(NaN).scene, 0);
  assert.equal(sampleStory(8).scene, 1);
  assert.equal(sampleStory(48).scene, 5);
  assert.deepEqual(sampleStory(1000), sampleStory(DURATION));
});

test('character actions change actual poses in every shot', () => {
  const walk = Array.from({length: 25}, (_, i) => sampleStory(1.2 + i * .1));
  assert.ok(Math.max(...walk.map(p => p.x)) - Math.min(...walk.map(p => p.x)) > 250);
  assert.ok(walk.some(p => p.feet[0][1] < -20));
  assert.ok(walk.some(p => p.feet[1][1] < -20));
  const book = sampleStory(10.6);
  assert.ok(book.page > .3 && book.page < .8);
  assert.ok(book.right[1] < sampleStory(8).right[1] - 20);
  assert.ok(sampleStory(18).talk > 0);
  assert.ok(sampleStory(30.7).plane > .99);
  assert.ok(sampleStory(37.5).left[1] < sampleStory(32).left[1] - 100);
  assert.ok(sampleStory(45.8).right[1] < sampleStory(40).right[1] - 100);
});

test('every sampled limb remains finite and retains both bone lengths', () => {
  const length = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  for (let t = 0; t <= DURATION; t += 1 / 60) {
    const p = sampleStory(t);
    for (const value of Object.values(p).flat(2))
      if (typeof value === 'number') assert.ok(Number.isFinite(value));
    for (const [start, end, upper, lower, bend] of [
      [[-38, -132], p.left, 44, 45, -1], [[38, -132], p.right, 44, 45, 1],
      [[-21, -77], p.feet[0], 37, 36, -1], [[21, -77], p.feet[1], 37, 36, -1],
      [[0, 0], [0, 0], 44, 45, 1],
    ]) {
      const k = joint(start, end, upper, lower, bend);
      assert.ok(Math.abs(length(start, k.elbow) - upper) < .001);
      assert.ok(Math.abs(length(k.elbow, k.end) - lower) < .001);
    }
  }
});
