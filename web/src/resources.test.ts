import { describe, expect, it } from 'vitest';
import { initialState, reducer } from './domain';
import { parseResourceReport } from './resources';

describe('راه‌اندازی ترتیبی و گزارش میزبان', () => {
  it('با ناوگان خالی شروع می‌کند و اولین دستگاه num01 است', () => {
    const initial = initialState();
    expect(initial.devices).toHaveLength(0);
    expect(reducer(initial, { type: 'add', label: '', phone: '+989000000001' }).devices[0].id).toBe('num01');
  });
  const report = { schema_version: 1, collected_at: Date.now() / 1000, cpu_cores: 72, ram_gib: 96, available_ram_gib: 80, disk_free_gib: 500, capacity: 10 };
  it('گزارش معتبر را می‌خواند', () => expect(parseResourceReport(report).capacity).toBe(10));
  it('گزارش قدیمی، ظرفیت ساختگی و حافظه نامعتبر را رد می‌کند', () => {
    for (const invalid of [{ capacity: 70 }, { collected_at: 0 }, { ram_gib: '96' }, { available_ram_gib: 100 }]) {
      expect(() => parseResourceReport({ ...report, ...invalid })).toThrow();
    }
  });
});
