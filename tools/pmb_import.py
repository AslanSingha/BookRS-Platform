"""Load a UNIMARC ISO2709 file into PMB through its own import module.

PMB's importer is a chain of auto-submitting hidden forms
(import_records::get_hidden_form). This logs in, drops the file where
PMB's convert step would have left it, starts phase 2, and re-POSTs
each hidden form it gets back until PMB stops producing one.
Standard library only.
"""
from __future__ import annotations

import argparse
import html
import http.cookiejar
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

INPUT_RE = re.compile(r"<input\b([^>]*)>", re.I)
ATTR_RE = re.compile(r"""(\w+)\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+)""")
FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)


def attrs(s: str) -> dict:
    return {k.lower(): html.unescape(v.strip("\"'")) for k, v in ATTR_RE.findall(s)}


class Pmb:
    def __init__(self, base: str, debug_dir: str | None):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.debug_dir = debug_dir
        self.n = 0

    def post(self, path: str, data: dict) -> str:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(f"{self.base}/{path}", data=body,
                                     headers={"User-Agent": "bookrs-pmb-import"})
        with self.op.open(req, timeout=600) as r:
            text = r.read().decode("utf-8", errors="replace")
        self.n += 1
        if self.debug_dir:
            with open(os.path.join(self.debug_dir, f"{self.n:05d}.html"), "w", encoding="utf-8") as fh:
                fh.write(text)
        return text

    def login(self, user: str, pw: str, uf: str, pf: str) -> None:
        page = self.post("main.php", {uf: user, pf: pw})
        if re.search(rf"<input[^>]*name=['\"]?{pf}['\"]?", page, re.I):
            raise SystemExit("PMB login failed (login form came back). Check PMB_USER/PMB_PASS in .env")
        print("logged in")


def hidden_forms(page: str) -> list[tuple[dict, dict]]:
    """[(form attrs, {hidden name: value})] for forms posting to iimport_expl.php."""
    out = []
    for fa, body in FORM_RE.findall(page):
        f = attrs(fa)
        if "iimport_expl" not in f.get("action", ""):
            continue
        fields = {}
        for ia in INPUT_RE.findall(body):
            i = attrs(ia)
            if i.get("type", "").lower() == "hidden" and i.get("name"):
                fields[i["name"]] = i.get("value", "")
        if fields.get("action"):
            out.append((f, fields))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="UNIMARC ISO2709 file on the host")
    ap.add_argument("--base", default="http://localhost:8090/pmb")
    ap.add_argument("--container", default="pmb-docker-web-1")
    ap.add_argument("--user-field", default="user")
    ap.add_argument("--pass-field", default="password")
    ap.add_argument("--func", default="func_bdp.inc", help="import function, without .php")
    ap.add_argument("--location", default="pmb", help="PMB database name; the file must be unimarc_<db>.fic")
    ap.add_argument("--statutnot", default="1")
    ap.add_argument("--max-steps", type=int, default=200000)
    ap.add_argument("--debug-dir", default="")
    a = ap.parse_args()

    user, pw = os.environ.get("PMB_USER", ""), os.environ.get("PMB_PASS", "")
    if not user or not pw:
        raise SystemExit("set PMB_USER and PMB_PASS (tools/env.sh reads .env)")
    if a.debug_dir:
        os.makedirs(a.debug_dir, exist_ok=True)

    if os.path.getsize(a.file) == 0:
        raise SystemExit(f"{a.file} is empty; regenerate it")
    dest = f"/var/www/html/pmb/admin/import/unimarc_{a.location}.fic"
    subprocess.run(["docker", "cp", a.file, f"{a.container}:{dest}"], check=True)
    subprocess.run(["docker", "exec", a.container, "chown", "www-data:www-data", dest], check=True)
    print(f"file in place: {dest} ({os.path.getsize(a.file) // 2**20} MB)")

    pmb = Pmb(a.base, a.debug_dir or None)
    pmb.login(user, pw, a.user_field, a.pass_field)

    page = pmb.post("admin/import/iimport_expl.php", {
        "categ": "import", "sub": "import", "action": "preload",
        "func_import": a.func, "file_submit": "",
        "isbn_mandatory": "0", "isbn_dedoublonnage": "1", "isbn_only": "1",
        "statutnot": a.statutnot, "link_generate": "0", "notice_replace_links": "0",
        "import_force_notice_is_new": "0", "authorities_notices": "0",
        "import_notice_existing_replace": "0", "upload": "1",
    })
    if re.search(r"name=['\"]isbn_mandatory['\"][^>]*type=['\"]radio", page, re.I) or \
       re.search(r"type=['\"]radio['\"][^>]*name=['\"]isbn_mandatory", page, re.I):
        raise SystemExit("PMB re-displayed the options form: the preload POST was not accepted "
                         "(run with --debug-dir data/ol/pmb-debug and read 00002.html)")

    t0 = time.time()
    steps = 0
    last = ""
    same = 0
    prev = None
    while True:
        forms = hidden_forms(page)
        if not forms:
            break
        fattrs, fields = forms[-1]
        steps += 1
        key = tuple(sorted(fields.items()))
        same = same + 1 if key == prev else 0
        prev = key
        if same >= 5:
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()
            raise SystemExit(f"stalled: PMB returned the same form 5 times. Page says: {text[-300:]}")
        if steps > a.max_steps:
            raise SystemExit("gave up: too many steps")
        stat = " ".join(f"{k}={fields[k]}" for k in
                        ("action", "noticenumber", "nbtot_notice", "notice_deja_presente", "notice_rejetee", "reste")
                        if k in fields)
        if stat != last and (steps % 20 == 1 or fields.get("action") != "import"):
            print(f"{time.strftime('%H:%M:%S')} step {steps}: {stat}", flush=True)
            last = stat
        page = pmb.post("admin/import/iimport_expl.php", fields)

    text = re.sub(r"<[^>]+>", " ", page)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    print(f"done in {steps} steps, {time.time() - t0:.0f}s")
    print("final page says:", text[-400:])


if __name__ == "__main__":
    main()
