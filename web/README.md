# کنسول نمایشی Android Farm

این React/TypeScript UI برای بازبینی معماری اطلاعات و جریان UX است. عملیات start/stop/queue/backup فقط در حافظهٔ tab شبیه‌سازی می‌شوند؛ هیچ اتصال زنده‌ای به Docker، Redis، API یا سرویس ثالث وجود ندارد و refresh دادهٔ نمونه را بازنشانی می‌کند. اطلاعات واقعی یا secret وارد آن نکنید.

کنسول همراه هسته در تنها Compose Application پروژه، یعنی `docker-compose.yml`، build می‌شود و روی `CONSOLE_DOMAIN` پشت middleware `farm-auth@file` قرار می‌گیرد.

## توسعهٔ محلی

```bash
cd web
npm ci
npm test -- --run
npm run build
npm run dev
```

نشانی پیش‌فرض Vite برابر `http://127.0.0.1:5173/` است. فایل‌های اصلی:

- `src/App.tsx`: صفحات و جریان‌های نمایشی؛
- `src/domain.ts`: state machine و صف شبیه‌سازی‌شده؛
- `src/resources.ts`: واردکردن snapshot گزارش منابع؛
- `src/*.test.ts`: آزمون‌های domain و resource.

برای نصب production، مرز دقیق demo و تمام runbookهای عملیاتی به [راهنمای واحد سیستم](../docs/GUIDE.fa.md) مراجعه کنید.
