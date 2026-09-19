export type CredentialTarget = 'platform' | 'web' | 'grafana';
export type CredentialForm = { target: CredentialTarget; username: string; password: string; confirmation: string;
  current_password: string; acknowledged: boolean };
export type ProxyCredentialForm = { id: string; password: string; current_password: string; acknowledged: boolean };

export function credentialRequest(form: CredentialForm) {
  if (!['platform', 'web', 'grafana'].includes(form.target)) throw new Error('بخش موردنظر را انتخاب کنید.');
  if (!/^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$/.test(form.username)) throw new Error('نام کاربری باید ۱ تا ۶۴ نویسهٔ لاتین، عدد، نقطه، زیرخط یا خط تیره باشد و با خط تیره شروع نشود.');
  const bytes = new TextEncoder().encode(form.password).length;
  if (bytes < 12 || bytes > 72 || form.password !== form.password.trim() || /[\u0000-\u001f\u007f]/.test(form.password)) throw new Error('رمز جدید باید ۱۲ تا ۷۲ بایت، بدون نویسهٔ کنترلی و بدون فاصله در ابتدا یا انتها باشد؛ حروف فارسی چندبایتی هستند.');
  if (form.password !== form.confirmation) throw new Error('تکرار رمز با رمز جدید یکسان نیست.');
  if (!form.current_password) throw new Error('رمز فعلی ورود به فارم برای تأیید هویت لازم است.');
  if (!form.acknowledged) throw new Error('پیام ورود مجدد و محدودهٔ تغییر را تأیید کنید.');
  return { action: 'credential-rotate', params: { target: form.target, username: form.username,
    password: form.password, current_password: form.current_password } };
}

export function proxyCredentialRequest(form: ProxyCredentialForm) {
  if (!form.id || !form.password) throw new Error('پراکسی و رمز جدید آن را وارد کنید.');
  if (new TextEncoder().encode(form.password).length > 4096 || form.password !== form.password.trim() || /[\u0000-\u001f\u007f]/.test(form.password)) throw new Error('رمز پراکسی باید حداکثر ۴۰۹۶ بایت، بدون نویسهٔ کنترلی و بدون فاصله در ابتدا یا انتها باشد.');
  if (!form.current_password) throw new Error('رمز فعلی ورود به فارم برای تأیید هویت لازم است.');
  if (!form.acknowledged) throw new Error('توقف دستگاه وابسته برای تغییر امن اتصال را تأیید کنید.');
  return { action: 'proxy-credentials', params: { id: form.id, password: form.password, current_password: form.current_password } };
}
