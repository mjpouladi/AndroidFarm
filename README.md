# Android Farm برای QA داخلی

پلتفرم on-demand برای اجرای Android/Redroid روی Ubuntu 22.04/24.04 با Coolify است. هر دستگاه data پایدار، proxy اختصاصی، ADB فقط روی loopback و کنترل صفحه پشت Traefik دارد. ظرفیت کاتالوگ و تعداد روشن همزمان از CPU، RAM، disk و inode همان میزبان محاسبه می‌شود و روی عدد ۷۰ قفل نیست.

راه‌انداز هدایت‌شده، استقرار در Coolify و فعال‌سازی CLI، Redis Worker و بررسی سلامت را هماهنگ می‌کند. کنسول React فعلی **دموی UX** است و دکمه‌های آن به Docker یا Worker production وصل نیستند.

این پروژه برای آزمون داخلیِ مجاز است. دورزدن Meta/Play Integrity، تغییر IMEI، جعل گوشی تجاری، پنهان‌سازی root/container، خودکارسازی OTP/ثبت‌نام انبوه و تضمین جلوگیری از ban را پیاده نمی‌کند. نصب برنامه فقط برای APK تأییدشده با hash و signer مجاز انجام می‌شود.

## نصب ساده

روی سرور Ubuntu 22.04/24.04 که Coolify در آن فعال است، این دو دستور را اجرا کنید:

```bash
curl -fsSL https://raw.githubusercontent.com/mjpouladi/AndroidFarm/main/install.sh -o install-android-farm.sh
sudo bash install-android-farm.sh
```

راه‌انداز فقط دامنهٔ فارم، آدرس Coolify و توکن API را می‌گیرد. برای Coolify روی همین سرور، آدرس پیش‌فرض را با Enter بپذیرید. توکن را در **Keys & Tokens → API tokens** بسازید و دسترسی API را فعال کنید؛ توکن به دسترسی خواندن، نوشتن و اجرای Deploy در تیم مربوط نیاز دارد. توکن مخفی دریافت می‌شود و ذخیره نمی‌شود.

قبل از اجرا، DNS دامنهٔ خودتان مثل `farm.example.com` و دو نام `console.farm.example.com` و `metrics.farm.example.com` را به سرور وصل کنید. می‌توانید برای زیرنام‌ها از رکورد `*.farm.example.com` استفاده کنید.

نصب‌کننده منابع و شبکه را تشخیص می‌دهد، پروژهٔ `android-farm` و برنامهٔ `farm-core` را در Coolify می‌سازد، ENV را می‌فرستد، همان commit را deploy می‌کند، CLI/Redis/Worker/health timer را فعال می‌کند و وضعیت HTTPS را بررسی می‌کند. کپی دستی Compose، ENV، فایل رمز یا اجرای دوبارهٔ `apply` لازم نیست. رمز وب و لینک‌ها در پایان نشان داده می‌شوند؛ رمز Grafana در فایل خصوصی میزبان می‌ماند.

اگر نصب قطع شد، ادامه با همین فرمان است:

```bash
sudo bash /opt/android-farm/source/install.sh
```

راه‌انداز source دارای تغییر محلی، برنامهٔ نامرتبط Coolify یا Redis مدیریت‌نشدهٔ قبلی را بازنویسی نمی‌کند. برای نصب‌های سفارشی و مسیر دستی `plan/apply/doctor`، [راهنمای واحد](docs/GUIDE.fa.md) را ببینید.

تمام مراحل نصب، معماری، proxy، profile، lifecycle، Worker/Redis، monitoring، Ansible، backup/restore، upgrade و آزمون پذیرش در [راهنمای واحد فارسی](docs/GUIDE.fa.md) آمده است.

## فرمان‌های اصلی

```bash
sudo device-provisioner resources --json
sudo device-provisioner proxy list
sudo device-provisioner up --request /root/farm-input/device-request.json
sudo device-provisioner up --id num01
sudo device-provisioner check --id num01
sudo device-provisioner check-ip --id num01 --json
sudo device-provisioner backup --id num01
sudo device-provisioner down --id num01
sudo device-provisioner status
```

Compose و تست‌های محلی باید پیش از انتشار اجرا شوند، اما بوت واقعی Redroid، egress proxy، TLS، noVNC/WebSocket و restore فقط روی میزبان Ubuntu مقصد قابل پذیرش‌اند.
