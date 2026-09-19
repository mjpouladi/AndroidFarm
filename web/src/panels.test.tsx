import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import ConnectionHelp from './ConnectionHelp';
import Diagnostics from './Diagnostics';
import SecurityPanel from './SecurityPanel';
import CatalogSetup from './CatalogSetup';
import { PreparationDiagnostics } from './App';
import { parseSnapshot } from './domain';

describe('پنل‌های عملیات و امنیت', () => {
  it('توقف قدیمی را با زمان ثبت و فرمان فقط‌خواندنی نشان می‌دهد، بدون نسبت‌دادن آن به تلاش جدید', () => {
    const html = renderToStaticMarkup(<PreparationDiagnostics device={{ id: 'num03', last_error: 'guarded start failed', failed_at: 1789849223 }} />);
    expect(html).toContain('آخرین توقف ثبت‌شده');
    expect(html).toContain('درخواست‌های ردشده پیش از اجرا');
    expect(html).toContain('dateTime="2026-09-19T20:20:23.000Z"');
    expect(html).toContain('sudo device-provisioner diagnose --id num03');
    expect(html).toContain('user-select:all');
    expect(html).not.toContain('آخرین علت توقف آماده‌سازی');
    expect(html).not.toContain('<button');
  });
  it('برای وضعیت قدیمیِ بدون زمان، تاریخ یا علت تازه نمی‌سازد و شناسهٔ ناامن را وارد فرمان نمی‌کند', () => {
    const html = renderToStaticMarkup(<PreparationDiagnostics device={{ id: 'num03', last_error: 'guarded start failed' }} />);
    expect(html).not.toContain('<time');
    expect(html).toContain('guarded start failed');
    const unsafe = renderToStaticMarkup(<PreparationDiagnostics device={{ id: 'num03; reboot', last_error: '<script>error</script>', failed_at: null }} />);
    expect(unsafe).not.toContain('sudo device-provisioner');
    expect(unsafe).not.toContain('<script>');
  });
  it('برای خطای احراز هویت نصب مجدد پیشنهاد نمی‌دهد', () => {
    const html = renderToStaticMarkup(<ConnectionHelp status={401} retry={() => {}} />);
    expect(html).toContain('احراز هویت');
    expect(html).not.toContain('install.sh');
  });
  it('خطای درگاه راهنمای واقعی نصب دارد و رمز یا توکن درخواست نمی‌کند', () => {
    const html = renderToStaticMarkup(<ConnectionHelp status={502} retry={() => {}} />);
    expect(html).toContain('sudo bash /opt/android-farm/source/install.sh');
    expect(html).toContain('راهنمای نصب و رفع خطا');
    expect(html).toContain('ارسال نکنید');
  });
  it('وقتی API آماده نیست ابزار مرکزی و فرم رمز غیرفعال هستند', () => {
    const diagnostics = renderToStaticMarkup(<Diagnostics snapshot={null} fresh={false} canMutate={false} onActivate={() => {}} />);
    expect(diagnostics).toContain('disabled=""');
    expect(diagnostics).not.toContain('گزارش دریافت شد');
    const security = renderToStaticMarkup(<SecurityPanel proxies={[]} canMutate={false} onSubmit={async () => false} onJobs={() => {}} />);
    expect((security.match(/<fieldset disabled=""/g) ?? [])).toHaveLength(2);
    expect(security).toContain('رمزهای فعلی نمایش داده نمی‌شوند');
    expect(security).toContain('حساب Coolify');
  });
  it('وضعیت غیرفعال سرویس را با سلامت اشتباه نمی‌گیرد', () => {
    const snapshot = parseSnapshot({ schema_version: 1, collected_at: Date.now() / 1000, csrf_token: 'test', resources: null,
      devices: [], proxies: [], backups: [], artifacts: [], jobs: [], errors: [],
      settings: { central_activation_available: true }, components: [{ id: 'worker', label: 'عامل میزبان', state: 'inactive' }] });
    const html = renderToStaticMarkup(<Diagnostics snapshot={snapshot} fresh={true} canMutate={true} onActivate={() => {}} />);
    expect(html).toContain('غیرفعال');
    expect(html).toContain('سلامت کامل را تضمین نمی‌کند');
  });
  it('نام پیش‌فرض غیرمحرمانه است و تمام ورودی‌های رمز ابتدا خالی‌اند', () => {
    const html = renderToStaticMarkup(<SecurityPanel settings={{ web_username: 'operator', grafana_username: 'admin', credential_rotation_available: true, proxy_credentials_available: true }} proxies={[]} canMutate={true} onSubmit={async () => true} onJobs={() => {}} />);
    expect(html).toContain('value="mjpouladi"');
    const passwordInputs = html.match(/<input[^>]+type="password"[^>]*>/g) ?? [];
    expect(passwordInputs).toHaveLength(5);
    for (const input of passwordInputs) expect(input).toContain('value=""');
  });
  it('فهرست خالی را آماده‌سازی اولیه نشان می‌دهد و به بخش دقیق راهنما وصل می‌کند', () => {
    const snapshot = parseSnapshot({ schema_version: 1, collected_at: Date.now() / 1000, csrf_token: 'test', resources: null,
      devices: [], proxies: [], backups: [], artifacts: [], jobs: [], errors: [],
      settings: { application_catalog: { state: 'setup_required' } } });
    const html = renderToStaticMarkup(<CatalogSetup snapshot={snapshot} fresh={true} />);
    expect(html).toContain('در نصب تازه طبیعی است');
    expect(html).toContain('GUIDE.fa.md#approved-app-catalog');
    const diagnostics = renderToStaticMarkup(<Diagnostics snapshot={snapshot} fresh={true} canMutate={true} onActivate={() => {}} />);
    expect(diagnostics).toContain('نیاز به ثبت برنامه؛ اختلال سرویس نیست');
    expect(renderToStaticMarkup(<CatalogSetup snapshot={snapshot} fresh={false} />)).toBe('');
    snapshot.settings.application_catalog = { state: 'invalid' };
    expect(renderToStaticMarkup(<CatalogSetup snapshot={snapshot} fresh={true} />)).toBe('');
    snapshot.settings.application_catalog = { state: 'ready' };
    expect(renderToStaticMarkup(<CatalogSetup snapshot={snapshot} fresh={true} />)).toBe('');
    snapshot.settings.application_catalog = { state: 'files_required' };
    expect(renderToStaticMarkup(<CatalogSetup snapshot={snapshot} fresh={true} />)).toContain('فایل برنامه‌های ثبت‌شده آمادهٔ نصب نیست');
  });
});
