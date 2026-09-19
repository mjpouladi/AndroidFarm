"""Inspect and start only the farm's existing, managed central services."""
import json
import subprocess


UNITS = {
    'android-farm-api.service': 'API کنترل میزبان',
    'redis-server.service': 'Redis',
    'android-farm-worker.service': 'Worker عملیات',
    'android-farm-health.timer': 'پایش و بازیابی سلامت',
}
CONTAINERS = {
    'farm-console': ('console', 'کنسول وب'),
    'android-farm-gateway': ('gateway', 'درگاه وب'),
    'android-farm-prometheus': ('prometheus', 'Prometheus'),
    'android-farm-node-exporter': ('node-exporter', 'پایش میزبان'),
    'android-farm-cadvisor': ('cadvisor', 'پایش کانتینرها'),
    'android-farm-grafana': ('grafana', 'Grafana'),
}


class ComponentError(RuntimeError):
    pass


class Components:
    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def _units(self):
        response = self.runner(['systemctl', 'show', '--no-pager',
                                '--property=Id,LoadState,ActiveState,SubState', *UNITS],
                               text=True, capture_output=True, timeout=15, check=False)
        if response.returncode:
            raise ComponentError('وضعیت سرویس‌های میزبان قابل خواندن نیست.')
        rows = {}
        for block in response.stdout.strip().split('\n\n'):
            values = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
            if values.get('Id') in UNITS:
                rows[values['Id']] = values
        return rows

    def _containers(self):
        response = self.runner(['docker', 'inspect', *CONTAINERS], text=True,
                               capture_output=True, timeout=15, check=False)
        # Docker returns the existing objects as well when some names are absent.
        values = json.loads(response.stdout or '[]')
        if not isinstance(values, list) or response.returncode and not values:
            raise ComponentError('وضعیت کانتینرهای مرکزی قابل خواندن نیست.')
        return {item.get('Name', '').lstrip('/'): item for item in values
                if isinstance(item, dict) and item.get('Name', '').lstrip('/') in CONTAINERS}

    @staticmethod
    def _managed(item, role):
        labels = (item.get('Config') or {}).get('Labels') or {}
        return (labels.get('farm.stack') == 'core' and labels.get('farm.role') == role
                and labels.get('com.docker.compose.project') == 'android-farm-core')

    def snapshot(self):
        rows = []
        try:
            units = self._units()
        except (OSError, ValueError, subprocess.SubprocessError, ComponentError):
            units = {}
        for name, label in UNITS.items():
            values = units.get(name, {})
            active = values.get('ActiveState')
            if values.get('LoadState') == 'not-found':
                state, detail = 'inactive', 'نصب نشده؛ راه‌انداز را اجرا کنید.'
            elif active == 'active':
                state, detail = 'active', 'سرویس در حال اجراست.'
            elif active == 'failed':
                state, detail = 'failed', 'سرویس با خطا متوقف شده است.'
            elif active in {'inactive', 'activating', 'deactivating'}:
                state, detail = 'inactive', 'متوقف یا در حال تغییر وضعیت.'
            else:
                state, detail = 'unknown', 'وضعیت سرویس دریافت نشد.'
            rows.append(dict(id=name, label=label, state=state, detail=detail))
        try:
            containers = self._containers()
        except (OSError, ValueError, subprocess.SubprocessError, ComponentError):
            containers = None
        for name, (role, label) in CONTAINERS.items():
            item = containers.get(name) if containers is not None else None
            if containers is None:
                state, detail = 'unknown', 'وضعیت Docker دریافت نشد.'
            elif item is None:
                state, detail = 'inactive', 'کانتینر موجود نیست؛ استقرار Coolify را کامل کنید.'
            elif not self._managed(item, role):
                state, detail = 'unknown', 'مالکیت کانتینر با این نصب سازگار نیست.'
            else:
                runtime = item.get('State') or {}
                health = (runtime.get('Health') or {}).get('Status')
                if runtime.get('Paused') or runtime.get('Dead') or health == 'unhealthy':
                    state, detail = 'failed', 'وضعیت اجرا یا healthcheck نیاز به بررسی دارد.'
                elif runtime.get('Restarting') or not runtime.get('Running'):
                    state, detail = 'inactive', 'کانتینر متوقف یا در حال بازراه‌اندازی است.'
                elif health == 'starting':
                    state, detail = 'inactive', 'منتظر نتیجهٔ healthcheck.'
                else:
                    state = 'active'
                    detail = 'healthcheck سالم است.' if health == 'healthy' else 'فرایند در حال اجراست؛ healthcheck تعریف نشده است.'
            rows.append(dict(id=name, label=label, state=state, detail=detail))
        return rows

    def activate(self):
        """No deployment, arbitrary unit, unpause, device start or safety-hold release."""
        units, containers = self._units(), self._containers()
        # Preflight all identities before any partial start.
        if any(units.get(name, {}).get('LoadState') != 'loaded' for name in UNITS):
            raise ComponentError('سرویس‌های مرکزی کامل نصب نشده‌اند؛ راه‌انداز را دوباره اجرا کنید.')
        if any(name not in containers or not self._managed(containers[name], role)
               for name, (role, _) in CONTAINERS.items()):
            raise ComponentError('کانتینرهای مرکزی ناقص یا متعلق به نصب دیگری هستند؛ استقرار Coolify را بررسی کنید.')
        started = []
        try:
            self.runner(['systemctl', 'start', *UNITS], check=True, text=True,
                        capture_output=True, timeout=90)
            for name, item in containers.items():
                state = item.get('State') or {}
                if not state.get('Running') and not state.get('Restarting') and not state.get('Dead'):
                    self.runner(['docker', 'start', item['Id']], check=True, text=True,
                                capture_output=True, timeout=60)
                    started.append(name)
        except (OSError, KeyError, subprocess.SubprocessError):
            raise ComponentError('راه‌اندازی بعضی اجزا کامل نشد؛ وضعیت واقعی سرویس‌ها را بررسی کنید.') from None
        return {'completed': True, 'started_containers': started, 'components': self.snapshot()}
