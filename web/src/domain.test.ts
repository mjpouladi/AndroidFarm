import { describe, expect, it } from 'vitest';
import { activeDevices, deviceStatus, isSnapshotFresh, parseSnapshot, provisionableProxies, screenPath, type Device, type Job, type ProxyRecord } from './domain';

const device: Device = { id: 'num01', phase: 'ready_for_operator', hold: null,
  containers: { android: 'running', proxy: 'healthy', screen: 'running' }, adb: '127.0.0.1:5551',
  screen_path: '/d/num01/', screen_ready: true, proxy: null, expected_egress_ip: null, phone: null, cpu: null, memory: null };
export const snapshotData = () => ({ schema_version: 1, collected_at: Date.now() / 1000, csrf_token: 'test-csrf',
  resources: null, devices: [], proxies: [], artifacts: [], backups: [], errors: [], jobs: [], settings: {} });
const job: Job = { id: 'queue-test', action: 'down', device: 'num01', state: 'queued', created_at: 1, updated_at: 1, error: null, result: null };

describe('وضعیت واقعی و مرز اعتماد API', () => {
  it('ناوگان خالی و منابع ناموجود را بدون تولید داده نگه می‌دارد', () => {
    const result = parseSnapshot(snapshotData());
    expect(result.devices).toEqual([]);
    expect(result.resources).toBeNull();
  });
  it('فیلدهای اندازه‌گیری‌نشده و تنظیمات ناقص را می‌پذیرد', () => {
    const result = parseSnapshot({ ...snapshotData(), devices: [device], errors: [{ component: 'configuration', message: 'unavailable' }] });
    expect(result.devices[0].cpu).toBeNull();
    expect(result.devices[0].phone).toBeNull();
    expect(result.settings).toEqual({});
  });
  it('پاسخ خراب یا نسخهٔ ناشناخته را به موفقیت تبدیل نمی‌کند', () => {
    for (const override of [{ schema_version: 9 }, { devices: {} }, { csrf_token: '' }, { devices: [{ ...device, containers: null }] }, { jobs: [{ ...job, state: 'magic' }] }]) {
      expect(() => parseSnapshot({ ...snapshotData(), ...override })).toThrow();
    }
  });
  it('قطع اتصال یا گزارش قدیمی مجوز تغییر ایجاد نمی‌کند', () => {
    const snapshot = parseSnapshot(snapshotData());
    expect(isSnapshotFresh(snapshot)).toBe(true);
    expect(isSnapshotFresh({ ...snapshot, collected_at: snapshot.collected_at - 21 })).toBe(false);
    expect(isSnapshotFresh({ ...snapshot, collected_at: snapshot.collected_at + 6 })).toBe(false);
    expect(isSnapshotFresh(null)).toBe(false);
  });
  it('صف توقف را با پایان واقعی توقف اشتباه نمی‌گیرد', () => {
    expect(deviceStatus(device, [job])).toBe('running');
    expect(deviceStatus(device, [{ ...job, state: 'running' }])).toBe('stopping');
    expect(activeDevices([device])).toBe(1);
    expect(deviceStatus({ ...device, containers: { ...device.containers, android: 'stopped' } }, [])).toBe('off');
  });
  it('توقف حفاظتی و خرابی کانتینر را آشکار نگه می‌دارد', () => {
    expect(deviceStatus({ ...device, hold: { reason: 'ip-change' } }, [])).toBe('error');
    expect(deviceStatus({ ...device, containers: { ...device.containers, proxy: 'unhealthy' } }, [])).toBe('error');
    expect(deviceStatus({ ...device, containers: { ...device.containers, android: 'unknown' } }, [])).toBe('unknown');
  });
  it('فقط مسیر همان دستگاه و همان مبدأ را در iframe می‌پذیرد', () => {
    expect(screenPath(device)).toBe('/d/num01/');
    for (const path of ['https://example.test/d/num01/', '//example.test/', '/d/num02/', '/d/num01/?token=x', '/d/num01/../']) {
      expect(screenPath({ ...device, screen_path: path })).toBeNull();
    }
    expect(screenPath({ id: '../bad', screen_path: '/d/../bad/' })).toBeNull();
    expect(screenPath({ id: 'num101', screen_path: '/d/num101/' })).toBe('/d/num101/');
  });
  it('کانتینر معلق یا در حال بازراه‌اندازی را خاموش یا آمادهٔ تصویر نشان نمی‌دهد', () => {
    const paused = { ...device, containers: { ...device.containers, android: 'paused' } };
    const restarting = { ...device, containers: { ...device.containers, android: 'restarting' } };
    expect(deviceStatus(paused, [])).toBe('error');
    expect(deviceStatus(restarting, [])).toBe('booting');
    expect(activeDevices([paused, restarting])).toBe(2);
    expect(activeDevices([{ ...restarting, running: false }])).toBe(0);
  });
  it('پراکسی نصب ناتمام را برای ادامه می‌پذیرد و دستگاه تکمیل‌شده را دوباره تخصیص نمی‌دهد', () => {
    const proxy: ProxyRecord = { id: 'proxy-01', label: 'QA', type: 'http', server: '192.0.2.1', server_port: 8080,
      expected_egress_ip: '192.0.2.2', assigned_device: device.id, state: 'enabled' };
    expect(provisionableProxies([proxy], [device])).toEqual([]);
    expect(provisionableProxies([proxy], [{ ...device, phase: 'failed' }])).toEqual([proxy]);
    expect(provisionableProxies([{ ...proxy, state: 'disabled' }], [{ ...device, phase: 'failed' }])).toEqual([]);
    expect(provisionableProxies([proxy], [])).toEqual([]);
    expect(provisionableProxies([{ ...proxy, assigned_device: null }], [])).toHaveLength(1);
  });
});
