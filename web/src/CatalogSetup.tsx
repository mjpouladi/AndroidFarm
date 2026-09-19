import { BookOpen } from 'lucide-react';
import type { Snapshot } from './domain';

export const applicationGuide = 'https://github.com/mjpouladi/AndroidFarm/blob/main/docs/GUIDE.fa.md#approved-app-catalog';

export default function CatalogSetup({ snapshot, fresh }: { snapshot: Snapshot | null; fresh: boolean }) {
  const state = snapshot?.settings.application_catalog?.state;
  if (!fresh || (state !== 'setup_required' && state !== 'files_required')) return null;
  return <div className="notice catalog-setup" role="status"><BookOpen size={21} /><div>
    <strong>{state === 'setup_required' ? 'فهرست برنامه‌ها نیاز به آماده‌سازی دارد' : 'فایل برنامه‌های ثبت‌شده آمادهٔ نصب نیست'}</strong>
    <p>{state === 'setup_required'
      ? 'هنوز APK تأییدشده‌ای ثبت نشده است. این وضعیت در نصب تازه طبیعی است؛ مدیریت فارم و دستگاه‌های موجود همچنان در دسترس‌اند.'
      : 'فهرست برنامه‌ها خوانده شد، اما فایل APK قابل استفاده‌ای روی میزبان پیدا نشد؛ مسیر فایل و مجوز دسترسی را بررسی کنید.'}</p>
    <p>پیش از آماده‌سازی دستگاه جدید، فایل APK، مشخصات نسخه و امضاکنندهٔ مورد اعتماد را طبق بخش ۹ راهنمای واحد ثبت کنید.</p>
    <a className="inline" href={applicationGuide} target="_blank" rel="noreferrer">راهنمای مرحله‌به‌مرحلهٔ ثبت برنامه</a>
  </div></div>;
}
