export const FRAME_ESTIMATE_DURATIONS = [5, 10, 20, 30, 50];

const ESTIMATED_FRAME_BYTES = 220 * 1024;
const MODEL_DIMENSION_COUNT = 5;
const MODEL_MAX_FRAMES_PER_DIMENSION = 20;
const VISUAL_BATCH_SIZE = 6;
const ESTIMATED_REQUEST_BASE_SECONDS = 55;
const ESTIMATED_SECONDS_PER_IMAGE = 1.2;

function defaultFrameInterval(durationMinutes: number): number {
  return durationMinutes <= 10 ? 1 : 5;
}

function evidenceNodeBudget(durationMinutes: number): number {
  if (durationMinutes <= 1) return 10;
  if (durationMinutes <= 5) return 18;
  if (durationMinutes <= 10) return 30;
  return 36;
}

export function frameEstimate(
  durationMinutes: number,
  customIntervalEnabled: boolean,
  customIntervalSeconds: number,
) {
  const intervalSeconds = customIntervalEnabled ? customIntervalSeconds : defaultFrameInterval(durationMinutes);
  const durationSeconds = durationMinutes * 60;
  const frameCount = Math.ceil(durationSeconds / Math.max(1, intervalSeconds));
  const storageBytes = frameCount * ESTIMATED_FRAME_BYTES;
  const evidenceNodeLimit = Math.min(frameCount, evidenceNodeBudget(durationMinutes));
  const visualEvidenceRequestCount = Math.ceil(evidenceNodeLimit / VISUAL_BATCH_SIZE);
  const dimensionImagePasses = Math.min(evidenceNodeLimit, MODEL_MAX_FRAMES_PER_DIMENSION) * MODEL_DIMENSION_COUNT;
  const modelImagePasses = evidenceNodeLimit + dimensionImagePasses;
  const modelRequestCount = visualEvidenceRequestCount + MODEL_DIMENSION_COUNT;
  const modelTimeSeconds = modelRequestCount * ESTIMATED_REQUEST_BASE_SECONDS + modelImagePasses * ESTIMATED_SECONDS_PER_IMAGE;

  return {
    intervalSeconds,
    frameCount,
    storageBytes,
    evidenceNodeLimit,
    visualEvidenceRequestCount,
    modelImagePasses,
    modelTimeSeconds,
  };
}
