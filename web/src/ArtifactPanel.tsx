import { useRef, useState, type FormEvent } from 'react';
import { LoaderCircle, Package, ShieldCheck, Trash2, Upload } from 'lucide-react';
import { ApiError, submitJob, uploadArtifact } from './api';
import { formatTime, type Artifact, type Job, type Snapshot } from './domain';

// مخزن APK: فایل یک بار بارگذاری می‌شود، میزبان آن را با aapt/apksigner بررسی و
// امضاکننده را ثبت می‌کند و همان فایل برای هر آماده‌سازی بعدی استفاده می‌شود.
const grants: [string, string][] = [
  ['android.permission.CAMERA', 'دوربین'],
  ['android.permission.READ_CONTACTS', 'مخاطبین'],
  ['android.permission.RECORD_AUDIO', 'میکروفون'],
];
const message = (error: unknown) => error instanceof Error ? error.message : 'عملیات ناموفق بود.';
export const shortSigner = (artifact: Pick<Artifact, 'signers'>) => artifact.signers?.length ? `${artifact.signers[0].slice(0, 12)}…` : '—';

export default function ArtifactPanel({ snapshot, csrf, canMutate, onJob, onError }: {
  snapshot: Snapshot | null; csrf: string; canMutate: boolean; onJob: (job: Job) => void; onError: (text: string) => void;
}) {
  const [label, setLabel] = useState('');
  const [permissions, setPermissions] = useState<string[]>([]);
  const [allowSignerChange, setAllowSignerChange] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const artifacts = snapshot?.artifacts ?? [];

  async function upload(event: FormEvent) {
    event.preventDefault();
    const file = fileInput.current?.files?.[0];
    if (!file) { setError('ابتدا فایل APK را انتخاب کنید.'); return; }
    if (!/\.apk$/i.test(file.name)) { setError('فقط فایل با پسوند .apk پذیرفته می‌شود.'); return; }
    setBusy(true); setError('');
    try {
      const job = await uploadArtifact(file, csrf, { label, permissions, allowSignerChange });
      onJob(job);
      setLabel(''); setPermissions([]); setAllowSignerChange(false);
      if (fileInput.current) fileInput.current.value = '';
    } catch (failure) {
      const text = failure instanceof ApiError ? failure.message : message(failure);
      setError(text); onError(text);
    } finally { setBusy(false); }
  }

  async function remove(artifact: Artifact) {
    if (!window.confirm(`«${artifact.label}» از مخزن حذف شود؟ دستگاه‌های موجود تغییر نمی‌کنند.`)) return;
    setBusy(true);
    try { onJob(await submitJob({ action: 'artifact-remove', params: { id: artifact.id } }, csrf)); }
    catch (failure) { onError(message(failure)); }
    finally { setBusy(false); }
  }

  return <section className="panel artifact-panel">
    <div className="section-header"><h2><Package size={20} />مخزن APK</h2><span className="tag">{artifacts.length ? `${artifacts.length} برنامه` : 'خالی'}</span></div>
    <p className="field-hint">فایل رسمی برنامه (مثلاً WhatsApp.apk) را یک‌بار بارگذاری کنید. میزبان hash و گواهی امضاکننده را ثبت می‌کند و همان فایل برای همهٔ دستگاه‌های بعدی نصب می‌شود. تغییر امضاکننده بدون تأیید صریح رد می‌شود.</p>
    {artifacts.length > 0 && <div className="table-wrap"><table><thead><tr><th>برنامه</th><th>بسته</th><th>نسخه</th><th>امضاکننده</th><th>ثبت</th><th>وضعیت</th><th /></tr></thead><tbody>
      {artifacts.map(artifact => <tr key={artifact.id}>
        <td>{artifact.label}</td><td dir="ltr">{artifact.package}</td><td dir="ltr">{artifact.version || '—'}</td>
        <td dir="ltr" className="mono">{shortSigner(artifact)}</td>
        <td>{artifact.imported_at ? formatTime(artifact.imported_at) : '—'}</td>
        <td><span className={`status ${artifact.available ? 'running' : 'error'}`}><i />{artifact.available ? 'آمادهٔ نصب' : 'فایل در دسترس نیست'}</span></td>
        <td><button className="icon-button" aria-label={`حذف ${artifact.label}`} title="حذف از مخزن" disabled={!canMutate || busy} onClick={() => void remove(artifact)}><Trash2 size={16} /></button></td>
      </tr>)}
    </tbody></table></div>}
    <form className="device-form" onSubmit={upload}>
      <label>فایل APK<input ref={fileInput} type="file" accept=".apk,application/vnd.android.package-archive" required disabled={!canMutate || busy} /></label>
      <label>نام نمایشی (اختیاری)<input value={label} onChange={event => setLabel(event.target.value)} maxLength={80} placeholder="WhatsApp" disabled={!canMutate || busy} /></label>
      <fieldset className="grant-list"><legend>مجوزهای زمان اجرا که پس از نصب داده شوند</legend>
        {grants.map(([id, title]) => <label key={id} className="checkbox-label"><input type="checkbox" checked={permissions.includes(id)} disabled={!canMutate || busy}
          onChange={event => setPermissions(current => event.target.checked ? [...current, id] : current.filter(item => item !== id))} /><span>{title}</span></label>)}
      </fieldset>
      <label className="checkbox-label"><input type="checkbox" checked={allowSignerChange} disabled={!canMutate || busy} onChange={event => setAllowSignerChange(event.target.checked)} /><span>گواهی امضاکنندهٔ این بسته با منبع ناشر تطبیق داده شد؛ تغییر امضاکننده پذیرفته شود.</span></label>
      <div className="notice compact-notice"><ShieldCheck size={18} /><p>فایل روی میزبان با aapt و apksigner بررسی می‌شود؛ نتیجه در صف عملیات ثبت می‌شود و پس از موفقیت، برنامه در فرم ساخت دستگاه قابل انتخاب است.</p></div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="form-actions"><button className="primary" type="submit" disabled={!canMutate || busy}>{busy ? <LoaderCircle size={17} className="spin" /> : <Upload size={17} />}بارگذاری و ثبت</button></div>
    </form>
  </section>;
}
