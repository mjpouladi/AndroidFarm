import { afterEach, describe, expect, it, vi } from 'vitest';
import { cancelJob, fetchSnapshot, pollSnapshots, submitJob } from './api';

const snapshotData = () => ({ schema_version: 1, collected_at: Date.now() / 1000, csrf_token: 'test-csrf',
  resources: null, devices: [], proxies: [], artifacts: [], backups: [], errors: [], jobs: [], settings: {} });
const job = { id: 'd156c9ce-557a-4f61-bcdd-78c302b775e4', action: 'up', device: 'num01', state: 'queued', created_at: 1, updated_at: 1, error: null, result: null };
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('درخواست‌های واقعی API', () => {
  it('وضعیت را بدون کش و با احراز هویت همان مبدأ می‌خواند', async () => {
    const mock = vi.fn().mockResolvedValue(response(snapshotData())); vi.stubGlobal('fetch', mock);
    expect((await fetchSnapshot()).devices).toEqual([]);
    expect(mock).toHaveBeenCalledWith('/api/v1/snapshot', expect.objectContaining({ cache: 'no-store', credentials: 'same-origin', redirect: 'error' }));
  });
  it('به جای HTML خطای proxy، پیام قابل اقدام برمی‌گرداند', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>gateway private details</html>', { status: 502 })));
    await expect(fetchSnapshot()).rejects.toThrow('android-farm-api');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>SPA index</html>', { headers: { 'Content-Type': 'text/html' } })));
    await expect(fetchSnapshot()).rejects.toThrow('/api/');
  });
  it('پیام JSON خطا را نمایش می‌دهد و موفقیت جعلی ایجاد نمی‌کند', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ error: { code: 'capacity', message: 'ظرفیت کافی نیست' } }, 409)));
    await expect(submitJob({ action: 'up', device: 'num01' }, 'csrf', 'request-key')).rejects.toThrow('ظرفیت کافی نیست');
  });
  it('فرمان را با CSRF و کلید یکتایی می‌فرستد و فقط پاسخ ثبت‌شده را می‌پذیرد', async () => {
    const mock = vi.fn().mockResolvedValue(response({ job }, 202)); vi.stubGlobal('fetch', mock);
    expect(await submitJob({ action: 'up', device: 'num01' }, 'csrf', 'request-key')).toEqual(job);
    expect(mock).toHaveBeenCalledWith('/api/v1/jobs', expect.objectContaining({ method: 'POST',
      headers: expect.objectContaining({ 'X-Farm-CSRF': 'csrf', 'Idempotency-Key': 'request-key' }), body: JSON.stringify({ action: 'up', device: 'num01' }) }));
  });
  it('بدون CSRF هیچ تغییری ارسال نمی‌کند', async () => {
    const mock = vi.fn(); vi.stubGlobal('fetch', mock);
    await expect(submitJob({ action: 'down', device: 'num01' }, '', 'key')).rejects.toThrow();
    expect(mock).not.toHaveBeenCalled();
  });
  it('لغو را با مسیر API و بدنهٔ خالی ارسال می‌کند', async () => {
    const mock = vi.fn().mockResolvedValue(response({ job: { ...job, state: 'cancelled' } })); vi.stubGlobal('fetch', mock);
    expect((await cancelJob(job.id, 'csrf')).state).toBe('cancelled');
    expect(mock.mock.calls[0][0]).toBe(`/api/v1/jobs/${job.id}/cancel`);
    expect(mock.mock.calls[0][1].body).toBe('{}');
  });
  it('خطای شبکه پس از ارسال را نتیجهٔ نامعلوم می‌داند و خودکار تکرار نمی‌کند', async () => {
    const mock = vi.fn().mockRejectedValue(new TypeError('network lost')); vi.stubGlobal('fetch', mock);
    await expect(submitJob({ action: 'up', device: 'num01' }, 'csrf', 'key')).rejects.toThrow('ممکن است درخواست ثبت شده باشد');
    expect(mock).toHaveBeenCalledTimes(1);
  });
  it('خواندن‌های آهسته را همپوشان نمی‌کند و پس از unmount پاسخ دیررس را کنار می‌گذارد', async () => {
    vi.useFakeTimers(); let resolve!: (value: Response) => void;
    const mock = vi.fn().mockImplementation(() => new Promise<Response>(done => { resolve = done; })); vi.stubGlobal('fetch', mock);
    const onData = vi.fn(); const onError = vi.fn(); const stop = pollSnapshots(onData, onError);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(mock).toHaveBeenCalledTimes(1);
    const signal = mock.mock.calls[0][1].signal as AbortSignal;
    stop(); expect(signal.aborted).toBe(true);
    resolve(response(snapshotData())); await vi.advanceTimersByTimeAsync(50_000);
    expect(onData).not.toHaveBeenCalled(); expect(onError).not.toHaveBeenCalled(); expect(mock).toHaveBeenCalledTimes(1);
  });
  it('پس از شکست snapshot دوباره می‌خواند؛ خطا به دادهٔ سالم تبدیل نمی‌شود', async () => {
    vi.useFakeTimers(); const mock = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(response(snapshotData())); vi.stubGlobal('fetch', mock);
    const onData = vi.fn(); const onError = vi.fn(); const stop = pollSnapshots(onData, onError);
    await vi.advanceTimersByTimeAsync(0); expect(onError).toHaveBeenCalledTimes(1); expect(onData).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000); expect(onData).toHaveBeenCalledTimes(1); stop();
  });
});
