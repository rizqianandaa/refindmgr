import unittest
from types import SimpleNamespace

from refindmgr import system as system_mod


def _fake_which(available):
    def fake(binary):
        return f"/usr/bin/{binary}" if binary in available else None
    return fake


class TestDetectPackageManager(unittest.TestCase):
    def test_detects_apt(self):
        manager = system_mod.detect_package_manager(which_fn=_fake_which({"apt-get"}))
        self.assertIsNotNone(manager)
        self.assertEqual(manager.name, "apt")

    def test_detects_pacman_when_only_pacman_present(self):
        manager = system_mod.detect_package_manager(which_fn=_fake_which({"pacman"}))
        self.assertEqual(manager.name, "pacman")

    def test_returns_none_when_nothing_available(self):
        manager = system_mod.detect_package_manager(which_fn=_fake_which(set()))
        self.assertIsNone(manager)

    def test_prefers_first_match_in_priority_order(self):
        manager = system_mod.detect_package_manager(
            which_fn=_fake_which({"apt-get", "dnf", "pacman"})
        )
        self.assertEqual(manager.name, "apt")


class TestRefindInstallAvailable(unittest.TestCase):
    def test_true_when_present(self):
        self.assertTrue(
            system_mod.is_refind_install_available(which_fn=_fake_which({"refind-install"}))
        )

    def test_false_when_absent(self):
        self.assertFalse(system_mod.is_refind_install_available(which_fn=_fake_which(set())))


class TestInstallPackage(unittest.TestCase):
    def test_success_does_not_raise(self):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        system_mod.install_package(manager, run_fn=fake_run)

    def test_failure_raises_bootstrap_error(self):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=1, stdout="", stderr="no internet")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        with self.assertRaises(system_mod.BootstrapError):
            system_mod.install_package(manager, run_fn=fake_run)


class TestRunRefindInstall(unittest.TestCase):
    def test_success_returns_stdout(self):
        def fake_run(cmd, capture_output, text):
            self.assertEqual(cmd, ["refind-install"])
            return SimpleNamespace(returncode=0, stdout="installed!", stderr="")

        output = system_mod.run_refind_install(run_fn=fake_run)
        self.assertEqual(output, "installed!")

    def test_failure_raises_bootstrap_error(self):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        with self.assertRaises(system_mod.BootstrapError):
            system_mod.run_refind_install(run_fn=fake_run)


class TestIsRoot(unittest.TestCase):
    def test_returns_bool(self):
        self.assertIsInstance(system_mod.is_root(), bool)


class TestVersionTuple(unittest.TestCase):
    def test_orders_numerically_not_lexically(self):
        self.assertGreater(system_mod.version_tuple("0.14.10"), system_mod.version_tuple("0.14.2"))

    def test_equal_versions_compare_equal(self):
        self.assertEqual(system_mod.version_tuple("0.14.1"), system_mod.version_tuple("0.14.1"))


class TestGetInstalledRefindVersion(unittest.TestCase):
    def test_apt_style_strips_distro_revision(self):
        def fake_run(cmd, capture_output, text):
            self.assertEqual(cmd, ["dpkg-query", "-W", "-f=${Version}\n", "refind"])
            return SimpleNamespace(returncode=0, stdout="0.14.2-2.1\n", stderr="")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        self.assertEqual(system_mod.get_installed_refind_version(manager, run_fn=fake_run), "0.14.2")

    def test_pacman_style_parses_second_token(self):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=0, stdout="refind 0.14.1-1\n", stderr="")

        manager = system_mod.PackageManagerInfo("pacman", ["pacman", "-S", "--noconfirm", "refind"])
        self.assertEqual(system_mod.get_installed_refind_version(manager, run_fn=fake_run), "0.14.1")

    def test_returns_none_when_not_installed(self):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=1, stdout="", stderr="not installed")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        self.assertIsNone(system_mod.get_installed_refind_version(manager, run_fn=fake_run))


class TestFindAvailableVersion(unittest.TestCase):
    def test_apt_madison_format(self):
        madison_output = (
            " refind | 0.14.1-1 | http://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n"
            " refind | 0.14.2-2.1 | http://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n"
        )

        def fake_run(cmd, capture_output, text):
            self.assertEqual(cmd, ["apt-cache", "madison", "refind"])
            return SimpleNamespace(returncode=0, stdout=madison_output, stderr="")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        self.assertEqual(system_mod.find_available_version(manager, "0.14.1", run_fn=fake_run), "0.14.1-1")

    def test_returns_none_when_target_not_listed(self):
        madison_output = " refind | 0.14.2-2.1 | http://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n"

        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=0, stdout=madison_output, stderr="")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        self.assertIsNone(system_mod.find_available_version(manager, "0.14.1", run_fn=fake_run))

    def test_returns_none_when_manager_unsupported(self):
        def fake_run(cmd, capture_output, text):
            raise AssertionError("should not be called for unsupported managers")

        manager = system_mod.PackageManagerInfo("pacman", ["pacman", "-S", "--noconfirm", "refind"])
        self.assertIsNone(system_mod.find_available_version(manager, "0.14.1", run_fn=fake_run))


class TestPinRefindVersion(unittest.TestCase):
    def test_success_installs_exact_version_found_in_repo(self):
        madison_output = " refind | 0.14.1-1 | http://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n"
        calls = []

        def fake_run(cmd, capture_output, text):
            calls.append(cmd)
            if cmd[:2] == ["apt-cache", "madison"]:
                return SimpleNamespace(returncode=0, stdout=madison_output, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        result = system_mod.pin_refind_version(manager, target="0.14.1", run_fn=fake_run)
        self.assertEqual(result, "0.14.1-1")
        self.assertIn(["apt-get", "install", "-y", "--allow-downgrades", "refind=0.14.1-1"], calls)

    def test_raises_when_target_not_available_in_repo(self):
        madison_output = " refind | 0.14.2-2.1 | http://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n"

        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=0, stdout=madison_output, stderr="")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        with self.assertRaises(system_mod.BootstrapError):
            system_mod.pin_refind_version(manager, target="0.14.1", run_fn=fake_run)

    def test_raises_when_manager_unsupported_for_pinning(self):
        manager = system_mod.PackageManagerInfo("pacman", ["pacman", "-S", "--noconfirm", "refind"])
        with self.assertRaises(system_mod.BootstrapError):
            system_mod.pin_refind_version(manager, target="0.14.1", run_fn=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))

    def test_raises_when_install_command_fails(self):
        madison_output = " refind | 0.14.1-1 | http://archive.ubuntu.com/ubuntu noble/universe amd64 Packages\n"

        def fake_run(cmd, capture_output, text):
            if cmd[:2] == ["apt-cache", "madison"]:
                return SimpleNamespace(returncode=0, stdout=madison_output, stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="dpkg lock held")

        manager = system_mod.PackageManagerInfo("apt", ["apt-get", "install", "-y", "refind"])
        with self.assertRaises(system_mod.BootstrapError):
            system_mod.pin_refind_version(manager, target="0.14.1", run_fn=fake_run)


if __name__ == "__main__":
    unittest.main()
