# کنسول طراحی Android Farm

برای استقرار روی سرور، [راهنمای کامل نصب با Coolify](../docs/DEPLOYMENT_COOLIFY.fa.md) را دنبال کنید.

نسخهٔ تعاملی UI/UX با React، TypeScript و Vite؛ برای بازبینی محصول ساخته شده است. ناوگان از صفر شروع می‌شود و اولین دستگاه num01 است. عملیات start/stop/queue/backup **شبیه‌سازی‌شده** و فقط در حافظهٔ همین tab هستند. گزارش واقعی منابع را می‌توان از فایل JSON وارد کرد؛ این گزارش snapshot است و اتصال زنده نیست. هیچ اتصال به Docker، API سرور یا سرویس ثالث وجود ندارد. refresh دادهٔ نمونه را بازنشانی می‌کند؛ اطلاعات واقعی وارد نکنید.

## اجرا

Node.js 24 و npm:

```bash
cd web
npm ci
npm run dev
```

نشانی محلی: `http://127.0.0.1:5173`.

```bash
npm test
npm run build
npm run preview
```

برای انتشار همین **پیش‌نمایش** در Coolify، Compose مستقل `docker-compose.console.yml` در ریشه آماده است: Raw Compose، Base Directory `/`، فایل مذکور، دامنه در `CONSOLE_DOMAIN` و شبکه در `COOLIFY_NETWORK`. middleware قبلی `farm-auth@file` باید روی Traefik موجود باشد. این فایل هیچ API production را راه‌اندازی نمی‌کند. پورت مستقیم publish ندارد و nginx به‌صورت non-root اجرا می‌شود. `web/` build context مستقل دارد تا secrets پروژه به build ارسال نشوند.

## ساختار

* `src/App.tsx`: صفحات، کارت‌ها، dialogها و تجربهٔ RTL.
* `src/domain.ts`: state machine شبیه‌ساز، ظرفیت، صف FIFO و داده‌های ساختگی.
* `src/domain.test.ts`: رفتار ظرفیت، شماره یکتا و حفظ دادهٔ نمونه.
* `src/style.css`: سبک بصری، breakpointها، focus و reduced-motion.
* `../docs/ARCHITECTURE.fa.md`: معماری backend و ارتباط با host.
* `../docs/UX.fa.md`: جریان‌های کاربر و معیار پذیرش.
* `../docs/schema.sql`: طرح PostgreSQL، هنوز اجرا نشده.
* `../docs/openapi.yml`: قرارداد API پیشنهادی، هنوز سرویس فعال ندارد.

فونت‌ها در build محلی بسته‌بندی می‌شوند و runtime به سرویس فونت خارجی وابسته نیست. نمونه هیچ شماره‌ای در localStorage یا خروجی قابل دانلود نمی‌نویسد. شناسه‌های role و کاربر در UI نمایشی‌اند؛ امنیت واقعی سمت سرور پیاده خواهد شد.
