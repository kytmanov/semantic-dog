from __future__ import annotations

import re
import socket
import subprocess
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.fixtures.generators import make_minimal_jpeg, make_truncated_jpeg


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_http(port: int, timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server on port {port} did not start in time")


@pytest.mark.playwright
def test_user_can_add_second_root_then_run_scan_from_dashboard(tmp_path: Path):
    library_a = tmp_path / "library-a"
    library_b = tmp_path / "library-b"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    library_a.mkdir()
    library_b.mkdir()
    state_dir.mkdir()
    log_dir.mkdir()

    make_minimal_jpeg(library_a / "good-a.jpg")
    make_truncated_jpeg(library_a / "bad-a.jpg")
    make_minimal_jpeg(library_b / "good-b.jpg")
    make_truncated_jpeg(library_b / "bad-b.jpg")

    port = _free_port()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "paths:",
                f"  - {library_a}",
                f"db_path: {state_dir / 'state.db'}",
                "workers: 1",
                "raw_workers: 1",
                "validation_timeout_s: 30",
                "force_recheck_days: 90",
                "trigger_cooldown_s: 0",
                f"http_port: {port}",
                "",
            ]
        )
    )

    log_file = log_dir / "server.log"
    server = subprocess.Popen(
        [
            "uv",
            "run",
            "sdog",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config",
            str(config_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=log_file.open("w"),
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        _wait_for_http(port)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()

            page.goto(f"http://127.0.0.1:{port}/config")
            scan_roots = page.get_by_role("textbox", name="/Volumes/home/Photos")
            expect(scan_roots).to_have_value(str(library_a))
            expect(page.locator("#schedule-preset")).to_have_value("0 2 * * *")
            expect(page.locator("#schedule-description")).to_have_text("Runs every day at 2:00 AM.")
            page.locator("#schedule-preset").select_option("__custom__")
            expect(page.locator("#schedule-preset")).to_have_value("__custom__")
            assert page.locator("#schedule-input").evaluate("(el) => el.readOnly") is False
            page.locator("#schedule-input").fill("15 3 * * 1")
            expect(page.locator("#schedule-input")).to_have_value("15 3 * * 1")
            expect(page.locator("#schedule-description")).to_have_text(
                "Custom cron schedule. Uses 5 fields: minute hour day-of-month month day-of-week."
            )
            page.locator("#schedule-preset").select_option("0 */6 * * *")
            expect(page.locator("#schedule-input")).to_have_value("0 */6 * * *")
            expect(page.locator("#schedule-description")).to_have_text("Runs every 6 hours at minute 00.")
            assert page.locator("#schedule-input").bounding_box()["width"] > 120
            scan_roots.fill(f"{library_a}\n{library_b}")
            page.get_by_role("button", name="Save Configuration").click()
            expect(page.locator("#config-feedback")).to_have_text("Saved successfully.")

            page.goto(f"http://127.0.0.1:{port}/dashboard")
            expect(page.locator("#banner-state")).to_have_text("Ready to scan")
            expect(page.locator("#next-scan-info")).to_contain_text("Next scan")
            expect(page.locator("#next-scan-relative")).to_have_text(re.compile(r"^in .+"))
            expect(page.locator("#scheduler-badge")).to_have_text("Active")
            expect(page.locator("#scheduler-cron")).to_have_text("0 */6 * * *")
            run_scan = page.get_by_role("button", name="Run Scan")
            expect(run_scan).to_be_enabled()
            run_scan.click()
            expect(page.get_by_role("button", name="Scan running…")).to_be_disabled()
            expect(page.locator("#files-indexed")).to_have_text("4", timeout=10000)
            expect(page.locator("#count-corrupt")).to_have_text("2", timeout=10000)
            expect(page.locator("#banner-state")).to_have_text("Issues found", timeout=10000)

            page.get_by_role("link", name="Issues").click()
            expect(page.get_by_role("cell", name=f"bad-b.jpg {library_b}/")).to_be_visible()

            page.get_by_role("link", name="History").click()
            expect(page.get_by_text(f"{library_a},{library_b}")).to_be_visible()

            page.goto(f"http://127.0.0.1:{port}/config")
            page.set_viewport_size({"width": 390, "height": 1000})
            expect(page.locator("html")).to_have_js_property("scrollWidth", 390)

            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
