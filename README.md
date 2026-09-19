# Android Farm برای QA داخلی

پلتفرم on-demand برای اجرای Android/Redroid روی Ubuntu 22.04/24.04 با Coolify است. هر دستگاه data پایدار، proxy اختصاصی، ADB فقط روی loopback و کنترل صفحه پشت Traefik دارد. ظرفیت کاتالوگ و تعداد روشن همزمان از CPU، RAM، disk و inode همان میزبان محاسبه می‌شود و روی عدد ۷۰ قفل نیست.

قابلیت‌های عملیاتی شامل installer دو مرحله‌ای `plan/apply/doctor`، CLI چرخهٔ عمر، registry پراکسی HTTP/SOCKS5، profile شفاف QA، Redis Worker، بازیابی محدود ADB، Prometheus/Grafana و Ansible است. کنسول React فعلی **دموی UX** است و دکمه‌های آن به Docker یا Worker production وصل نیستند.

این پروژه برای آزمون داخلیِ مجاز است. دورزدن Meta/Play Integrity، تغییر IMEI، جعل گوشی تجاری، پنهان‌سازی root/container، خودکارسازی OTP/ثبت‌نام انبوه و تضمین جلوگیری از ban را پیاده نمی‌کند. نصب برنامه فقط برای APK تأییدشده با hash و signer مجاز انجام می‌شود.

## شروع سریع

روی سروری که Coolify در آن فعال است:

```bash
sudo install -d -m 0755 /opt/android-farm
sudo git clone https://github.com/mjpouladi/AndroidFarm.git /opt/android-farm/source
cd /opt/android-farm/source
sudo chown -R root:root /opt/android-farm/source
sudo chmod -R go-w /opt/android-farm/source

sudo bash -c 'umask 077; read -rsp "Farm Basic Auth password: " p; printf "\n"; printf "%s\n" "$p" > /root/android-farm-basic-auth.pass'

sudo python3 installer/install.py plan \
  --farm-domain farm.example.com \
  --console-domain console.farm.example.com \
  --catalog-count auto \
  --auth-user operator \
  --auth-password-file /root/android-farm-basic-auth.pass

sudo python3 installer/install.py apply \
  --farm-domain farm.example.com \
  --console-domain console.farm.example.com \
  --catalog-count auto \
  --auth-user operator \
  --auth-password-file /root/android-farm-basic-auth.pass
```

سپس `docker-compose.yml` را به‌عنوان **یک** Compose Application در Coolify وارد، مقدارهای `/etc/android-farm/coolify.env` را کپی و deploy کنید. همان فرمان `apply` را دوباره اجرا و بعد `doctor` را بررسی کنید. installer فایل خصوصی رمز Grafana را خودکار می‌سازد و مقدار secret در ENV قرار نمی‌گیرد.

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
