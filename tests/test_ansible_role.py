import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible" / "roles" / "android_farm"


class AnsibleRoleContractTests(unittest.TestCase):
    def test_release_selection_is_immutable_and_shared_by_services(self):
        defaults = (ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
        tasks = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
        worker = (ROLE / "templates" / "android-farm-worker.service.j2").read_text(encoding="utf-8")
        health = (ROLE / "templates" / "android-farm-health.service.j2").read_text(encoding="utf-8")

        self.assertIn('farm_release_dir: ""', defaults)
        self.assertIn("/var/lib/android-farm/install-state.json", defaults)
        self.assertIn("^/opt/android-farm/releases/[0-9a-f]{16}$", tasks)
        self.assertIn("argv: [readlink, -f, --", tasks)
        self.assertIn("ansible_facts.distribution_version in ['22.04', '24.04']", tasks)
        self.assertIn("farm_provisioner_configuration.stat.mode == '0600'", tasks)
        self.assertIn("farm_install_state_metadata.stat.mode == '0600'", tasks)
        self.assertIn("farm_provisioner_state.compose_file | default('') == farm_effective_release_dir", tasks)
        self.assertIn("farm_provisioner_state.compose_project | default('') == 'android-farm-runtime'", tasks)
        self.assertIn("proxy_network_names == ['farm-control-' ~ item.id, 'farm-egress-' ~ item.id]", tasks)
        self.assertIn("farm_device_ids | unique | list | length == farm_device_ids | length", tasks)
        self.assertIn("item.id[3:] | int <= 8192", tasks)
        self.assertIn("item.id == 'num' ~ ('%02d' | format(item.id[3:] | int))", tasks)
        self.assertIn("apple|asus|galaxy|google|honor|huawei", tasks)
        self.assertIn("item.locale is match('^[a-z]{2,3}", tasks)
        self.assertNotIn("WorkingDirectory={{ farm_release_dir }}", worker + health)
        self.assertEqual((worker + health).count("WorkingDirectory={{ farm_effective_release_dir }}"), 2)

    def test_redis_acl_is_host_scoped_and_least_privilege(self):
        acl = (ROLE / "templates" / "redis-users.acl.j2").read_text(encoding="utf-8")

        self.assertIn("user default off", acl)
        self.assertIn("~android-farm:*", acl)
        self.assertIn(" reset on ", acl)
        for broad_rule in ("+@all", "+@read", "+@write", "+client", "+flushall", "+flushdb"):
            self.assertNotIn(broad_rule, acl)
        for required_command in ("+ping", "+hget", "+hset", "+lpush", "+zadd", "+eval", "+multi", "+exec"):
            self.assertIn(required_command, acl)

    def test_check_mode_does_not_restart_services(self):
        handlers = (ROLE / "handlers" / "main.yml").read_text(encoding="utf-8")
        self.assertEqual(handlers.count("not ansible_check_mode"), 3)


if __name__ == "__main__":
    unittest.main()
