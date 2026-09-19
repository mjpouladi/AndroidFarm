export type DeviceStatus = 'running' | 'off' | 'booting' | 'queued' | 'stopping' | 'backup' | 'error';
export type Device = { id: string; label: string; phone: string; status: DeviceStatus; region: string;
  proxyHealthy: boolean; cpu: number; ram: number; latency: number; backup: string | null; queueOrder?: number; };
export type FarmEvent = { id: string; device: string; text: string; at: number; kind: 'success' | 'info' | 'warning' };
export type Backup = { id: string; device: string; at: number; size: string };
export type FarmState = { devices: Device[]; events: FarmEvent[]; backups: Backup[]; message: string; capacity: number };
export type Action = { type: 'start' | 'stop' | 'cancel' | 'backup' | 'tick' | 'proxy' | 'reset'; id?: string; now?: number }
  | { type: 'add'; label: string; phone: string; now?: number }
  | { type: 'capacity'; capacity: number; now?: number };
export const fa = (value: number) => new Intl.NumberFormat('fa-IR').format(value);
export const reserved = (devices: Device[]) => devices.filter(d => ['running', 'booting', 'stopping'].includes(d.status)).length;
export const maskPhone = (phone: string) => `${phone.slice(0, 3)} ••• ••• ${phone.slice(-4)}`;
export const statusText: Record<DeviceStatus, string> = { running: 'آماده به کار', off: 'خاموش', booting: 'در حال راه‌اندازی',
  queued: 'در صف', stopping: 'در حال توقف', backup: 'پشتیبان‌گیری', error: 'نیاز به بررسی' };

export function initialState(): FarmState {
  return { devices: [], events: [], backups: [], message: '', capacity: 1 };
}

// Explicit demonstration fixture, never used as the initial fleet.
export function demoState(): FarmState {
  return { capacity: 10, devices: Array.from({ length: 70 }, (_, index) => {
    const n = index + 1;
    return { id: `num${String(n).padStart(2, '0')}`, label: n <= 12 ? ['رجیستری تهران', 'ورود مجدد', 'تست نشست', 'رجیستری جدید', 'عملیات تیم اول', 'کنترل کیفیت', 'ذخیره', 'رجیستری شیراز', 'ذخیره', 'تست اتصال', 'عملیات تیم دوم', 'ذخیره'][index] : `دستگاه ${fa(n)}`,
      phone: `+98900000${String(4100 + n)}`, status: [1, 2, 3, 5, 6, 8, 11].includes(n) ? 'running' : [4, 19].includes(n) ? 'error' : 'off',
      region: ['آلمان', 'هلند', 'آلمان', 'فنلاند'][index % 4], proxyHealthy: ![4, 19].includes(n),
      cpu: 12 + n % 19, ram: +(1.2 + (n % 12) / 10).toFixed(1), latency: 82 + (n * 7) % 65,
      backup: n % 4 ? 'امروز، ۰۳:۰۰' : null };
  }), events: [
    { id: 'e1', device: 'num11', text: 'دستگاه آمادهٔ شروع جلسه است', at: Date.now() - 60000, kind: 'success' },
    { id: 'e2', device: 'num04', text: 'اتصال پراکسی نیاز به بررسی دارد', at: Date.now() - 180000, kind: 'warning' },
    { id: 'e3', device: 'num03', text: 'پشتیبان داده‌ها ثبت شد', at: Date.now() - 600000, kind: 'info' },
  ], backups: [1, 2, 3, 5, 6, 7].map(n => ({ id: `b${n}`, device: `num0${n}`, at: Date.now() - n * 3600000, size: `${(1.1 + n / 10).toFixed(1)} GB` })), message: '' };
}

export function reducer(state: FarmState, action: Action): FarmState {
  const now = action.now ?? Date.now();
  if (action.type === 'reset') return { ...initialState(), capacity: state.capacity };
  if (action.type === 'capacity') {
    if (!Number.isInteger(action.capacity) || action.capacity < 0) return { ...state, message: 'ظرفیت میزبان نامعتبر است.' };
    return { ...state, capacity: action.capacity, message: 'ظرفیت امن میزبان به‌روزرسانی شد.' };
  }
  let devices = state.devices.map(d => ({ ...d }));
  let events = [...state.events];
  let backups = [...state.backups];
  let message = '';
  const event = (device: string, text: string, kind: FarmEvent['kind'] = 'info') => {
    events.unshift({ id: `${now}-${events.length}`, device, text, at: now, kind });
    message = text;
  };
  if (action.type === 'add') {
    if (!/^\+\d{10,15}$/.test(action.phone)) return { ...state, message: 'شماره را با + و کد کشور وارد کنید.' };
    if (devices.some(d => d.phone === action.phone)) return { ...state, message: 'این شماره قبلاً به یک دستگاه اختصاص یافته است.' };
    const n = devices.length + 1;
    devices.push({ id: `num${String(n).padStart(2, '0')}`, label: action.label.trim() || `دستگاه ${fa(n)}`,
      phone: action.phone, status: 'off', region: 'آلمان', proxyHealthy: true, cpu: 0, ram: 0,
      latency: 95, backup: null });
    event(devices.at(-1)!.id, 'دستگاه آزمایشی به فارم اضافه شد', 'success');
  } else if (action.type === 'tick') {
    for (const d of devices) {
      if (d.status === 'booting') { d.status = 'running'; d.cpu = 18; d.ram = 1.8; event(d.id, 'دستگاه آماده به کار است', 'success'); }
      else if (d.status === 'stopping') { d.status = 'off'; event(d.id, 'جلسه پایان یافت؛ داده‌ها حفظ شدند', 'success'); }
      else if (d.status === 'backup') {
        d.status = 'off'; d.backup = 'همین حالا';
        backups.unshift({ id: `b-${now}-${d.id}`, device: d.id, at: now, size: '1.4 GB' });
        event(d.id, 'پشتیبان آزمایشی ثبت شد', 'success');
      }
    }
    for (const d of devices.filter(d => d.status === 'queued').sort((a, b) => (a.queueOrder ?? 0) - (b.queueOrder ?? 0))) if (reserved(devices) < state.capacity && d.proxyHealthy) {
      d.status = 'booting'; event(d.id, 'جایگاه آزاد شد؛ راه‌اندازی آغاز شد');
    }
    if (!message) return state;
  } else {
    const d = devices.find(d => d.id === action.id);
    if (!d) return state;
    if (action.type === 'start' && ['off', 'error'].includes(d.status)) {
      if (!d.proxyHealthy) return { ...state, message: 'ابتدا اتصال پراکسی این دستگاه را بررسی کنید.' };
      d.status = reserved(devices) >= state.capacity ? 'queued' : 'booting';
      if (d.status === 'queued') d.queueOrder = Math.max(0, ...devices.map(item => item.queueOrder ?? 0)) + 1;
      event(d.id, d.status === 'queued' ? 'ظرفیت تکمیل است؛ دستگاه به صف اضافه شد' : 'راه‌اندازی دستگاه آغاز شد');
    } else if (action.type === 'stop' && ['running', 'booting'].includes(d.status)) {
      d.status = 'stopping'; event(d.id, 'پایان امن جلسه آغاز شد');
    } else if (action.type === 'cancel' && d.status === 'queued') {
      d.status = 'off'; event(d.id, 'درخواست از صف خارج شد');
    } else if (action.type === 'backup') {
      if (d.status !== 'off') return { ...state, message: 'برای بکاپ سازگار، ابتدا دستگاه را خاموش کنید.' };
      d.status = 'backup'; event(d.id, 'پشتیبان‌گیری آزمایشی آغاز شد');
    } else if (action.type === 'proxy') {
      d.proxyHealthy = true;
      if (d.status === 'error') d.status = 'off';
      event(d.id, 'آزمون شبیه‌سازی‌شدهٔ پراکسی موفق بود', 'success');
    }
  }
  return { ...state, devices, events: events.slice(0, 100), backups, message };
}
