import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import ConnectionHelp from './ConnectionHelp';
import Diagnostics from './Diagnostics';
import SecurityPanel from './SecurityPanel';
import { parseSnapshot } from './domain';

describe('پنل‌های عملیات و امنیت', () => {
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
});
