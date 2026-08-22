"""Run this ONCE on your own machine to capture a Fantrax session cookie.

    pip install selenium webdriver-manager
    python -m mtalga.login_helper

It opens a real Chrome window at fantrax.com. Log in yourself (the script
never sees your password — it just waits until you're logged in), then the
session cookies are saved to data/fantrax_cookies.json.

For GitHub Actions: copy that file's ENTIRE contents into a repo secret
named FANTRAX_COOKIES. When the session eventually expires (months), the
nightly sync will alert — just rerun this script and update the secret.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "fantrax_cookies.json"


def main() -> None:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--window-size=1200,900")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.get("https://www.fantrax.com/login")

    print("\nA Chrome window is open. Log in to Fantrax normally.")
    print("Waiting for login (checks every 3 seconds, Ctrl-C to abort)...")
    try:
        while True:
            time.sleep(3)
            cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            # Fantrax sets a JSESSIONID plus auth cookies once logged in; the
            # reliable signal is that the app redirects off the login page.
            if "login" not in driver.current_url and cookies:
                break
    finally:
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        driver.quit()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cookies, indent=2))
    print(f"\nSaved {len(cookies)} cookies to {OUT}")
    print("Local syncs will now work. For GitHub Actions, paste this file's")
    print("contents into the FANTRAX_COOKIES repository secret.")


if __name__ == "__main__":
    main()
