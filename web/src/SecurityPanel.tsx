import { useEffect, useState, type FormEvent } from 'react';
import { KeyRound, LockKeyhole, Network, RefreshCw, ShieldCheck } from 'lucide-react';
import { credentialRequest, proxyCredentialRequest, type CredentialForm, type CredentialTarget, type ProxyCredentialForm } from './credentials';
import type { ProxyRecord, SecuritySettings } from './domain';

type Props = { settings?: SecuritySettings; proxies: ProxyRecord[]; canMutate: boolean;
  operationError?: string;
  initialProxyId?: string; onSubmit: (action: string, device?: string, params?: Record<string, unknown>) => Promise<boolean>;
  onJobs: () => void };
const freshCredentials = (): CredentialForm => ({ target: 'platform', username: 'mjpouladi', password: '', confirmation: '', current_password: '', acknowledged: false });

export default function SecurityPanel({ settings, proxies, canMutate, initialProxyId, onSubmit, onJobs, operationError }: Props) {
  const [form, setForm] = useState(freshCredentials);
  const [proxyForm, setProxyForm] = useState<ProxyCredentialForm>({ id: initialProxyId ?? '', password: '', current_password: '', acknowledged: false });
  const [error, setError] = useState('');
  const [remoteError, setRemoteError] = useState(false);
  const [proxyError, setProxyError] = useState('');
  const [remoteProxyError, setRemoteProxyError] = useState(false);
  const [rotationQueued, setRotationQueued] = useState<CredentialTarget | null>(null);
  const [proxyQueued, setProxyQueued] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => { if (initialProxyId) setProxyForm(current => ({ ...current, id: initialProxyId })); }, [initialProxyId]);
  const rotationAvailable = settings?.credential_rotation_available === true;
  const proxyAvailable = settings?.proxy_credentials_available === true;
  const selected = proxies.find(proxy => proxy.id === proxyForm.id);
  const allowRotation = canMutate && rotationAvailable && !submitting;
  const allowProxy = canMutate && proxyAvailable && !submitting;

  async function rotate(event: FormEvent) {
    event.preventDefault(); if (!allowRotation) return;
    setError(''); setRemoteError(false); setRotationQueued(null);
    let request: ReturnType<typeof credentialRequest>;
    try { request = credentialRequest(form); }
    catch (failure) { setError(failure instanceof Error ? failure.message : 'اطلاعات معتبر نیست.'); return; }
    setSubmitting(true);
    const result = onSubmit(request.action, undefined, request.params);
    setForm(current => ({ ...current, password: '', confirmation: '', current_password: '', acknowledged: false }));
    try { if (await result) setRotationQueued(request.params.target); else { setRemoteError(true); setError('درخواست ثبت نشد؛ پیام خطا را بررسی کنید و رمزها را دوباره وارد کنید.'); } }
    finally { setSubmitting(false); }
  }
  async function updateProxy(event: FormEvent) {
    event.preventDefault(); if (!allowProxy || !selected) return;
    setProxyError(''); setRemoteProxyError(false); setProxyQueued(false);
    let request: ReturnType<typeof proxyCredentialRequest>;
    try { request = proxyCredentialRequest(proxyForm); }
    catch (failure) { setProxyError(failure instanceof Error ? failure.message : 'اطلاعات معتبر نیست.'); return; }
    setSubmitting(true);
    const result = onSubmit(request.action, undefined, request.params);
    setProxyForm(current => ({ ...current, password: '', current_password: '', acknowledged: false }));
    try { if (await result) setProxyQueued(true); else { setRemoteProxyError(true); setProxyError('درخواست ثبت نشد؛ پیام خطا را بررسی کنید و اطلاعات ورود را دوباره وارد کنید.'); } }
    finally { setSubmitting(false); }
  }

  return <section className="security-section"><div className="section-header"><div className="section-title"><LockKeyhole size={21} /><h2>مدیریت مرکزی اطلاعات ورود</h2></div><span className="tag">رمزهای فعلی نمایش داده نمی‌شوند</span></div>
    <div className="notice"><ShieldCheck size={22} /><div><strong>محدودهٔ مدیریت: ورود فارم، Grafana و اتصال‌های پراکسی</strong><p>رمز SSH سرور، حساب Coolify و حساب سرویس‌دهندهٔ پراکسی در این بخش تغییر نمی‌کنند. اطلاعات جدید فقط به سرویس میزبان ارسال می‌شوند و در تاریخچهٔ عملیات یا حافظهٔ ماندگار کنسول ثبت نمی‌شوند.</p></div></div>
    <div className="security-grid"><section className="panel"><div className="section-header"><h2><KeyRound size={19} /> ورود فارم و مانیتورینگ</h2></div><dl className="security-accounts"><div><dt>کاربر فعلی فارم</dt><dd dir="ltr">{settings?.web_username ?? '—'}</dd></div><div><dt>کاربر فعلی Grafana</dt><dd dir="ltr">{settings?.grafana_username ?? '—'}</dd></div></dl>
      {!rotationAvailable && <p className="security-unavailable">مدیریت رمز هنوز توسط سرویس میزبان آماده گزارش نشده است. اتصال API و آخرین نصب را بررسی کنید.</p>}
      <form className="device-form security-form" autoComplete="off" onSubmit={rotate}><fieldset disabled={!allowRotation}><label>محدودهٔ تغییر<select value={form.target} onChange={event => setForm({ ...form, target: event.target.value as CredentialTarget })}><option value="platform">فارم و Grafana با یک حساب</option><option value="web">فقط ورود فارم</option><option value="grafana">فقط ورود Grafana</option></select></label><label>نام کاربری جدید<input dir="ltr" autoComplete="username" value={form.username} onChange={event => setForm({ ...form, username: event.target.value })} maxLength={64} required /></label><label>رمز فعلی ورود فارم<input type="password" dir="ltr" autoComplete="current-password" value={form.current_password} onChange={event => setForm({ ...form, current_password: event.target.value })} required /></label><label>رمز جدید<input type="password" dir="ltr" autoComplete="new-password" value={form.password} onChange={event => setForm({ ...form, password: event.target.value })} required /></label><label>تکرار رمز جدید<input type="password" dir="ltr" autoComplete="new-password" value={form.confirmation} onChange={event => setForm({ ...form, confirmation: event.target.value })} required /></label><p className="field-hint">۱۲ تا ۷۲ بایت؛ برای تأیید هر تغییر، رمز فعلی فارم لازم است.</p><label className="checkbox-label"><input type="checkbox" checked={form.acknowledged} onChange={event => setForm({ ...form, acknowledged: event.target.checked })} required /><span>محدودهٔ تغییر را بررسی کرده‌ام و می‌دانم پس از تغییر ورود فارم، باید با حساب جدید وارد شوم.</span></label><button className="primary" type="submit"><KeyRound size={16} />ثبت تغییر اطلاعات ورود</button></fieldset>{error && <p className="form-error" role="alert">{remoteError && operationError ? operationError : error}</p>}</form>
      {rotationQueued && <div className="security-result" role="status"><strong>درخواست تغییر ثبت شد؛ نتیجه هنوز باید بررسی شود.</strong><p>{rotationQueued === 'grafana' ? 'پس از موفقیت عملیات، با حساب جدید در Grafana وارد شوید.' : 'نتیجه را در صف دنبال کنید. اگر مرورگر ورود مجدد خواست، نام کاربری و رمز جدید را وارد کنید. دریافت پاسخ خطای ورود، به‌تنهایی تأیید موفقیت عملیات نیست.'}</p><div className="inline"><button className="secondary small" onClick={onJobs}>پیگیری در صف</button>{rotationQueued !== 'grafana' && <button className="secondary small" onClick={() => window.location.reload()}><RefreshCw size={14} />ورود مجدد پس از تغییر</button>}</div></div>}
    </section><section className="panel"><div className="section-header"><h2><Network size={19} /> رمز اتصال پراکسی</h2></div><p className="diagnostics-note">رمز معتبر فعلی سرویس‌دهنده را اینجا ثبت کنید. حساب شما نزد سرویس‌دهنده تغییر نمی‌کند؛ نام کاربری حاوی شناسهٔ اتصال پایدار، نشانی و IP خروجی حفظ می‌شوند.</p>{!proxyAvailable && <p className="security-unavailable">قابلیت تغییر رمز پراکسی هنوز توسط میزبان آماده گزارش نشده است.</p>}
      <form className="device-form security-form" autoComplete="off" onSubmit={updateProxy}><fieldset disabled={!allowProxy}><label>اتصال موردنظر<select value={proxyForm.id} onChange={event => setProxyForm({ ...proxyForm, id: event.target.value })} required><option value="">انتخاب پراکسی</option>{proxies.map(proxy => <option key={proxy.id} value={proxy.id}>{proxy.label} — {proxy.assigned_device ?? 'آزاد'}</option>)}</select></label>{selected && <p className="security-proxy-endpoint" dir="ltr">{selected.server}:{selected.server_port}</p>}<label>رمز جدید پراکسی<input type="password" dir="ltr" autoComplete="new-password" value={proxyForm.password} onChange={event => setProxyForm({ ...proxyForm, password: event.target.value })} required /></label><label>رمز فعلی ورود فارم<input type="password" dir="ltr" autoComplete="current-password" value={proxyForm.current_password} onChange={event => setProxyForm({ ...proxyForm, current_password: event.target.value })} required /></label><label className="checkbox-label"><input type="checkbox" checked={proxyForm.acknowledged} onChange={event => setProxyForm({ ...proxyForm, acknowledged: event.target.checked })} required /><span>{selected?.assigned_device ? `توقف دستگاه ${selected.assigned_device} برای تغییر امن اتصال را تأیید می‌کنم.` : 'می‌دانم اگر اتصال به دستگاهی تخصیص یافته باشد، آن دستگاه برای تغییر امن متوقف می‌شود.'}</span></label><button className="primary" type="submit" disabled={!selected}><Network size={16} />ثبت رمز اتصال</button></fieldset>{proxyError && <p className="form-error" role="alert">{remoteProxyError && operationError ? operationError : proxyError}</p>}</form>
      {proxyQueued && <div className="security-result" role="status"><strong>درخواست تغییر اتصال ثبت شد.</strong><p>دستگاه متوقف می‌ماند؛ پس از موفقیت و بررسی اتصال، آن را روشن کنید. توقف حفاظتی قبلی، اگر وجود داشته باشد، همچنان باید بررسی شود.</p><button className="secondary small" onClick={onJobs}>پیگیری در صف</button></div>}
    </section></div></section>;
}
