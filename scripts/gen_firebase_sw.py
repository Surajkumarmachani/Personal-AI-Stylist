"""Regenerate web/public/firebase-messaging-sw.js from the repo-root .env.

WHY A GENERATOR AND NOT A COMMITTED FILE
----------------------------------------
A service worker cannot read `process.env`: the browser fetches it from the
origin root, outside the app bundle, and runs it when the page is closed. So
the Firebase config has to be literal text in the file — and a literal that
somebody maintains by hand is a literal that will disagree with `.env` the
first time a project is recreated.

The five values are public by design (they ship to every browser), which is
precisely why the SENDING credential is a different artefact living in
`secrets/` and never appearing here.

    python scripts/gen_firebase_sw.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "web/public/firebase-messaging-sw.js"
KEYS = (
    "NEXT_PUBLIC_FIREBASE_API_KEY",
    "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
    "NEXT_PUBLIC_FIREBASE_SENDER_ID",
    "NEXT_PUBLIC_FIREBASE_APP_ID",
)


def main() -> int:
    env = (ROOT / ".env").read_text() if (ROOT / ".env").exists() else ""
    values = {}
    for key in KEYS:
        match = re.search(rf"^{key}=(.*)$", env, re.M)
        values[key] = match.group(1).strip() if match else ""

    missing = [k for k, v in values.items() if not v]
    if missing:
        print(f"  missing from .env: {', '.join(missing)}")
        print("  the worker will be written with empty values and push will not register")

    if not OUT.exists():
        print(f"  {OUT} does not exist; run the generator that created it first")
        return 1

    text = OUT.read_text()
    for key, value in values.items():
        field = {
            "NEXT_PUBLIC_FIREBASE_API_KEY": "apiKey",
            "NEXT_PUBLIC_FIREBASE_PROJECT_ID": "projectId",
            "NEXT_PUBLIC_FIREBASE_SENDER_ID": "messagingSenderId",
            "NEXT_PUBLIC_FIREBASE_APP_ID": "appId",
        }[key]
        text = re.sub(rf'({field}: ")[^"]*(")', rf"\g<1>{value}\g<2>", text)
    OUT.write_text(text)
    print(f"  wrote {OUT}")
    return 0


sys.exit(main())
