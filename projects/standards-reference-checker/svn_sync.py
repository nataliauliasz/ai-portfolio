import os
import subprocess
import shutil
from datetime import datetime
from typing import Optional


def resolve_svn_bin(pref: Optional[str] = None) -> str:
    """Zwraca ścieżkę do svn.exe (ENV/param, PATH lub popularne lokalizacje TortoiseSVN)."""
    if pref and os.path.exists(pref):
        return pref
    found = shutil.which("svn")
    if found:
        return found
    for cand in (
        r"C:\Program Files\TortoiseSVN\bin\svn.exe",
        r"C:\Program Files (x86)\TortoiseSVN\bin\svn.exe",
    ):
        if os.path.exists(cand):
            return cand
    raise RuntimeError("Nie znaleziono svn.exe w PATH ani w standardowych lokalizacjach.")


def sync_svn_repo(local_path: str, repo_url: str, svn_bin: Optional[str] = None, username=None, password=None):
    """
    Utrzymuje lokalny mirror repozytorium SVN.
    Jeśli katalog nie istnieje -> svn checkout.
    Jeśli istnieje -> svn update.
    """
    svn_bin = resolve_svn_bin(svn_bin)

    os.makedirs(local_path, exist_ok=True)
    cmd = []

    if not os.path.exists(os.path.join(local_path, ".svn")):
        print(f"[{datetime.now()}] Tworzenie lokalnego mirrora: {local_path}")
        cmd = [svn_bin, "checkout", repo_url, local_path]
    else:
        print(f"[{datetime.now()}] Aktualizacja lokalnego mirrora: {local_path}")
        cmd = [svn_bin, "update", local_path]

    if username and password:
        cmd += ["--non-interactive", "--trust-server-cert", "--username", username, "--password", password]

    subprocess.run(cmd, check=True)
    print(f"[{datetime.now()}] Synchronizacja zakończona.")
