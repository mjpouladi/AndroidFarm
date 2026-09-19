import { describe, expect, it } from 'vitest';
import { parseResourceReport } from './resources';

describe('راه‌اندازی ترتیبی و گزارش میزبان', () => {
  const report = { schema_version: 1, collected_at: Date.now() / 1000, cpu_cores: 72, ram_gib: 96, available_ram_gib: 80, disk_free_gib: 500, capacity: 10 };
  it('گزارش معتبر را می‌خواند', () => expect(parseResourceReport(report).capacity).toBe(10));
  it('ظرفیت بزرگ‌تر را برای میزبان قوی‌تر می‌پذیرد', () => {
    const large = parseResourceReport({ ...report, schema_version: 2, capacity: 19,
      active_capacity: 19, catalog_capacity: 120 });
    expect(large.active_capacity).toBe(19);
  });
  it('گزارش قدیمی، ظرفیت منفی و حافظه نامعتبر را رد می‌کند', () => {
    for (const invalid of [{ capacity: -1 }, { collected_at: 0 }, { ram_gib: '96' }, { available_ram_gib: 100 }]) {
      expect(() => parseResourceReport({ ...report, ...invalid })).toThrow();
    }
  });
});
