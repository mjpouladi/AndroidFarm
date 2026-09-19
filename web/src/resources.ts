export type ResourceReport = { schema_version: 1; collected_at: number; cpu_cores: number; ram_gib: number;
  available_ram_gib: number; disk_free_gib: number; capacity: number };

export function parseResourceReport(value: unknown): ResourceReport {
  if (!value || typeof value !== 'object') throw new Error('گزارش نامعتبر است.');
  const report = value as Record<string, unknown>;
  if (report.schema_version !== 1) throw new Error('نسخهٔ گزارش پشتیبانی نمی‌شود.');
  for (const key of ['collected_at', 'cpu_cores', 'ram_gib', 'available_ram_gib', 'disk_free_gib', 'capacity']) {
    if (typeof report[key] !== 'number' || !Number.isFinite(report[key]) || report[key] < 0) throw new Error('مقدار منابع نامعتبر است.');
  }
  if (!Number.isInteger(report.capacity) || (report.capacity as number) > 10 || (report.cpu_cores as number) <= 0 ||
      (report.ram_gib as number) <= 0 || (report.available_ram_gib as number) > (report.ram_gib as number)) throw new Error('مقادیر گزارش سازگار نیستند.');
  if (Math.abs(Date.now() / 1000 - (report.collected_at as number)) > 3600) throw new Error('گزارش قدیمی است؛ گزارش یک ساعت اخیر را وارد کنید.');
  return report as ResourceReport;
}
