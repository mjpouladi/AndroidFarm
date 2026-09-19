# گزارش اعتبارسنجی طراحی — ۲۰۲۶/۰۹/۱۹

## انجام‌شده در محیط توسعه

- `python -m compileall -q ops installer provisioner.py tests` موفق.
- `python -m unittest discover -s tests -v` موفق؛ ۱۸ آزمون Python برای generator، topology، ظرفیت، admission، inventory monotonic، hold، identity drift/missing-baseline، parser IP، policy Compose و APK trust policy.
- `docker compose --profile manual -f docker-compose.yml config --quiet` و Compose console با Compose 2.39.4 parse شدند.
- `npm test -- --run` موفق؛ ۱۰ آزمون UI/domain/resource. `npm run build` نیز موفق.
- UI در مرورگر به‌صورت demo بررسی شد: شروع از دستگاه صفر، افزودن num01 با دادهٔ نمونه، جست‌وجو، queue، modal، backup demo و responsiveness موبایل.

## مرز اعتبار

- Docker Engine، Ubuntu، Coolify، DNS، TLS، Traefik، image build، Binder و Redroid واقعی در این workspace اجرا نشده‌اند.
- IP واقعی پراکسی، kill-switch، DNS، boot Redroid، ADB، noVNC/WebSocket و restore باید روی میزبان مقصد با pilot یک‌دستگاه آزموده شوند.
- UI فقط prototype تعاملی است؛ API/Worker/Agent/OIDC/RBAC/SSE/backend production ندارد.
- تست APK، identity و Compose policy از mock/دادهٔ ساختگی استفاده می‌کنند و نصب یا کنترل واقعی device محسوب نمی‌شوند.
- هیچ commit یا push به GitHub و هیچ deploy واقعی در این مرحله انجام نشده است.
