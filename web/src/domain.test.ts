import { describe, expect, it } from 'vitest';
import { demoState, reducer, reserved } from './domain';

describe('چرخهٔ دستگاه و محدودیت ظرفیت در شبیه‌ساز', () => {
  it('اولویت صف زمان درخواست است، نه شماره دستگاه', () => {
    let state = demoState();
    for (const id of ['num07', 'num09', 'num10', 'num50', 'num12']) state = reducer(state, { type: 'start', id });
    state = reducer(state, { type: 'stop', id: 'num01' });
    state = reducer(state, { type: 'tick' });
    expect(state.devices.find(d => d.id === 'num50')!.status).toBe('booting');
    expect(state.devices.find(d => d.id === 'num12')!.status).toBe('queued');
  });
  it('دستگاه یازدهم را صف می‌کند و شروع تکراری ظرفیت اضافه نمی‌گیرد', () => {
    let state = demoState();
    for (const id of ['num07', 'num09', 'num10', 'num12']) state = reducer(state, { type: 'start', id });
    expect(reserved(state.devices)).toBe(10);
    expect(state.devices.find(d => d.id === 'num12')!.status).toBe('queued');
    state = reducer(state, { type: 'start', id: 'num07' });
    expect(reserved(state.devices)).toBe(10);
  });
  it('ظرفیت تا تأیید توقف حفظ و سپس به دستگاه صف تخصیص داده می‌شود', () => {
    let state = demoState();
    for (const id of ['num07', 'num09', 'num10', 'num12']) state = reducer(state, { type: 'start', id });
    state = reducer(state, { type: 'stop', id: 'num01' });
    expect(reserved(state.devices)).toBe(10);
    expect(state.devices.find(d => d.id === 'num12')!.status).toBe('queued');
    state = reducer(state, { type: 'tick' });
    expect(state.devices.find(d => d.id === 'num01')!.status).toBe('off');
    expect(state.devices.find(d => d.id === 'num12')!.status).toBe('booting');
    expect(reserved(state.devices)).toBe(10);
  });
  it('بدون پراکسی سالم، شروع نمی‌کند', () => {
    const state = reducer(demoState(), { type: 'start', id: 'num04' });
    expect(state.devices[3].status).toBe('error');
    expect(reserved(state.devices)).toBe(7);
  });
  it('بکاپ فقط در حالت خاموش انجام می‌شود و شماره ثابت می‌ماند', () => {
    let state = demoState();
    state = reducer(state, { type: 'backup', id: 'num01' });
    expect(state.devices[0].status).toBe('running');
    const phone = state.devices[6].phone;
    state = reducer(state, { type: 'backup', id: 'num07' });
    state = reducer(state, { type: 'tick' });
    expect(state.backups[0].device).toBe('num07');
    expect(state.devices[6].status).toBe('off');
    expect(state.devices[6].phone).toBe(phone);
  });
  it('شماره تکراری یا نامعتبر را رد می‌کند و num71 یکتا می‌سازد', () => {
    let state = demoState();
    state = reducer(state, { type: 'add', label: 'تست', phone: state.devices[0].phone });
    expect(state.devices.length).toBe(70);
    state = reducer(state, { type: 'add', label: 'تست', phone: '123' });
    expect(state.devices.length).toBe(70);
    state = reducer(state, { type: 'add', label: 'تست', phone: '+989000009999' });
    expect(state.devices.at(-1)?.id).toBe('num71');
  });
  it('صف لغوشده دوباره راه‌اندازی نمی‌شود', () => {
    let state = demoState();
    for (const id of ['num07', 'num09', 'num10', 'num12']) state = reducer(state, { type: 'start', id });
    state = reducer(state, { type: 'cancel', id: 'num12' });
    state = reducer(state, { type: 'stop', id: 'num01' });
    state = reducer(state, { type: 'tick' });
    expect(state.devices[11].status).toBe('off');
  });
});
