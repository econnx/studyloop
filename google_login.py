"""Moodle sign-in via Google in a separate Chrome window.

Catches the moodlemobile://token=… link before the browser hands it to the Moodle app
and prints it to stdout. Usage: python google_login.py <launch.php link>
"""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path(__file__).parent / "data" / "browser-profile"
TIMEOUT_S = 300
APP_LINK = re.compile(r"moodlemobile://token=[A-Za-z0-9+/=_-]+")


def main(launch_url: str):
    found = []

    def on_response(resp):
        loc = resp.headers.get("location", "")
        if loc.startswith("moodlemobile://"):
            found.append(loc)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ctx.on("response", on_response)
        page.goto(launch_url)
        for _ in range(TIMEOUT_S * 2):
            if found or page.is_closed():
                break
            try:
                m = APP_LINK.search(page.content())
                if m:
                    found.append(m.group(0))
                    break
                page.wait_for_timeout(500)
            except Exception:
                if page.is_closed():
                    break  # the user closed the window
                # the page is navigating
        try:
            ctx.close()
        except Exception:
            pass

    if not found:
        sys.exit("Sign-in not completed (the window was closed or 5 minutes passed)")
    print(found[0])


if __name__ == "__main__":
    main(sys.argv[1])
