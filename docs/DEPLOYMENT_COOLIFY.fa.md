# استقرار Android Farm در Coolify

این runbook برای Ubuntu 22.04/24.04 با Coolify و Traefik داخلی نوشته شده است. lifecycle دستگاه‌ها فقط با `device-provisioner` انجام می‌شود؛ دکمهٔ Start مستقیم Coolify، `docker compose up --profile manual` و Docker socket برای اپراتور روزمره مجاز نیستند.

## ۱. پیش‌نیاز و مرز دسترسی

سرور باید Docker rootful، Compose v2، cgroup v2 و Binder Linux داشته باشد. Redroid privileged است؛ آن را مرز اجرای APK غیرقابل‌اعتماد یا multi-tenant فرض نکنید. فقط تیم زیرساخت root/Coolify admin به Docker دسترسی دارد؛ اپراتور از URL کنترل صفحه و CLI محدود استفاده می‌کند.

Coolify از قبل روی سرور فرض شده است. نصب مجدد سیستم‌عامل یا تغییر kernel در این راهنما انجام نمی‌شود.

```bash
sudo install -d -m 0755 /opt/android-farm
sudo git clone https://github.com/mjpouladi/AndroidFarm.git /opt/android-farm/release
sudo chown -R root:root /opt/android-farm/release
sudo chmod -R go-w /opt/android-farm/release
cd /opt/android-farm/release

sudo python3 installer/bootstrap.py --apply
sudo bash setup_host.sh --apply
sudo install -m 0755 provisioner.py /usr/local/sbin/device-provisioner
```

`device-provisioner` tree release را پیش از اجرای Docker بررسی می‌کند: Compose، ancestorها و build inputهای `images/` باید root-owned، بدون symlink و برای group/other غیرقابل‌نوشتن باشند. checkout موقت یا قابل‌نوشتن Coolify را به‌عنوان `compose_file` CLI تنظیم نکنید.

برای بررسی پیش‌نیاز بدون تغییر:

```bash
cd /opt/android-farm/release
sudo python3 installer/bootstrap.py
sudo bash setup_host.sh --check
sudo device-provisioner resources
```

در Ubuntuهای جدید ممکن است `ashmem_linux` وجود نداشته باشد؛ Compose `androidboot.use_memfd=true` را فعال می‌کند. نبود `/dev/binder` یا خطای Binder یک blocker است؛ قبل از پایلوت مطابق راهنمای رسمی Redroid آن را حل کنید.

## ۲. ساخت Project و Appها

در Coolify یک Project به نام **android-farm** بسازید و دو Application از نوع Docker Compose/Raw Compose اضافه کنید:

| App | Compose | دامنه | هدف |
|---|---|---|---|
| `farm-core` | `docker-compose.console.yml` | `console.farm.example.com` | UI/UX نمایشی و راهنمای اپراتور؛ backend عملیاتی ندارد |
| `farm-devices` | `docker-compose.farm.yml` | `farm.example.com` | کاتالوگ deviceها، `farm-anchor` و routeهای screen |

در `farm-devices` فقط `farm-anchor` بدون profile است. **`COMPOSE_PROFILES=manual` را در ENV Coolify قرار ندهید.** Deploy عادی باید فقط همین anchor را بسازد. در لاگ deploy تأیید کنید که هیچ `android-*`، `proxy-*` یا `screen-*` بالا نیامده است.

برای هر App، repository/branch مورد تأیید را انتخاب کنید. Coolify source را برای build/deploy نگه می‌دارد، اما CLI میزبان از `/opt/android-farm/release` استفاده می‌کند تا یک checkout قابل‌تغییر به spec ریشهٔ Docker تبدیل نشود. پس از هر تغییر release، همان commit را در Coolify deploy و در `/opt/android-farm/release` هم checkout کنید؛ تفاوت commit قابل‌قبول نیست.

## ۳. ENV و secrets

در UI Coolify برای `farm-devices` این ENVهای غیرحساس را تنظیم کنید:

```dotenv
FARM_DOMAIN=farm.example.com
COOLIFY_NETWORK=coolify
FARM_SECRETS_DIR=/etc/android-farm/secrets
# پس از pilot، هر image را به digest تأییدشده pin کنید.
REDROID_IMAGE=redroid/redroid:12.0.0-latest
PROXY_IMAGE=android-farm/proxy:1
SCREEN_IMAGE=android-farm/screen:1
```

برای `farm-core` اضافه کنید:

```dotenv
CONSOLE_DOMAIN=console.farm.example.com
COOLIFY_NETWORK=coolify
```

secrets در UI Coolify یا `.env` قرار نمی‌گیرند:

| مسیر میزبان | محتوا | mode |
|---|---|---|
| `/etc/android-farm/secrets/num01.json` | credential پراکسی همان دستگاه | `0600` |
| `/etc/android-farm/apk-trust.json` | allowlist مستقل package/signer APK | `0600` |
| `/var/lib/android-farm/identity.key` | HMAC inventory | `0600` |
| `/var/lib/android-farm/*.json` | inventory، baseline و hold | `0600` |
| `/etc/android-farm` و `/var/lib/android-farm` | دایرکتوری‌های حساس | `0700` |

فایل secret هر device فقط هنگام `device-provisioner up --request` ایجاد می‌شود. از password، phone number کامل، token، APK یا signing certificate در Git/log/Coolify ENV استفاده نکنید.

## ۴. Traefik، Basic Auth و path-based routing

از بخش **Servers → Proxy → Dynamic Configurations** در Coolify برای اضافه‌کردن middleware استفاده کنید. اگر UI version شما این بخش را ندارد، مسیر dynamic فعال Traefik را با inspect تأیید و فایل زیر را در آن نصب کنید:

```bash
sudo apt-get install -y apache2-utils
sudo install -m 0644 /opt/android-farm/release/traefik/farm-auth.yml \
  /data/coolify/proxy/dynamic/farm-auth.yml
sudo htpasswd -cB /data/coolify/proxy/dynamic/farm-users.htpasswd operator
sudo chmod 600 /data/coolify/proxy/dynamic/farm-users.htpasswd
```

`-c` فقط برای ساخت نخستین فایل است؛ برای کاربر بعدی آن را حذف کنید. اگر Traefik با UID غیرroot اجرا می‌شود، گروه خواندن محدود همان UID را تنظیم کنید. حذف middleware برای حل خطا مجاز نیست.

generator برای `num01` labelهای زیر را تولید می‌کند:

```yaml
traefik.enable: "true"
traefik.docker.network: "${COOLIFY_NETWORK:-coolify}"
traefik.http.routers.farm-num01.rule: "Host(`${FARM_DOMAIN:-farm.example.com}`) && PathPrefix(`/d/num01/`)"
traefik.http.routers.farm-num01.entrypoints: https
traefik.http.routers.farm-num01.tls: "true"
traefik.http.routers.farm-num01.tls.certresolver: letsencrypt
traefik.http.routers.farm-num01.middlewares: farm-auth@file,farm-num01-strip
traefik.http.middlewares.farm-num01-strip.stripprefix.prefixes: /d/num01
traefik.http.services.farm-num01.loadbalancer.server.port: "6080"
```

ابتدا Basic Auth و بعد حذف prefix اعمال می‌شود؛ همین router HTML، asset و WebSocket noVNC را پوشش می‌دهد. URL را با slash انتهایی باز کنید:

```text
https://farm.example.com/d/num01/
```

`screen` پورت 6080 را روی میزبان publish نمی‌کند. با این حال هر workload مورداعتماد روی network مشترک Coolify می‌تواند به screen دسترسی شبکه‌ای داشته باشد؛ هیچ workload غیرمورداعتماد را به آن network وصل نکنید. برای RBAC per-device یا MFA از ForwardAuth/Authelia با policy جدا استفاده کنید.

## ۵. Deploy و ثبت configuration عملیاتی

ابتدا `farm-core` و سپس `farm-devices` را deploy کنید. سپس این مقدارها را از deployment واقعی استخراج کنید:

```bash
docker inspect farm-anchor --format '{{ index .Config.Labels "com.docker.compose.project" }}'
docker inspect farm-anchor --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
```

اولی را به `compose_project` بدهید. `compose_file` به Compose همان commit در release immutable اشاره می‌کند، نه `working_dir` موقت Coolify:

```bash
sudo install -m 0600 /opt/android-farm/release/installer/provisioner.example.json \
  /etc/android-farm/provisioner.json
sudo install -m 0600 /opt/android-farm/release/installer/apk-trust.example.json \
  /etc/android-farm/apk-trust.json
sudoedit /etc/android-farm/provisioner.json
sudoedit /etc/android-farm/apk-trust.json
```

نمونهٔ config:

```json
{
  "compose_file": "/opt/android-farm/release/docker-compose.farm.yml",
  "compose_project": "ACTUAL_COOLIFY_PROJECT",
  "console_url": "https://farm.example.com",
  "secret_dir": "/etc/android-farm/secrets",
  "backup_dir": "/var/backups/android-farm",
  "state_dir": "/var/lib/android-farm",
  "apk_trust_file": "/etc/android-farm/apk-trust.json"
}
```

`compose_project` باید دقیقاً با label anchor برابر باشد. پیش از lifecycle، Compose resolved توسط CLI به‌صورت strict بررسی می‌شود: topology شبکه، CIDR، bridge، static IP، secret، volume named external، capabilityها، ADB loopback، restart policy و auth route باید با generator یکی باشند.

هر `redroid-data-numXX` با local bind driver به `/opt/farm/data/instances/numXX/data` متصل می‌شود. این دایرکتوری را دستی حذف، جابه‌جا یا clone نکنید؛ نبود آن برای device تخصیص‌داده‌شده fail-closed است و restore می‌خواهد.

## ۶. Runbook روزمره

```bash
# تخصیص ترتیبی num01، سپس num02 و …
sudo device-provisioner up --request /root/farm-input/num01.json

# شروع یک device از قبل آماده
sudo device-provisioner up --id num01
sudo device-provisioner check --id num01
sudo device-provisioner check-ip --id num01 --json
sudo device-provisioner status

# پایان کار بدون حذف /data
sudo device-provisioner down --id num01

# رخداد حساب/شبکه یا نگهداری
sudo device-provisioner hold --id num01 --reason ip-change
sudo device-provisioner release --id num01 --review-completed

# بکاپ offline با manifest SHA-256
sudo device-provisioner backup --id num01
```

در start، gateway تنها در حالی‌که Android خاموش است به upstream allow می‌شود. پس از تأیید IP gateway، Android با خروجی مسدود boot می‌شود تا baseline واقعی آن ثبت/مقایسه شود. سپس Android pause می‌شود، gateway دوباره بررسی می‌شود و در پایان IP از Android shell با IP approved مقایسه می‌شود. mismatch سبب توقف می‌شود؛ `check-ip` نیز hold پایدار ثبت، egress را قطع و containerها را stop می‌کند.

همزمان بیش از ۱۰ Android شروع نمی‌شود. میزان واقعی CPU/RAM/disk/inode در هر start بررسی می‌شود. `docker stats` برای مشاهدهٔ جزئیات مفید است، اما تصمیم admission را جایگزین نمی‌کند.

## ۷. scale، backup و پذیرش

برای `num71`، خروجی را تولید، review و در هر دو source deploy/release به یک commit ارتقا دهید:

```bash
cd /opt/android-farm/release
python3 generate_farm.py --count 71 --output docker-compose.farm.yml
```

قبل از production، pilot با یک دستگاه انجام دهید:

- فقط `farm-anchor` پس از deploy عادی روشن باشد.
- ADB فقط روی `127.0.0.1` باشد و URL screen بدون Basic Auth پاسخ موفق ندهد.
- کنترل noVNC و WebSocket پس از ورود کار کنند.
- IP پراکسی و Android shell برابر IP approved باشد؛ قطع upstream نباید fallback مستقیم داشته باشد.
- restart عادی دادهٔ `/data` و baseline را حفظ کند.
- دستگاه یازدهم رد شود؛ reboot دستگاه‌ها را خودکار روشن نکند.
- backup/restore در volume جدا و با SHA-256 manifest بررسی شود.

`device-provisioner render` فقط خروجی تک‌دستگاه برای review/interface است و bind mount دارد؛ آن را `compose_file` lifecycle اصلی قرار ندهید. lifecycle managed فقط named volumeهای `redroid-data-numXX` را می‌پذیرد.

## منابع

- [Redroid](https://github.com/remote-android/redroid-doc)
- [Docker Compose در Coolify](https://coolify.io/docs/applications/builds/docker-compose)
- [Traefik در Coolify](https://coolify.io/docs/core/networking/proxy/traefik/overview)
- [مستندات sing-box](https://sing-box.sagernet.org/)
