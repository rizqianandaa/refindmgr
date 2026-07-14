import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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


class TestDownloadRefindDeb(unittest.TestCase):
    # Bug nyata yang dialami pengguna: repo apt Ubuntu-nya cuma punya 0.14.2,
    # jadi pin_refind_version() gagal terus -- refindmgr butuh jalur cadangan
    # yang mengunduh .deb resmi rEFInd langsung dari SourceForge.

    def test_success_returns_dest_path(self):
        def fake_run(cmd, capture_output, text):
            self.assertEqual(cmd[0], "curl")
            self.assertIn("0.14.1", cmd[-1])
            with open(cmd[cmd.index("-o") + 1], "w") as f:
                f.write("fake deb bytes")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "refind.deb")
            result = system_mod.download_refind_deb("0.14.1", dest, run_fn=fake_run)
            self.assertEqual(result, dest)
            self.assertTrue(os.path.exists(dest))

    def test_raises_when_curl_fails(self):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=1, stdout="", stderr="curl: (6) could not resolve host")

        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "refind.deb")
            with self.assertRaises(system_mod.BootstrapError):
                system_mod.download_refind_deb("0.14.1", dest, run_fn=fake_run)

    def test_raises_when_downloaded_file_is_empty(self):
        def fake_run(cmd, capture_output, text):
            # curl "succeeds" (returncode 0) but never actually writes the file
            # (e.g. SourceForge redirect edge case) -- must still be treated as failure.
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "refind.deb")
            with self.assertRaises(system_mod.BootstrapError):
                system_mod.download_refind_deb("0.14.1", dest, run_fn=fake_run)


class TestInstallDebFile(unittest.TestCase):
    def test_success_on_first_dpkg_call(self):
        calls = []

        def fake_run(cmd, capture_output, text):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        result = system_mod.install_deb_file("/tmp/refind.deb", run_fn=fake_run)
        self.assertEqual(result, "ok")
        self.assertEqual(calls, [["dpkg", "-i", "/tmp/refind.deb"]])

    def test_retries_after_fixing_dependencies(self):
        calls = []

        def fake_run(cmd, capture_output, text):
            calls.append(cmd)
            if cmd[:2] == ["dpkg", "-i"] and calls.count(cmd) == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="dependency problems")
            return SimpleNamespace(returncode=0, stdout="fixed", stderr="")

        result = system_mod.install_deb_file("/tmp/refind.deb", run_fn=fake_run)
        self.assertEqual(result, "fixed")
        self.assertIn(["apt-get", "install", "-f", "-y"], calls)

    def test_raises_when_retry_also_fails(self):
        def fake_run(cmd, capture_output, text):
            return SimpleNamespace(returncode=1, stdout="", stderr="still broken")

        with self.assertRaises(system_mod.BootstrapError):
            system_mod.install_deb_file("/tmp/refind.deb", run_fn=fake_run)


class TestFindBootKernelFiles(unittest.TestCase):
    def test_finds_vmlinuz_and_refind_linux_conf(self) -> None:
        with TemporaryDirectory() as tmp:
            boot = Path(tmp)
            (boot / "vmlinuz-5.15.0-91-generic").write_text("x")
            (boot / "refind_linux.conf").write_text("x")
            (boot / "initrd.img-5.15.0-91-generic").write_text("x")
            found = system_mod.find_boot_kernel_files(str(boot))
            self.assertIn("vmlinuz-5.15.0-91-generic", found)
            self.assertIn("refind_linux.conf", found)
            self.assertNotIn("initrd.img-5.15.0-91-generic", found)

    def test_returns_empty_when_boot_dir_missing(self) -> None:
        found = system_mod.find_boot_kernel_files("/no/such/boot/dir")
        self.assertEqual(found, [])


class TestListEspLoaderFiles(unittest.TestCase):
    def test_finds_uncovered_loader_outside_refind_and_tools_dirs(self):
        with TemporaryDirectory() as tmp:
            esp = Path(tmp)
            refind_dir = esp / "EFI" / "refind"
            refind_dir.mkdir(parents=True)
            (refind_dir / "refind_x64.efi").write_text("x")
            tools_dir = esp / "EFI" / "tools"
            tools_dir.mkdir(parents=True)
            (tools_dir / "shellx64.efi").write_text("x")
            ubuntu_dir = esp / "EFI" / "ubuntu"
            ubuntu_dir.mkdir(parents=True)
            (ubuntu_dir / "shimx64.efi").write_text("x")
            (ubuntu_dir / "grubx64.efi").write_text("x")
            boot_dir = esp / "EFI" / "BOOT"
            boot_dir.mkdir(parents=True)
            (boot_dir / "bootx64.efi").write_text("x")

            found = system_mod.list_esp_loader_files(refind_dir)

            # rEFInd's own binary and everything under EFI/tools must never
            # be reported -- rEFInd itself never scans those.
            self.assertNotIn("EFI/refind/refind_x64.efi", found)
            self.assertNotIn("EFI/tools/shellx64.efi", found)
            # Real OS loaders and the duplicate-prone ones must be found.
            self.assertIn("EFI/ubuntu/shimx64.efi", found)
            self.assertIn("EFI/ubuntu/grubx64.efi", found)
            self.assertIn("EFI/BOOT/bootx64.efi", found)

    def test_returns_empty_when_refind_dir_not_under_efi_folder(self):
        with TemporaryDirectory() as tmp:
            refind_dir = Path(tmp) / "somewhere" / "refind"
            refind_dir.mkdir(parents=True)
            self.assertEqual(system_mod.list_esp_loader_files(refind_dir), [])


if __name__ == "__main__":
    unittest.main()
