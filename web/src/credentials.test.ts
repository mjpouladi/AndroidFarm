import { describe, expect, it } from 'vitest';
import { credentialRequest, proxyCredentialRequest, type CredentialForm } from './credentials';

const form: CredentialForm = { target: 'platform', username: 'qa-operator', password: 'test-only-strong-password',
  confirmation: 'test-only-strong-password', current_password: 'test-only-current', acknowledged: true };
describe('فرم مدیریت مرکزی اطلاعات ورود', () => {
  it('فقط payload لازم را می‌فرستد؛ تأیید و UI preferences وارد صف نمی‌شوند', () => {
    expect(credentialRequest(form)).toEqual({ action: 'credential-rotate', params: {
      target: 'platform', username: 'qa-operator', password: form.password, current_password: form.current_password,
    } });
  });
  it('تأیید رمز و احراز هویت فعلی و اطلاع از ورود مجدد اجباری‌اند', () => {
    for (const override of [{ current_password: '' }, { confirmation: 'different' }, { acknowledged: false }]) {
      expect(() => credentialRequest({ ...form, ...override })).toThrow();
    }
  });
  it('محدودیت bcrypt را بر اساس بایت UTF8 می‌سنجد', () => {
    const valid = 'الف'.repeat(12); // 72 UTF-8 bytes.
    expect(credentialRequest({ ...form, password: valid, confirmation: valid }).params.password).toBe(valid);
    const tooLong = `${valid}a`;
    expect(() => credentialRequest({ ...form, password: tooLong, confirmation: tooLong })).toThrow('۷۲');
    expect(() => credentialRequest({ ...form, password: 'short', confirmation: 'short' })).toThrow();
  });
  it('نام کاربری نامعتبر و نویسهٔ کنترل را رد می‌کند', () => {
    for (const username of ['', '-operator', 'user:name', 'a'.repeat(65)]) expect(() => credentialRequest({ ...form, username })).toThrow();
    for (const control of ['\n', '\r', '\0', '\t', '\x7f', ' ']) {
      const password = `test-only-password${control}`;
      expect(() => credentialRequest({ ...form, password, confirmation: password })).toThrow();
    }
  });
  it('تغییر رمز پراکسی نام کاربری حاوی sticky identity را تغییر نمی‌دهد', () => {
    const request = proxyCredentialRequest({ id: 'proxy-01', password: 'provider-secret', current_password: 'current', acknowledged: true });
    expect(request).toEqual({ action: 'proxy-credentials', params: { id: 'proxy-01', password: 'provider-secret', current_password: 'current' } });
    expect(request.params).not.toHaveProperty('username');
  });
  it('رمز پراکسی کوتاه سرویس‌دهنده را می‌پذیرد؛ بدون تأیید توقف یا با کنترل ارسال نمی‌کند', () => {
    const proxy = { id: 'proxy-01', password: 'x', current_password: 'current', acknowledged: true };
    expect(proxyCredentialRequest(proxy).params.password).toBe('x');
    for (const override of [{ acknowledged: false }, { current_password: '' }, { password: 'bad\tvalue' }, { password: 'a'.repeat(4097) }, { password: ' trailing ' }]) {
      expect(() => proxyCredentialRequest({ ...proxy, ...override })).toThrow();
    }
  });
});
