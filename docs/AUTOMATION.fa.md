# Provisioning کنترل‌شده و سیاست عملیاتی

این سند رفتار production-ready ابزار `device-provisioner` را توضیح می‌دهد. تنها entrypoint عملیاتی آن است؛ `ops/farmctl.py` implementation داخلی است و نباید مستقیماً از runbook یا UI فراخوانی شود.

## مرز اتوماسیون

| مرحله | رفتار |
|---|---|
| Bootstrap Ubuntu و Docker | `installer/bootstrap.py --apply`، فقط روی Ubuntu مقصد و بدون نصب مجدد OS/disk |
| Binder و Redroid prerequisite | `setup_host.sh --apply` |
| Compose و image build | Coolify از commit reviewشده؛ lifecycle از release root-owned |
| تخصیص device، volume، proxy و boot | `device-provisioner up --request`، دقیقاً یک device در هر request |
| APK QA | hash، package، signer allowlist مستقل، SDK/ABI و permission allowlist بررسی می‌شود |
| ورود، OTP و acceptance سرویس ثالث | فقط دستی توسط اپراتور مجاز؛ خودکار نیست |
| UI وب | demo تعاملی است؛ backend/کنترل production ندارد |

## ورودی خصوصی device

نمونهٔ request در `installer/device-request.example.json` است. فایل باید root-owned و `0600` باشد:

```json
{
  "phone": "+989000000001",
  "owner_authorized": true,
  "proxy_file": "/root/farm-input/num01-proxy.json",
  "expected_egress_ip": "198.51.100.42",
  "apk_path": "/root/farm-input/approved-qa-app.apk",
  "apk_sha256": "64_HEX_SHA256",
  "apk_package": "com.example.qaapp",
  "apk_permissions": ["android.permission.CAMERA"],
  "apk_activity": ".MainActivity"
}
```

شمارهٔ کامل فقط در request خصوصی است. inventory فقط HMAC شماره و مقدار maskشده ذخیره می‌کند و یک شماره یا session پراکسی را به دو device تخصیص نمی‌دهد. `owner_authorized=true` تأیید فنی مالکیت نیست؛ کنترل دسترسی و فرایند سازمانی باید آن را پشتیبانی کند.

فایل پراکسی شامل `type` (`socks` یا `http`)، IPv4 عمومی pinned، port و credential اختصاصی است. hostname را پیش از ثبت روی host resolve و IPv4 را pin کنید. gateway کنونی TCP را از HTTP CONNECT/SOCKS5 عبور می‌دهد و DNS را به DoH از همان upstream می‌فرستد؛ UDP عمومی، QUIC و HTTPS-proxy transport پشتیبانی نمی‌شوند.

## state machine و پایداری

رکورد جدید به‌ترتیب این stateها را می‌گذراند:

```text
reserved → volume_created → secret_installed → identity_baselining
         → installing_apk → ready_for_operator
```

در failure، phase `failed` می‌شود و شماره/ID دوباره تخصیص نمی‌یابند. همان request immutable را اجرا کنید؛ تغییر شماره، endpoint/session یا artifact تخصیصی موجب رد شدن request می‌شود. تغییر password پراکسی با endpoint/session و IP خروجی ثابت قابل ادامه است، اما هر تغییر شبکه یا IP نیازمند review و تخصیص جدید است.

اولین baseline فقط در state `identity_baselining` ثبت می‌شود. در restartهای بعدی، حذف baseline یا تغییر serial/brand/model/manufacturer/build fingerprint/تنظیم shell Android ID دستگاه را متوقف می‌کند؛ baseline جدید خودکار پذیرفته نمی‌شود. این گارد فقط تشخیص drift محیط است و attestation یا هویت دستگاه فیزیکی را اثبات نمی‌کند.

## فرآیند up

```bash
sudo device-provisioner up --request /root/farm-input/num01.json
```

فرآیند زیر قفل میزبان و بررسی capacity انجام می‌شود:

1. request، APK، signer policy، پراکسی IPv4 و IP خروجی approved اعتبارسنجی می‌شود.
2. اولین slot ترتیبی رزرو و named volume با label مالکیت ساخته می‌شود؛ volume با local bind driver به `/opt/farm/data/instances/numXX/data` متصل است.
3. secret در `/etc/android-farm/secrets/numXX.json` با `0600` نصب می‌شود.
4. host guard روی bridge device برقرار می‌شود و فقط TCP به endpoint پراکسی اجازه می‌گیرد؛ در این زمان Android هنوز خاموش است.
5. gateway بالا می‌آید و IP خروجی آن باید دقیقاً با `expected_egress_ip` برابر باشد.
6. host guard بسته می‌شود؛ Android و screen با خروجی مسدود boot می‌شوند و baseline واقعی بررسی می‌شود.
7. Android pause می‌شود، gateway دوباره با IP approved آزموده، سپس Android آزاد می‌شود و IP دیده‌شده از Android shell با مقدار approved مقایسه می‌شود.
8. APK QA تأییدشده نصب و activity موردنظر یا launcher باز می‌شود. موفقیت یعنی `ready_for_operator`.

در هر خطا device stop می‌شود. در هیچ مرحله‌ای fallback مستقیم به IP میزبان وجود ندارد.

## APK trust policy

`/etc/android-farm/apk-trust.json` جدا از request نگهداری می‌شود تا اپراتور درخواست نتواند signer مورداعتماد را خودش تعریف کند:

```json
{
  "packages": {
    "com.example.qaapp": {
      "signers": ["64_HEX_CERTIFICATE_SHA256"]
    }
  }
}
```

installer حداکثر 500 MiB می‌پذیرد، SHA-256 content، output `apksigner`، package از `aapt`، minimum SDK و ABI را بررسی می‌کند. فقط CAMERA، READ_CONTACTS و RECORD_AUDIO به‌صورت explicit قابل grant هستند. هیچ permission حساس دیگری، translation layer، patch APK یا setup اختصاصی vendor نصب نمی‌شود.

## lifecycle روزمره

```bash
sudo device-provisioner up --id num01
sudo device-provisioner check --id num01
sudo device-provisioner check-ip --id num01 --json
sudo device-provisioner down --id num01
sudo device-provisioner status --json
```

`check-ip` هم IP gateway و هم IP خروجی از Android shell را مقایسه می‌کند. هر mismatch hold `ip-change` را پایدار ثبت، egress را قطع و device را stop می‌کند. آزمون مرورگر داخل Android در pilot همچنان لازم است، زیرا test shell جایگزین یک acceptance test واقعی UI/WebSocket نیست.

برای نگهداری یا رخداد حساب/مالکیت:

```bash
sudo device-provisioner hold --id num01 --reason maintenance
sudo device-provisioner release --id num01 --review-completed
```

hold ابتدا persist می‌شود، سپس guard همان bridge را قطع و containerها را خاموش می‌کند. release خودکار یا time-based ندارد.

## backup و restore

```bash
sudo device-provisioner backup --id num01
```

backup device را offline نگه می‌دارد و زیر `/var/backups/android-farm/num01/` یک archive `0600` و manifest مستقل شامل SHA-256 تولید می‌کند. `.partial` هرگز restore candidate نیست. همراه آن `/var/lib/android-farm`، `/etc/android-farm` و digest imageهای استفاده‌شده را با restic/borg رمزنگاری‌شده backup کنید.

restore اول در named volume جدید، روی محیط ایزوله و در حالی‌که نمونهٔ اصلی خاموش است انجام می‌شود. `/data` فعال را extract نکنید و نسخهٔ اصلی و restore را همزمان روشن نکنید.

## ظرفیت و پایش

برای هر device، budget محافظه‌کارانه ۶ vCPU و ۵٫۲۵ GiB است. reserve میزبان بزرگ‌ترِ ۲ CPU یا ۱۰٪ CPU و بزرگ‌ترِ ۸ GiB یا ۲۰٪ RAM است. در host نمونهٔ ۷۲ vCPU / ۹۶ GiB سقف ۱۰ device حاصل می‌شود؛ load، RAM آزاد، disk و inode ممکن است آن را پایین‌تر بیاورند.

```bash
sudo device-provisioner resources --json
sudo device-provisioner status
docker stats android-num01 proxy-num01 screen-num01
```

هرگز برای رفع OOM یا failure، restart policy Android را به `always` تغییر ندهید. علت را بررسی، backup و سپس یک start کنترل‌شده انجام دهید.

## استفادهٔ مجاز

دادهٔ پایدار per-device و IP اختصاصی، عیب‌یابی، privacy boundary و ثبات آزمون را بهتر می‌کند؛ تضمین وضعیت یک حساب یا مجوز سرویس ثالث نیست. برای فعالیت حساس یا کاربرد سازمانی، مسیر رسمی vendor و حساب‌های دارای اختیار را انتخاب کنید. این فارم sandbox QA است و session/کلیدها را به engine غیررسمی منتقل نمی‌کند.
