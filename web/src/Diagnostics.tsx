import { Activity, Check, CircleHelp, Power } from 'lucide-react';
import { formatTime, type Snapshot } from './domain';

const sources = [
  ['configuration', 'تنظیمات میزبان'], ['resources', 'منابع و ظرفیت'], ['inventory', 'موجودی دستگاه‌ها'],
  ['docker', 'وضعیت کانتینرها'], ['proxies', 'رجیستری پراکسی'], ['artifacts', 'برنامه‌های مورد اعتماد'],
  ['backups', 'فهرست پشتیبان'], ['holds', 'توقف‌های حفاظتی'], ['queue', 'صف عملیات'],
] as const;

export default function Diagnostics({ snapshot, fresh, canMutate, onActivate }: {
  snapshot: Snapshot | null; fresh: boolean; canMutate: boolean; onActivate: () => void;
}) {
  const configFailed = snapshot?.errors.some(error => error.component === 'configuration');
  const stateText = { active: 'فعال', inactive: 'غیرفعال', failed: 'ناموفق', unknown: 'نامشخص' };
  return <section className="panel diagnostics-panel"><div className="section-header"><h2>وضعیت سرویس‌ها و ابزارها</h2><Activity size={20} /></div>
    <div className="central-activation"><div><h3>سرویس‌های مرکزی نصب‌شده</h3><p>این فرمان سرویس‌های مدیریت‌شدهٔ موجود را راه‌اندازی می‌کند؛ دستگاه اندروید نمی‌سازد، آن را روشن نمی‌کند و توقف حفاظتی را تغییر نمی‌دهد.</p></div><button className="primary" disabled={!canMutate || snapshot?.settings.central_activation_available !== true} onClick={onActivate}><Power size={16} />راه‌اندازی سرویس‌های مرکزی</button></div>
    {snapshot?.components?.length ? <div className="diagnostics-grid component-grid">{snapshot.components.map(component => <div className={`diagnostic-item ${fresh && component.state === 'active' ? 'available' : 'unavailable'}`} key={component.id}><span>{fresh && component.state === 'active' ? <Check size={16} /> : <CircleHelp size={16} />}</span><div><strong>{component.label}</strong><p>{fresh ? stateText[component.state] : 'وضعیت قدیمی؛ اتصال تازه لازم است'}</p>{component.detail && <p>{component.detail}</p>}</div></div>)}</div> : <p className="diagnostics-note">وضعیت سرویس‌های مرکزی هنوز از میزبان دریافت نشده است؛ اتصال API و نسخهٔ نصب را بررسی کنید.</p>}
    <p className="diagnostics-note">«فعال» وضعیت اجرای سرویس است و به‌تنهایی سلامت کامل را تضمین نمی‌کند. سلامت ADB و IP هر دستگاه را از جزئیات همان دستگاه آزمایش کنید.</p><h3 className="diagnostics-subtitle">دسترسی به منابع داده</h3><div className="diagnostics-grid">{sources.map(([id, title]) => {
    const failure = snapshot?.errors.find(error => error.component === id);
    const catalog = id === 'artifacts' ? snapshot?.settings.application_catalog?.state : undefined;
    const setupRequired = fresh && !configFailed && !failure && (catalog === 'setup_required' || catalog === 'files_required');
    const available = fresh && !!snapshot && !configFailed && !failure && !setupRequired && (id !== 'resources' || !!snapshot.resources);
    return <div className={`diagnostic-item ${available ? 'available' : 'unavailable'}`} key={id}><span>{available ? <Check size={16} /> : <CircleHelp size={16} />}</span><div><strong>{title}</strong><p>{setupRequired ? catalog === 'setup_required' ? 'نیاز به ثبت برنامه؛ اختلال سرویس نیست' : 'فایل APK روی میزبان آماده نیست' : available ? 'گزارش دریافت شد' : failure ? failure.message : !fresh ? 'در انتظار اتصال تازه' : 'گزارش قابل اتکا موجود نیست'}</p></div></div>;
  })}</div>{snapshot && <p className="diagnostics-note">آخرین دریافت: {formatTime(snapshot.collected_at)}</p>}</section>;
}
