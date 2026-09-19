import { describe, expect, it } from 'vitest';
import { actionText, activeDevices, deviceStatus, diagnoseCommand, egressText, eventText, isSnapshotFresh, parseSnapshot, provisionableProxies, resumablePhases, screenPath, startablePhases, type Device, type Job, type ProxyRecord } from './domain';

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
  it('خروجی مستقیم را فقط با مقدار معتبر می‌پذیرد و بدون پراکسی نمایش می‌دهد', () => {
    const direct = parseSnapshot({ ...snapshotData(), devices: [{ ...device, egress: 'direct' }] }).devices[0];
    expect(direct.egress).toBe('direct');
    expect(egressText(direct)).toBe('خروجی مستقیم میزبان');
    expect(egressText({ ...device, proxy: 'socks://8.8.8.8:1080' })).toBe('socks://8.8.8.8:1080');
    expect(egressText(device)).toBe('—');
    expect(() => parseSnapshot({ ...snapshotData(), devices: [{ ...device, egress: 'tor' }] })).toThrow();
  });
  it('مخزن APK را با فراداده‌های اختیاری می‌خواند و مقادیر نامعتبر را رد می‌کند', () => {
    const artifact = { id: 'whatsapp', label: 'WhatsApp', package: 'com.whatsapp', available: true, version: '2.24.1', signers: ['a'.repeat(64)], imported_at: 1 };
    expect(parseSnapshot({ ...snapshotData(), artifacts: [artifact] }).artifacts[0].version).toBe('2.24.1');
    expect(parseSnapshot({ ...snapshotData(), artifacts: [{ id: 'qa', label: 'QA', package: 'com.example.qa', available: false }] }).artifacts[0].signers).toBeUndefined();
    for (const broken of [{ ...artifact, signers: 'not-a-list' }, { ...artifact, version: 7 }, { ...artifact, imported_at: 'now' }]) {
      expect(() => parseSnapshot({ ...snapshotData(), artifacts: [broken] })).toThrow();
    }
  });
  it('رویدادهای میزبان را فقط با ساختار معتبر می‌پذیرد', () => {
    const event = { at: 1700000000, kind: 'device-crashed', device: 'num01', detail: 'exit code 137' };
    expect(parseSnapshot({ ...snapshotData(), events: [event, { ...event, device: null, detail: null }] }).events?.length).toBe(2);
    expect(parseSnapshot(snapshotData()).events).toBeUndefined();
    for (const broken of [{ ...event, at: 'now' }, { ...event, kind: 7 }, { ...event, device: 12 }, 'text']) {
      expect(() => parseSnapshot({ ...snapshotData(), events: [broken] })).toThrow();
    }
    expect(() => parseSnapshot({ ...snapshotData(), events: {} })).toThrow();
  });
  it('فیلدهای اندازه‌گیری‌نشده و تنظیمات ناقص را می‌پذیرد', () => {
    const result = parseSnapshot({ ...snapshotData(), devices: [device], errors: [{ component: 'configuration', message: 'unavailable' }] });
    expect(result.devices[0].cpu).toBeNull();
    expect(result.devices[0].phone).toBeNull();
    expect(result.settings).toEqual({});
  });
  it('قابلیت مدیریت رمز و وضعیت سرویس را فقط از پاسخ معتبر میزبان می‌پذیرد', () => {
    const security = { web_username: 'operator', grafana_username: null, credential_rotation_available: true, proxy_credentials_available: false };
    const payload = { ...snapshotData(), settings: { security, central_activation_available: true },
      components: [{ id: 'worker', label: 'عامل اجرا', state: 'inactive', detail: 'not running' }] };
    expect(parseSnapshot(payload).settings.security?.credential_rotation_available).toBe(true);
    expect(parseSnapshot(payload).components?.[0].state).toBe('inactive');
    expect(() => parseSnapshot({ ...payload, settings: { security: { ...security, credential_rotation_available: 'yes' } } })).toThrow();
    expect(() => parseSnapshot({ ...payload, components: [{ id: 'worker', label: 'عامل', state: 'guessed' }] })).toThrow();
  });
  it('پاسخ خراب یا نسخهٔ ناشناخته را به موفقیت تبدیل نمی‌کند', () => {
    for (const override of [{ schema_version: 9 }, { devices: {} }, { csrf_token: '' }, { devices: [{ ...device, containers: null }] }, { jobs: [{ ...job, state: 'magic' }] }]) {
      expect(() => parseSnapshot({ ...snapshotData(), ...override })).toThrow();
    }
  });
  it('آماده‌سازی اولیهٔ فهرست را از خرابی یا آمادگی واقعی جدا می‌کند', () => {
    for (const state of ['setup_required', 'files_required', 'ready', 'invalid']) {
      expect(parseSnapshot({ ...snapshotData(), settings: { application_catalog: { state } } }).settings.application_catalog?.state).toBe(state);
    }
    for (const application_catalog of [null, [], 'ready', { state: 'guessed' }, {}]) {
      expect(() => parseSnapshot({ ...snapshotData(), settings: { application_catalog } })).toThrow();
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
    expect(deviceStatus(device, [{ ...job, action: 'restart', state: 'running' }])).toBe('booting');
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

  it('مرحلهٔ ناتمام را از مرحلهٔ قابل روشن‌شدن جدا می‌کند و حذف دستگاه را می‌شناسد', () => {
    expect(startablePhases).toContain('ready_for_operator');
    expect(startablePhases).not.toContain('failed');
    expect(resumablePhases).toContain('failed');
    for (const phase of ['reserved', 'volume_created', 'secret_installed', 'installing_apk', 'failed']) expect(startablePhases).not.toContain(phase);
    expect(actionText.remove).toBe('حذف دستگاه از فارم');
    const failed = { ...device, phase: 'failed', last_error: 'guarded start failed: identity drift detected' };
    expect(parseSnapshot({ ...snapshotData(), devices: [failed] })?.devices[0].last_error).toBe(failed.last_error);
    expect(() => parseSnapshot({ ...snapshotData(), devices: [{ ...device, last_error: 7 }] })).toThrow();
    expect(eventText['device-removed']).toBe('دستگاه از فارم حذف شد');
  });
  it('زمان توقف ثبت‌شده اختیاری است و فقط ثانیهٔ صحیح و قابل نمایش را می‌پذیرد', () => {
    for (const failed_at of [undefined, null, 0, 1789849223]) {
      expect(parseSnapshot({ ...snapshotData(), devices: [{ ...device, failed_at }] }).devices[0].failed_at).toBe(failed_at);
    }
    for (const failed_at of [-1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1, 8640000000001, '1789849223', true]) {
      expect(() => parseSnapshot({ ...snapshotData(), devices: [{ ...device, failed_at }] })).toThrow();
    }
  });
  it('فرمان عیب‌یابی فقط برای شناسهٔ استاندارد قابل کپی است', () => {
    expect(diagnoseCommand('num03')).toBe('sudo device-provisioner diagnose --id num03');
    expect(diagnoseCommand('num101')).toBe('sudo device-provisioner diagnose --id num101');
    for (const id of ['', 'num3', 'num00', 'num03; shutdown', 'num03\nreboot', '$(reboot)', '../num03']) {
      expect(diagnoseCommand(id)).toBeNull();
    }
  });
});
