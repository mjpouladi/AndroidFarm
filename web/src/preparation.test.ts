import { afterEach, describe, expect, it, vi } from 'vitest';
import { submitJob } from './api';
import { preparationDialog, preparationRequest, preparationTitle, type PreparationValues, type ResumeTarget } from './preparation';

const target: ResumeTarget = { id: 'num04', phone: '+98•••••1234', egress: 'direct', proxyId: '' };
const values: PreparationValues = { phone: '+989121234567', owner_authorized: true, artifact_id: 'qa-app', egress: 'direct' };
afterEach(() => vi.unstubAllGlobals());

describe('ادامهٔ آماده‌سازی همان دستگاه', () => {
  it('شناسهٔ انتخاب‌شده را تا ارسال واقعی API حفظ می‌کند و شماره را از ورودی صریح می‌گیرد', async () => {
    const dialog = preparationDialog(null, { type: 'resume', target });
    const request = preparationRequest(dialog, values);
    expect(preparationTitle(dialog!)).toBe('ادامهٔ آماده‌سازی num04');
    expect(request).toEqual({ action: 'resume', device: 'num04', params: values });
    expect(request.params.phone).not.toBe(target.phone);
    const job = { id: 'resume-job', action: 'resume', device: 'num04', state: 'queued', created_at: 1, updated_at: 1, error: null, result: null };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job }), { status: 202, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetch);
    await submitJob(request, 'csrf', 'resume-request');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ action: 'resume', device: 'num04', params: values });
  });
  it('انصراف هدف قبلی را کنار می‌گذارد و از فرم بسته درخواست نمی‌سازد', () => {
    const resumed = preparationDialog(null, { type: 'resume', target });
    const closed = preparationDialog(resumed, { type: 'close' });
    expect(closed).toBeNull();
    expect(() => preparationRequest(closed, values)).toThrow('ابتدا فرم');
  });
  it('افزودن پس از ادامه، دوباره provision بدون شناسه می‌فرستد', () => {
    const resumed = preparationDialog(null, { type: 'resume', target });
    for (const preceding of [resumed, preparationDialog(resumed, { type: 'close' })]) {
      const fresh = preparationDialog(preceding, { type: 'new' });
      expect(fresh?.target).toBeNull();
      expect(preparationTitle(fresh!)).toBe('آماده‌سازی دستگاه');
      expect(JSON.parse(JSON.stringify(preparationRequest(fresh, values)))).toEqual({ action: 'provision', params: values });
    }
  });
  it('درخواست ادامه اجازهٔ تغییر مسیر خروجی یا پراکسی را نمی‌دهد', () => {
    const direct = preparationDialog(null, { type: 'resume', target });
    expect(() => preparationRequest(direct, { ...values, egress: 'proxy', proxy_id: 'other' })).toThrow('درخواست اولیه');
    const proxied = preparationDialog(null, { type: 'resume', target: { ...target, egress: 'proxy', proxyId: 'original' } });
    expect(preparationRequest(proxied, { ...values, egress: 'proxy', proxy_id: 'original' }).params.proxy_id).toBe('original');
    expect(() => preparationRequest(proxied, { ...values, egress: 'proxy', proxy_id: 'other' })).toThrow('درخواست اولیه');
    expect(() => preparationRequest(proxied, values)).toThrow('درخواست اولیه');
  });
  it('برای ادامه، دستگاه دیگری را حدس نمی‌زند و هدف ناشناخته را نمی‌پذیرد', () => {
    expect(() => preparationDialog(null, { type: 'resume', target: { ...target, id: '../num04' } })).toThrow('شناسه');
    const second = preparationDialog(preparationDialog(null, { type: 'resume', target }), { type: 'resume', target: { ...target, id: 'num05' } });
    expect(preparationRequest(second, values).device).toBe('num05');
  });
});
