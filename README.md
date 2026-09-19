# Android Farm برای QA داخلی

پلتفرم on-demand برای اجرای Android/Redroid روی Ubuntu 22.04/24.04 و Coolify؛ با دادهٔ پایدار، پراکسی اختصاصی، ADB محلی، صف Redis و مانیتورینگ. ظرفیت بر اساس منابع واقعی سرور محاسبه می‌شود و روی ۷۰ دستگاه قفل نیست.

**مرجع واحد نصب و مدیریت: [راهنمای گام‌به‌گام فارسی](docs/GUIDE.fa.md).** از بخش «شروع سریع» همان راهنما شروع کنید؛ DNS، تنظیمات Coolify، نصب، اولین دستگاه، پشتیبان‌گیری و عیب‌یابی همگی در همان فایل هستند.

آدرس‌های انتخاب‌شده:

| بخش | آدرس |
|---|---|
| سامانه | `https://commex-box.com/` |
| کنترل دستگاه | `https://commex-box.com/d/num01/` |
| مانیتورینگ | `https://metrics.commex-box.com/` |
| Coolify موجود | `https://coolify.commex-box.com/` |

این‌ها تنظیمات مقصدند؛ انتشار کد، DNS یا سرور را خودکار تغییر نمی‌دهد. پس از آماده‌کردن DNS و توکن Coolify طبق راهنما، روی سرور `185.208.172.141` اجرا کنید:

```bash
curl -fsSL https://raw.githubusercontent.com/mjpouladi/AndroidFarm/main/install.sh -o install-android-farm.sh
sudo bash install-android-farm.sh --domain commex-box.com
```

نصب‌کننده ساخت پروژه، Compose، ENV، استقرار و فعال‌سازی CLI/Worker را هماهنگ می‌کند. دامنهٔ پنل در نصب تازه همان دامنهٔ اصلی است؛ نصب‌های قبلی تنظیمات ذخیره‌شده را حفظ می‌کنند. برای تغییر پنل نصب قبلی، گزینهٔ `--console-domain commex-box.com` را نیز بدهید. حالت IP همچنان اختیاری است.

کنسول React فعلی **دموی UX** است؛ مدیریت واقعی از CLI انجام می‌شود. تست‌های محلی جای پذیرش TLS، Redroid، پراکسی و WebSocket روی Ubuntu مقصد را نمی‌گیرند.

این پروژه برای QA مجاز است و جعل IMEI/گوشی، پنهان‌سازی شبیه‌ساز، دورزدن Meta/Play Integrity یا ثبت‌نام انبوه را پیاده نمی‌کند. APK باید hash و signer تأییدشده داشته باشد.
