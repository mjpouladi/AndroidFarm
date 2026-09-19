import { parseResourceReport, type ResourceReport } from './resources';

export type Device = {
  id: string; phase: string; hold: { reason?: string } | null;
  containers: { android: string; proxy: string; screen: string };
  adb: string | null; screen_path: string; screen_ready: boolean;
  proxy: string | null; proxy_id?: string; expected_egress_ip: string | null;
  phone: string | null; cpu: string | null; memory: string | null;
  running?: boolean;
};
export type ProxyRecord = {
  id: string; label: string; type: 'http' | 'socks' | 'socks5'; server: string; server_port: number;
  expected_egress_ip: string; assigned_device: string | null; state: 'enabled' | 'disabled';
  last_health?: { status: string; checked_at: number; observed_egress_ip: string | null } | null;
};
export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted' | 'cancelled';
export type Job = { id: string; action: string; device: string | null; state: JobState;
  created_at: number; updated_at: number; error: string | null; result: Record<string, unknown> | null };
export type Backup = { id: string; device: string; created_at: number; size_bytes: number };
export type Artifact = { id: string; label: string; package: string; available: boolean };
export type ApplicationCatalog = { state: 'setup_required' | 'files_required' | 'ready' | 'invalid' };
export type SecuritySettings = { web_username: string | null; grafana_username: string | null;
  credential_rotation_available: boolean; proxy_credentials_available: boolean };
export type ComponentHealth = { id: string; label: string; state: 'active' | 'inactive' | 'failed' | 'unknown'; detail?: string };
export type Snapshot = { schema_version: 1; collected_at: number; csrf_token: string; resources: ResourceReport | null;
  devices: Device[]; proxies: ProxyRecord[]; backups: Backup[]; artifacts: Artifact[];
  errors: { component: string; message: string }[]; jobs: Job[];
  settings: { console_url?: string | null; access_mode?: string | null; security?: SecuritySettings; central_activation_available?: boolean; application_catalog?: ApplicationCatalog };
  components?: ComponentHealth[]; queue?: Record<string, unknown> };
export type DeviceStatus = 'running' | 'off' | 'booting' | 'queued' | 'stopping' | 'backup' | 'error' | 'unknown';
export const fa = (value: number) => new Intl.NumberFormat('fa-IR', { maximumFractionDigits: 1 }).format(value);
export const statusText: Record<DeviceStatus, string> = { running: 'روشن', off: 'خاموش', booting: 'در حال راه‌اندازی',
  queued: 'در صف', stopping: 'در حال توقف', backup: 'پشتیبان‌گیری', error: 'نیاز به بررسی', unknown: 'نامشخص' };
export const jobStateText: Record<JobState, string> = { queued: 'در صف', running: 'در حال اجرا', succeeded: 'موفق',
  failed: 'ناموفق', interrupted: 'متوقف‌شده پس از وقفه', cancelled: 'لغوشده' };
export const actionText: Record<string, string> = { up: 'روشن‌کردن', down: 'خاموش‌کردن', check: 'بررسی ADB',
  'check-ip': 'بررسی IP خروجی', backup: 'پشتیبان‌گیری', provision: 'آماده‌سازی دستگاه',
  'proxy-add': 'ثبت پراکسی', 'proxy-test': 'تست پراکسی', 'proxy-enable': 'فعال‌کردن پراکسی', 'proxy-disable': 'غیرفعال‌کردن پراکسی',
  'credential-rotate': 'تغییر اطلاعات ورود', 'proxy-credentials': 'تغییر رمز پراکسی', 'core-activate': 'راه‌اندازی سرویس‌های مرکزی', release: 'رفع توقف حفاظتی' };
export const runningContainer = (state: string) => ['running', 'healthy', 'starting', 'unhealthy', 'paused', 'restarting'].includes(state);
export const activeDevices = (devices: Device[]) => devices.filter(d => d.running ?? runningContainer(d.containers.android)).length;
export const activeJob = (job: Job) => job.state === 'queued' || job.state === 'running';
export const formatTime = (stamp: number) => new Date(stamp * 1000).toLocaleString('fa-IR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
export const formatSize = (bytes: number) => bytes >= 1024 ** 3 ? `${fa(bytes / 1024 ** 3)} GB` : `${fa(bytes / 1024 ** 2)} MB`;
export const provisionableProxies = (proxies: ProxyRecord[], devices: Device[]) => proxies.filter(proxy => {
  if (proxy.state !== 'enabled') return false;
  if (!proxy.assigned_device) return true;
  const device = devices.find(item => item.id === proxy.assigned_device);
  return !!device && ['reserved', 'volume_created', 'secret_installed', 'identity_baselining', 'starting', 'installing_apk', 'failed'].includes(device.phase);
});

export function deviceStatus(device: Device, jobs: Job[]): DeviceStatus {
  const job = jobs.find(j => j.device === device.id && activeJob(j));
  if (job?.state === 'running') {
    if (job.action === 'down') return 'stopping';
    if (job.action === 'backup') return 'backup';
    if (job.action === 'up' || job.action === 'provision') return 'booting';
  }
  if (device.hold || Object.values(device.containers).includes('unhealthy')) return 'error';
  if (device.containers.android === 'paused') return 'error';
  if (device.containers.android === 'restarting') return 'booting';
  if (runningContainer(device.containers.android)) return 'running';
  if (job?.state === 'queued') return 'queued';
  if (device.phase === 'failed') return 'error';
  if (['stopped', 'missing', 'exited', 'created'].includes(device.containers.android)) return 'off';
  return 'unknown';
}

// Only canonical, same-origin device paths may be embedded in the console.
export function screenPath(device: Pick<Device, 'id' | 'screen_path'>): string | null {
  return /^num(?:0[1-9]|[1-9]\d+)$/.test(device.id) && device.screen_path === `/d/${device.id}/` ? device.screen_path : null;
}
export const isSnapshotFresh = (snapshot: Snapshot | null, now = Date.now()) => !!snapshot &&
  now / 1000 - snapshot.collected_at <= 20 && snapshot.collected_at - now / 1000 <= 5;

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const text = (value: unknown): value is string => typeof value === 'string';
const nullableText = (value: unknown) => value === null || text(value);
const jobStates = new Set(['queued', 'running', 'succeeded', 'failed', 'interrupted', 'cancelled']);
export function parseJob(value: unknown): Job {
  if (!object(value) || !text(value.id) || !text(value.action) || !nullableText(value.device) || !jobStates.has(String(value.state)) ||
      !finite(value.created_at) || !finite(value.updated_at) || !nullableText(value.error) || !(value.result === null || object(value.result))) {
    throw new Error('پاسخ وضعیت عملیات نامعتبر است.');
  }
  return value as Job;
}
export function parseSnapshot(value: unknown): Snapshot {
  const invalid = () => { throw new Error('پاسخ API با نسخهٔ کنسول سازگار نیست؛ استقرار هسته را بررسی کنید.'); };
  if (!object(value) || value.schema_version !== 1 || !finite(value.collected_at) || !text(value.csrf_token) || !value.csrf_token ||
      !object(value.settings) || !(value.settings.console_url === undefined || nullableText(value.settings.console_url)) ||
      !(value.settings.access_mode === undefined || nullableText(value.settings.access_mode))) return invalid();
  for (const key of ['devices', 'proxies', 'backups', 'artifacts', 'errors', 'jobs']) if (!Array.isArray(value[key])) return invalid();
  if (value.settings.central_activation_available !== undefined && typeof value.settings.central_activation_available !== 'boolean') return invalid();
  if (value.settings.application_catalog !== undefined && (!object(value.settings.application_catalog) ||
      !['setup_required', 'files_required', 'ready', 'invalid'].includes(String(value.settings.application_catalog.state)))) return invalid();
  if (value.settings.security !== undefined) {
    const security = value.settings.security;
    if (!object(security) || !nullableText(security.web_username) || !nullableText(security.grafana_username) ||
        typeof security.credential_rotation_available !== 'boolean' || typeof security.proxy_credentials_available !== 'boolean') return invalid();
  }
  if (value.components !== undefined) {
    if (!Array.isArray(value.components)) return invalid();
    for (const item of value.components) if (!object(item) || !text(item.id) || !text(item.label) ||
      !['active', 'inactive', 'failed', 'unknown'].includes(String(item.state)) || !(item.detail === undefined || text(item.detail))) return invalid();
  }
  for (const item of value.devices as unknown[]) {
    if (!object(item) || !text(item.id) || !text(item.phase) || !(item.hold === null || object(item.hold)) || !object(item.containers) ||
        !['android', 'proxy', 'screen'].every(key => text((item.containers as Record<string, unknown>)[key])) ||
        !nullableText(item.adb) || !text(item.screen_path) || typeof item.screen_ready !== 'boolean' || !nullableText(item.proxy) ||
        !nullableText(item.expected_egress_ip) || !nullableText(item.phone) || !nullableText(item.cpu) || !nullableText(item.memory)) return invalid();
  }
  for (const item of value.proxies as unknown[]) {
    if (!object(item) || !['id', 'label', 'server', 'expected_egress_ip'].every(key => text(item[key])) || !finite(item.server_port) ||
        !['http', 'socks', 'socks5'].includes(String(item.type)) || !['enabled', 'disabled'].includes(String(item.state)) || !nullableText(item.assigned_device)) return invalid();
    if (item.last_health != null && (!object(item.last_health) || !text(item.last_health.status) ||
        !finite(item.last_health.checked_at) || !nullableText(item.last_health.observed_egress_ip))) return invalid();
  }
  for (const item of value.backups as unknown[]) if (!object(item) || !text(item.id) || !text(item.device) || !finite(item.created_at) || !finite(item.size_bytes)) return invalid();
  for (const item of value.artifacts as unknown[]) if (!object(item) || !text(item.id) || !text(item.label) || !text(item.package) || typeof item.available !== 'boolean') return invalid();
  for (const item of value.errors as unknown[]) if (!object(item) || !text(item.component) || !text(item.message)) return invalid();
  (value.jobs as unknown[]).forEach(parseJob);
  return { ...value, resources: value.resources === null ? null : parseResourceReport(value.resources) } as Snapshot;
}
