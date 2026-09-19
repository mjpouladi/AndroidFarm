import { parseJob, parseSnapshot, type Job, type Snapshot } from './domain';

const API = '/api/v1';
type JobRequest = { action: string; device?: string; params?: Record<string, unknown> };
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

async function jsonResponse(response: Response): Promise<unknown> {
  // Never render a proxy HTML error document, which may contain infrastructure details.
  if (!response.ok) {
    let message = response.status === 401 ? 'احراز هویت منقضی شده است؛ صفحه را دوباره باز کنید.' :
      response.status === 403 ? 'درخواست رد شد؛ اتصال و مجوز دسترسی را بررسی کنید.' :
      response.status === 502 || response.status === 503 ? 'سرویس کنترل میزبان در دسترس نیست؛ سرویس android-farm-api را بررسی کنید.' : `درخواست API ناموفق بود (HTTP ${response.status}).`;
    if (response.headers.get('content-type')?.includes('application/json')) {
      try {
        const body = await response.json();
        if (typeof body.error?.message === 'string') message = body.error.message;
        else if (typeof body.error === 'string') message = body.error;
        else if (typeof body.message === 'string') message = body.message;
      } catch { /* retain safe status description */ }
    }
    throw new ApiError(message, response.status);
  }
  if (!response.headers.get('content-type')?.includes('application/json')) throw new ApiError('آدرس API پاسخ JSON نمی‌دهد؛ مسیر /api/ در استقرار را بررسی کنید.', response.status);
  return response.json();
}

export async function fetchSnapshot(signal?: AbortSignal): Promise<Snapshot> {
  const response = await fetch(`${API}/snapshot`, { credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
    headers: { Accept: 'application/json' } });
  return parseSnapshot(await jsonResponse(response));
}

export function requestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // getRandomValues also works on a private HTTP/IP deployment.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, n => n.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function postJob(path: string, csrf: string, body: object, key: string): Promise<Job> {
  if (!csrf) throw new ApiError('ابتدا منتظر اتصال تازه به میزبان بمانید.', 0);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20_000);
  try {
    const response = await fetch(path, { method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Farm-CSRF': csrf, 'Idempotency-Key': key }, body: JSON.stringify(body) });
    const result = await jsonResponse(response) as { job?: unknown };
    return parseJob(result.job);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError('پاسخ عملیات دریافت نشد؛ ممکن است درخواست ثبت شده باشد. پیش از تکرار، صف عملیات را بررسی کنید.', 0);
  } finally { clearTimeout(timer); }
}
export const submitJob = (body: JobRequest, csrf: string, key = requestId()) => postJob(`${API}/jobs`, csrf, body, key);
export const cancelJob = (id: string, csrf: string) => postJob(`${API}/jobs/${encodeURIComponent(id)}/cancel`, csrf, {}, requestId());

// Completion-based scheduling prevents overlapping snapshots on a busy host.
export function pollSnapshots(onData: (data: Snapshot) => void, onError: (error: Error) => void, delay = 5000): () => void {
  let stopped = false;
  let scheduled: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  async function poll() {
    controller = new AbortController();
    const timeout = setTimeout(() => controller?.abort(), 90_000);
    try {
      const data = await fetchSnapshot(controller.signal);
      if (!stopped) onData(data);
    } catch (error) {
      if (!stopped) onError(error instanceof ApiError || error instanceof Error && error.name !== 'AbortError' ? error : new Error('دریافت وضعیت میزبان بیش از حد طول کشید؛ اتصال دوباره بررسی می‌شود.'));
    } finally {
      clearTimeout(timeout);
      if (!stopped) scheduled = setTimeout(poll, delay);
    }
  }
  void poll();
  return () => { stopped = true; clearTimeout(scheduled); controller?.abort(); };
}
