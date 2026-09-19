# کنسول عملیاتی Android Farm

این رابط React/TypeScript وضعیت واقعی میزبان را از `/api/v1/snapshot` دریافت می‌کند و عملیات مجاز را به صف پایدار API می‌فرستد. قطع اتصال، خطای صریح ایجاد می‌کند؛ وضعیت موفق یا دستگاه ساختگی تولید نمی‌شود. درخواست‌های در حال اجرا، نتیجه و خطا از backend می‌آیند؛ refresh مرورگر آن‌ها را پاک نمی‌کند.

کنسول همراه هسته در تنها Compose Application پروژه، یعنی `docker-compose.yml`، build می‌شود و روی `CONSOLE_DOMAIN` پشت middleware `farm-console-auth@file` قرار می‌گیرد. Nginx فقط `/api/` را به Unix socket محدود `/run/farm-api/control.sock` می‌فرستد؛ Docker socket در کانتینر وب وجود ندارد. API همان credential را مستقل بررسی می‌کند و درخواست‌های تغییر وضعیت به origin مجاز، توکن CSRF و idempotency key نیاز دارند. سرویس `android-farm-api.service` با همان نصب‌کننده فعال می‌شود.

## توسعهٔ محلی

```bash
cd web
npm ci
npm test -- --run
npm run build
npm run dev
```

نشانی پیش‌فرض Vite برابر `http://127.0.0.1:5173/` است. این فرمان فقط frontend را اجرا می‌کند؛ API عملیاتی به میزبان Linux مدیریت‌شده و مسیر احراز هویت‌شده نیاز دارد. برای کار با دادهٔ واقعی، نسخهٔ buildشده را از دامنهٔ نصب‌شده باز کنید. نصب، اتصال، کاتالوگ APK و عیب‌یابی فقط در [راهنمای واحد سیستم](../docs/GUIDE.fa.md) نگهداری می‌شوند.
