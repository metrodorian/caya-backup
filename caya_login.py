#!/usr/bin/env python3
"""Meldet sich mit den Daten aus .env bei Caya an und speichert die Sitzung.

Die Sitzung landet in .auth/state.json (nur für den Nutzer lesbar). Alle
weiteren Skripte nutzen diese Sitzung und brauchen das Passwort nicht.

    .venv/bin/python caya_login.py          # ohne Fenster
    .venv/bin/python caya_login.py --show   # mit sichtbarem Browser
    .venv/bin/python caya_login.py --snap   # alle 300 ms Screenshot nach .auth/snaps/
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
APP = "https://app.caya.com"
AUTH = BASE / ".auth"
STATE = AUTH / "state.json"
SNAPS = AUTH / "snaps"
SNAP_INTERVAL = 0.3


def totp_code(secret: str) -> str:
    import base64, hashlib, hmac, struct, time

    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret) % 8))
    msg = struct.pack(">Q", int(time.time()) // 30)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


async def snapper(page):
    """Macht bis zum Abbruch alle SNAP_INTERVAL Sekunden einen Screenshot."""
    i = 0
    while True:
        i += 1
        try:
            await page.screenshot(path=str(SNAPS / f"{i:04d}.png"), timeout=2000)
        except Exception:
            pass  # z. B. während eines Seitenwechsels
        await asyncio.sleep(SNAP_INTERVAL)


async def fail(page, msg: str) -> int:
    await page.screenshot(path=str(AUTH / "login-fehler.png"))
    print(f"{msg}, Screenshot: .auth/login-fehler.png", file=sys.stderr)
    return 1


async def login(page, email: str, password: str) -> int:
    await page.goto(f"{APP}/login", wait_until="networkidle")

    # Cookie-Banner: nur notwendige Cookies
    reject = page.locator("#cookiescript_reject")
    if await reject.count() and await reject.is_visible():
        await reject.click()
        # Der Banner lädt die Seite danach teils neu -> abwarten
        await page.wait_for_timeout(1500)
        await page.wait_for_load_state("networkidle")

    email_in = page.get_by_test_id("login-form-input-email")
    pw_in = page.get_by_test_id("login-form-input-password")
    submit = page.get_by_test_id("login-form-submit-button")
    await email_in.wait_for(state="visible")
    # Tippen statt fill(), damit das React-Formular die Eingabe registriert
    for field, value in ((email_in, email), (pw_in, password)):
        await field.click()
        await field.press_sequentially(value, delay=20)
    if not await submit.is_enabled():
        return await fail(page, "Login-Knopf bleibt gesperrt")
    await submit.click()

    try:
        await page.wait_for_url(lambda u: "/login" not in u, timeout=20_000)
    except PWTimeout:
        secret = os.getenv("CAYA_TOTP_SECRET")
        code_input = page.locator("input[autocomplete='one-time-code'], input[name*='code' i]")
        if not (secret and await code_input.count()):
            return await fail(page, "Anmeldung fehlgeschlagen")
        await code_input.first.fill(totp_code(secret))
        await page.keyboard.press("Enter")
        await page.wait_for_url(lambda u: "/login" not in u, timeout=20_000)

    await page.wait_for_load_state("networkidle")
    await page.context.storage_state(path=str(STATE))
    STATE.chmod(0o600)
    print(f"Angemeldet, Sitzung gespeichert ({page.url})")
    return 0


async def main() -> int:
    load_dotenv(BASE / ".env")
    email, password = os.getenv("CAYA_EMAIL"), os.getenv("CAYA_PASSWORD")
    if not email or not password:
        print("CAYA_EMAIL/CAYA_PASSWORD fehlen in .env", file=sys.stderr)
        return 2

    AUTH.mkdir(mode=0o700, exist_ok=True)
    snap = "--snap" in sys.argv
    if snap:
        shutil.rmtree(SNAPS, ignore_errors=True)
        SNAPS.mkdir()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless="--show" not in sys.argv)
        ctx = await browser.new_context(locale="de-DE")
        page = await ctx.new_page()
        task = asyncio.create_task(snapper(page)) if snap else None
        try:
            return await login(page, email, password)
        finally:
            if task:
                task.cancel()
                print(f"Screenshots: {len(list(SNAPS.glob('*.png')))} in .auth/snaps/")
            await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
