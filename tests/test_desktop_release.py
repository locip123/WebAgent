from pathlib import Path

from browser_use.webretriever.desktop import release


def test_windows_x64_release_stages_and_verifies_a_sidecar_executable(tmp_path, monkeypatch):
	"""The public release-tool seam accepts a native Windows x64 payload."""
	monkeypatch.setattr(release.sys, "platform", "win32")
	monkeypatch.setattr(release.platform, "machine", lambda: "AMD64")
	monkeypatch.setattr(
		release,
		"_expected_playwright_browser_entries",
		lambda: (("chromium-123", "chromium_headless_shell-123", "ffmpeg-123"), "1.61.0"),
	)
	monkeypatch.setattr(release, "_desktop_app_build", lambda: "0.1.0")

	sidecar_dist = tmp_path / "sidecar-dist"
	sidecar_dist.mkdir()
	(sidecar_dist / "webretriever-sidecar.exe").write_bytes(b"windows sidecar")
	browsers = tmp_path / "playwright-browsers"
	for entry in ("chromium-123", "chromium_headless_shell-123", "ffmpeg-123"):
		(browser := browsers / entry).mkdir(parents=True)
		(browser / "payload.bin").write_bytes(entry.encode())

	bundle = tmp_path / "webretriever-windows-resources"
	staged = release.stage_bundle(
		sidecar_dist=sidecar_dist,
		playwright_browsers_dir=browsers,
		output_dir=bundle,
		target="x86_64-pc-windows-msvc",
		sidecar_build="sidecar-build",
		runner_build="runner-build",
	)

	assert staged["status"] == "staged"
	assert (bundle / "sidecar" / "webretriever-sidecar.exe").is_file()
	assert release.verify_bundle(bundle)["target"] == "x86_64-pc-windows-msvc"
