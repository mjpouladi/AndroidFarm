import { useState } from 'react';
import { BookOpen, Check, Copy, RefreshCw, Server } from 'lucide-react';

const installCommand = 'sudo bash /opt/android-farm/source/install.sh';
const guide = 'https://github.com/mjpouladi/AndroidFarm/blob/main/docs/GUIDE.fa.md';

export default function ConnectionHelp({ status, retry }: { status: number | null; retry: () => void }) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  async function copy() {
    try { await navigator.clipboard.writeText(installCommand); setCopied(true); setCopyFailed(false); }
    catch { setCopied(false); setCopyFailed(true); }
  }
  if (status === 401 || status === 403) return <section className="connection-help"><LockNotice /><p>ورود با حساب فارم را در مرورگر تکمیل کنید. پس از تغییر نام کاربری یا رمز، اطلاعات قبلی مرورگر دیگر معتبر نیست.</p><button className="secondary small" onClick={() => window.location.reload()}><RefreshCw size={15} />ورود مجدد و تازه‌سازی</button></section>;
  return <section className="connection-help"><div className="inline"><Server size={18} /><strong>راه‌اندازی اتصال واقعی میزبان</strong></div><p>اگر این رابط را بدون سرویس میزبان باز کرده‌اید، ابزارها به دستگاهی دسترسی ندارند. روی سرور فارم فرمان زیر را اجرا کنید؛ نصب‌کننده تنظیمات موجود را بازیابی و سرویس کنترل را آماده می‌کند.</p><div className="command-copy"><code dir="ltr">{installCommand}</code><button className="secondary small" onClick={() => void copy()}>{copied ? <Check size={15} /> : <Copy size={15} />}{copied ? 'کپی شد' : 'کپی فرمان'}</button></div>{copyFailed && <p className="field-hint">مرورگر اجازهٔ کپی نداد؛ فرمان نمایش‌داده‌شده را دستی کپی کنید.</p>}<p>پس از پایان نصب، همین صفحه را تازه‌سازی کنید. فایل ENV، رمزها و توکن‌ها را در گزارش خطا ارسال نکنید.</p><div className="inline"><button className="secondary small" onClick={retry}><RefreshCw size={15} />بررسی دوبارهٔ اتصال</button><a className="secondary small inline" href={guide} target="_blank" rel="noreferrer"><BookOpen size={15} />راهنمای نصب و رفع خطا</a></div></section>;
}
function LockNotice() { return <strong>ابتدا احراز هویت را بازیابی کنید</strong>; }
