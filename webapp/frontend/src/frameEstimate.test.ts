import assert from 'node:assert/strict';
import test from 'node:test';

import { frameEstimate } from './frameEstimate.ts';

test('默认策略同时估算原始帧、证据节点和模型处理张次', () => {
  const fiveMinutes = frameEstimate(5, false, 5);
  assert.equal(fiveMinutes.intervalSeconds, 1);
  assert.equal(fiveMinutes.frameCount, 300);
  assert.equal(fiveMinutes.evidenceNodeLimit, 18);
  assert.equal(fiveMinutes.modelImagePasses, 108);
  assert.equal(fiveMinutes.visualEvidenceRequestCount, 3);

  const tenMinutes = frameEstimate(10, false, 5);
  assert.equal(tenMinutes.intervalSeconds, 1);
  assert.equal(tenMinutes.frameCount, 600);
  assert.equal(tenMinutes.evidenceNodeLimit, 30);
  assert.equal(tenMinutes.modelImagePasses, 130);
  assert.equal(tenMinutes.visualEvidenceRequestCount, 5);

  const twentyMinutes = frameEstimate(20, false, 5);
  assert.equal(twentyMinutes.intervalSeconds, 5);
  assert.equal(twentyMinutes.frameCount, 240);
  assert.equal(twentyMinutes.evidenceNodeLimit, 36);
  assert.equal(twentyMinutes.modelImagePasses, 136);
  assert.equal(twentyMinutes.visualEvidenceRequestCount, 6);
});

test('稀疏自定义抽帧不会虚报超过原始帧数的证据节点', () => {
  const estimate = frameEstimate(5, true, 30);
  assert.equal(estimate.frameCount, 10);
  assert.equal(estimate.evidenceNodeLimit, 10);
  assert.equal(estimate.modelImagePasses, 60);
  assert.equal(estimate.visualEvidenceRequestCount, 2);
});
