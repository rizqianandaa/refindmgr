import unittest
from types import SimpleNamespace

from refindtm import system as system_mod


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


if __name__ == "__main__":
    unittest.main()
