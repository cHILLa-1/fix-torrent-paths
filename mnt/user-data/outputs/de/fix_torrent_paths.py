#!/usr/bin/env python3
"""
fix_torrent_paths.py

Prueft fuer jeden Torrent in qBittorrent, ob die Dateien im korrekten
Kategorie-Unterordner liegen (so wie in qBittorrent selbst unter
"Kategorien -> Speicherort" konfiguriert). Falls nicht:

  1. Die Dateien werden lokal auf dem Datentraeger vom falschen in den
     richtigen Kategorie-Ordner verschoben (per Python, nicht ueber die
     qBittorrent-API - siehe Docker-Hinweis unten).
     - Existiert im Ziel bereits eine gleichnamige Datei, gewinnt die
       GROESSERE Datei: die kleinere wird ueberschrieben/verworfen.
  2. Automatic Torrent Management (AutoTMM) fuer den Torrent temporaer
     deaktivieren (verhindert den qBittorrent-internen Race, der einen
     API-Move sonst sofort wieder storniert, siehe qbittorrent.log:
     "Torrent move canceled" gefolgt von "Failed to enqueue torrent move ...
     both paths point to the same location").
  3. setLocation aufrufen, damit qBittorrent seine interne Pfad-Verwaltung
     auf den neuen (jetzt bereits korrekten) Ort aktualisiert.
  4. Torrent neu pruefen (recheck).
  5. AutoTMM optional wieder aktivieren (nachdem alles sicher fertig ist).

WICHTIG - qBittorrent laeuft als Docker-Container:
  Die Pfade, die die qBittorrent-API liefert (z.B. "/data/torrents/..."),
  sind die Pfade INNERHALB des Containers. Wenn dieses Script auf dem
  Host (oder in einem anderen Container) laeuft, sind das NICHT dieselben
  Pfade wie auf dem lokalen Dateisystem. Deshalb gibt es die Optionen
  --container-root und --local-root: damit wird der von qBittorrent
  gemeldete Pfad-Praefix auf den lokal tatsaechlich erreichbaren Pfad
  umgerechnet, bevor das Script selbst Dateien anfasst.

  Beispiel (passend zu einem Setup wie deinem qbit_manage-Mapping):
      --container-root /data/torrents
      --local-root /mnt/user/share_media/torrents

  MEHRERE Mappings: Manche Setups haben mehr als eine Docker-Mount-Wurzel
  (z.B. /data/torrents fuer normale Downloads UND /data/cross-seed fuer
  Cross-Seed-Links). Dafuer gibt es --root-map, wiederholbar oder
  kommagetrennt, Format container_pfad=lokaler_pfad:
      --root-map /data/torrents=/mnt/user/share_media/torrents \\
      --root-map /data/cross-seed=/mnt/user/share_media/cross-seed
  oder als eine Umgebungsvariable QBIT_ROOT_MAP (kommagetrennt):
      QBIT_ROOT_MAP="/data/torrents=/mnt/.../torrents,/data/cross-seed=/mnt/.../cross-seed"
  --container-root/--local-root werden weiterhin unterstuetzt und einfach
  als zusaetzliches Mapping mit aufgenommen.

  Wird gar kein Mapping angegeben, geht das Script davon aus, dass die von
  der API gemeldeten Pfade 1:1 lokal erreichbar sind (z.B. weil das Script
  selbst im selben Container/Mount-Namespace wie qBittorrent laeuft).

Das Script spricht fuer alle Torrent-Steuerbefehle mit der qBittorrent
WebAPI, fasst fuer den eigentlichen Datei-Merge aber direkt das lokale
Dateisystem an (dafuer muss der --local-root fuer dieses Script lesbar
UND beschreibbar gemountet sein).

Nutzung:
    python3 fix_torrent_paths.py --host 192.168.178.22:8082 --user admin --password 'xxx' \\
        --container-root /data/torrents --local-root /mnt/user/share_media/torrents --dry-run

    python3 fix_torrent_paths.py --host 192.168.178.22:8082 --user admin --password 'xxx' \\
        --container-root /data/torrents --local-root /mnt/user/share_media/torrents

Alternativ koennen Host/User/Passwort/Pfade auch ueber Umgebungsvariablen
gesetzt werden (praktisch, um das Passwort nicht in der Shell-History zu haben):
    export QBIT_HOST=192.168.178.22:8082
    export QBIT_USER=admin
    export QBIT_PASS='xxx'
    export QBIT_CONTAINER_ROOT=/data/torrents
    export QBIT_LOCAL_ROOT=/mnt/user/share_media/torrents
    python3 fix_torrent_paths.py --dry-run

Empfehlung: IMMER zuerst mit --dry-run laufen lassen und die Ausgabe pruefen,
bevor scharf verschoben wird.
"""

import argparse
import os
import shutil
import sys
import time
import logging
from datetime import datetime

try:
    import requests
except ImportError:
    print("Das Paket 'requests' fehlt. Installieren mit: pip install requests --break-system-packages")
    sys.exit(1)


# --------------------------------------------------------------------------
# Konfiguration / CLI
# --------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Findet Torrents, deren Dateien nicht im konfigurierten "
                     "Kategorie-Unterordner liegen, verschiebt sie dorthin und "
                     "stoesst einen Recheck an."
    )
    p.add_argument("--host", help="qBittorrent WebUI host:port, z.B. 192.168.178.22:8082 "
                                   "(alternativ: Umgebungsvariable QBIT_HOST)")
    p.add_argument("--user", help="qBittorrent WebUI Benutzername (alternativ: QBIT_USER)")
    p.add_argument("--password", dest="password", help="qBittorrent WebUI Passwort (alternativ: QBIT_PASS)")
    p.add_argument("--https", action="store_true", help="https statt http fuer die WebUI verwenden")

    p.add_argument("--dry-run", action="store_true",
                    help="Nur anzeigen, was gemacht wuerde, nichts wirklich veraendern")
    p.add_argument("--categories", help="Kommagetrennte Liste von Kategorien, auf die beschraenkt werden soll "
                                         "(Default: alle Kategorien mit gesetztem Speicherort)")
    p.add_argument("--exclude-categories", default="",
                    help="Kommagetrennte Liste von Kategorien, die NIE angefasst werden sollen")
    p.add_argument("--skip-source-prefix", action="append", default=None,
                    help="Torrents, deren AKTUELLER save_path (von der API, Container-Pfad) mit "
                         "diesem Praefix beginnt, werden komplett uebersprungen (kein Merge-Versuch, "
                         "keine Warnung) - z.B. --skip-source-prefix /data/cross-seed, um Cross-Seed-"
                         "Torrents nie anzufassen. Kann mehrfach angegeben werden, alternativ/"
                         "kommagetrennt: Umgebungsvariable QBIT_SKIP_SOURCE_PREFIXES.")

    p.add_argument("--no-recheck", action="store_true", help="Nach dem Move keinen Recheck ausloesen")
    p.add_argument("--no-reenable-autotmm", action="store_true",
                    help="AutoTMM nach dem Move NICHT wieder aktivieren (bleibt auf 'manuell')")
    p.add_argument("--no-pause-all-during-run", action="store_true",
                    help="Nicht ALLE Torrents in qBittorrent pausieren, waehrend das Script "
                         "Dateien verschiebt/rechecked. Standardmaessig werden (im scharfen Modus, "
                         "nicht im --dry-run) VOR dem ersten Merge alle Torrents pausiert und nach "
                         "Abschluss aller Moves+Rechecks wieder gestartet - auch bei einem Fehler "
                         "oder Abbruch (try/finally), um Schreibkonflikte waehrend der Datei-Merges "
                         "zu vermeiden.")
    p.add_argument("--no-scan-filesystem", action="store_true",
                    help="Deaktiviert den zusaetzlichen Dateisystem-Scan der Kategorie-Ordner. "
                         "Standardmaessig AKTIV: prueft physisch, ob Torrent-Inhalte im Ordner "
                         "einer anderen Kategorie liegen als der, die dem Torrent in qBittorrent "
                         "zugeordnet ist - auch wenn der von der API gemeldete save_path bereits "
                         "(faelschlicherweise) korrekt aussieht, z.B. weil ein frueherer Move von "
                         "qBittorrent selbst nicht sauber durchgefuehrt wurde.")
    p.add_argument("--extra-scan-dir", action="append", default=None,
                    help="Zusaetzlicher LOKALER Ordner (kein Container-Pfad, direkt wie auf der "
                         "Platte erreichbar), der NICHT selbst eine gueltige Kategorie ist, aber "
                         "auf Torrent-Inhalte durchsucht werden soll, z.B. "
                         "--extra-scan-dir /srv/mergerfs/config/share_media/torrents/orphaned_data. "
                         "Alles darin, was per Name zu einem bekannten Torrent passt, wird IMMER "
                         "als falsch platziert gewertet und in den passenden Kategorie-Ordner "
                         "verschoben (groessere Datei gewinnt bei Konflikt) und danach gerecheckt. "
                         "Kann mehrfach angegeben werden, alternativ/kommagetrennt: "
                         "Umgebungsvariable QBIT_EXTRA_SCAN_DIRS.")

    p.add_argument("--poll-interval", type=float, default=5.0, help="Sekunden zwischen Move-Status-Checks")
    p.add_argument("--poll-timeout", type=float, default=600.0, help="Max. Sekunden, die auf einen Move gewartet wird")

    p.add_argument("--batch-size", type=int, default=25,
                    help="Max. Anzahl Torrents pro API-Aufruf/Batch (grosse Kategorien werden aufgeteilt)")

    p.add_argument("--container-root", default=None,
                    help="Pfad-Praefix, wie ihn qBittorrent (im Docker-Container) meldet, "
                         "z.B. /data/torrents (alternativ: QBIT_CONTAINER_ROOT). "
                         "Wird zusaetzlich zu --root-map als ein Mapping verwendet.")
    p.add_argument("--local-root", default=None,
                    help="Derselbe Ort, wie er fuer DIESES Script lokal erreichbar ist, "
                         "z.B. /mnt/user/share_media/torrents (alternativ: QBIT_LOCAL_ROOT). "
                         "Wenn nicht gesetzt, werden die von der API gemeldeten Pfade 1:1 "
                         "als lokale Pfade verwendet.")
    p.add_argument("--root-map", action="append", default=None,
                    help="Weiteres Container->Lokal Pfad-Mapping im Format "
                         "container_pfad=lokaler_pfad, z.B. /data/cross-seed=/mnt/user/share_media/cross-seed. "
                         "Kann mehrfach angegeben werden (fuer mehrere Docker-Mount-Wurzeln). "
                         "Alternativ/kommagetrennt: Umgebungsvariable QBIT_ROOT_MAP.")

    p.add_argument("--log-file", default="fix_torrent_paths.log", help="Pfad zur Log-Datei")
    p.add_argument("-v", "--verbose", action="store_true", help="Ausfuehrlichere Konsolen-Ausgabe")

    return p.parse_args()


def _clean(value):
    """
    Bereinigt einen aus CLI/ENV/.env stammenden Wert von typischen
    Stoerenfrieden: fuehrende/folgende Whitespaces, Windows-Zeilenenden
    (\\r), sowie umschliessende einfache/doppelte Anfuehrungszeichen
    (falls die .env-Datei die Werte in Quotes gesetzt hat).
    """
    if value is None:
        return value
    v = value.strip().strip("\r").strip("\n")
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1].strip()
    return v


def resolve_credentials(args):
    """CLI-Argumente haben Vorrang, sonst Umgebungsvariablen QBIT_HOST/QBIT_USER/QBIT_PASS."""
    host = _clean(args.host or os.environ.get("QBIT_HOST"))
    user = _clean(args.user or os.environ.get("QBIT_USER"))
    password = _clean(args.password or os.environ.get("QBIT_PASS"))

    if not host:
        print("Kein Host angegeben. Entweder --host verwenden oder die Umgebungsvariable QBIT_HOST setzen.")
        sys.exit(1)

    # Falls versehentlich ein Schema oder ein trailing Slash mit angegeben wurde
    host = host.replace("http://", "").replace("https://", "").rstrip("/")

    return host, user, password


def resolve_path_mapping(args):
    """
    Baut die Liste aller Container->Lokal Pfad-Mappings.
    Quellen (alle werden kombiniert):
      - --container-root/--local-root (bzw. QBIT_CONTAINER_ROOT/QBIT_LOCAL_ROOT): ein Paar
      - --root-map (mehrfach) bzw. QBIT_ROOT_MAP (kommagetrennt): container=lokal
    Gibt eine Liste von (container_root, local_root)-Tupeln zurueck, sortiert nach
    Laenge des container_root absteigend (laengster/spezifischster Pfad zuerst).
    """
    mappings = []

    container_root = _clean(args.container_root or os.environ.get("QBIT_CONTAINER_ROOT"))
    local_root = _clean(args.local_root or os.environ.get("QBIT_LOCAL_ROOT"))
    if container_root and local_root:
        mappings.append((norm(container_root), local_root.rstrip("/")))

    raw_entries = list(args.root_map or [])
    env_map = os.environ.get("QBIT_ROOT_MAP")
    if env_map:
        raw_entries.extend(env_map.split(","))

    for entry in raw_entries:
        entry = _clean(entry)
        if not entry:
            continue
        if "=" not in entry:
            print(f"Ungueltiges --root-map/QBIT_ROOT_MAP Format (erwartet container=lokal): {entry!r}")
            sys.exit(1)
        c, l = entry.split("=", 1)
        c, l = _clean(c), _clean(l)
        if not c or not l:
            print(f"Ungueltiges --root-map/QBIT_ROOT_MAP Format (erwartet container=lokal): {entry!r}")
            sys.exit(1)
        mappings.append((norm(c), l.rstrip("/")))

    # Laengste (spezifischste) Container-Pfade zuerst pruefen, damit z.B.
    # /data/cross-seed nicht versehentlich von einem allgemeineren /data
    # Mapping verschluckt wird.
    mappings.sort(key=lambda pair: len(pair[0]), reverse=True)
    return mappings


def resolve_skip_prefixes(args):
    """
    Liste normalisierter Pfad-Praefixe (Container-Pfade), fuer die Torrents
    komplett uebersprungen werden sollen, egal welcher Kategorie sie
    zugeordnet sind - z.B. Cross-Seed-Ordner, die man nie anfassen will.
    """
    raw = list(args.skip_source_prefix or [])
    env_val = os.environ.get("QBIT_SKIP_SOURCE_PREFIXES")
    if env_val:
        raw.extend(env_val.split(","))
    prefixes = [norm(_clean(p)) for p in raw if _clean(p)]
    return prefixes


def matches_skip_prefix(save_path, skip_prefixes):
    p = norm(save_path)
    for prefix in skip_prefixes:
        if p == prefix or p.startswith(prefix + os.sep):
            return True
    return False


def resolve_extra_scan_dirs(args):
    """
    Liste zusaetzlicher LOKALER Ordner (bereits so wie auf der Platte
    erreichbar, keine Container-Pfad-Uebersetzung), die auf Torrent-Inhalte
    durchsucht werden sollen, obwohl sie selbst keine gueltige Kategorie
    sind (z.B. orphaned_data). Gibt ein dict label -> lokaler_pfad zurueck,
    Label = Ordnername (fuer Log-Ausgaben).
    """
    raw = list(args.extra_scan_dir or [])
    env_val = os.environ.get("QBIT_EXTRA_SCAN_DIRS")
    if env_val:
        raw.extend(env_val.split(","))

    result = {}
    for entry in raw:
        entry = _clean(entry)
        if not entry:
            continue
        local_dir = entry.rstrip("/")
        label = os.path.basename(local_dir) or local_dir
        result[label] = local_dir
    return result


# --------------------------------------------------------------------------
# qBittorrent WebAPI Client (minimal, nur die benoetigten Endpunkte)
# --------------------------------------------------------------------------

class QbitClient:
    def __init__(self, host, user, password, use_https=False, timeout=30):
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}"
        self.session = requests.Session()
        self.timeout = timeout
        self._login(user, password)

    def _login(self, user, password):
        url = f"{self.base_url}/api/v2/auth/login"
        resp = self.session.post(
            url,
            data={"username": user or "", "password": password or ""},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        if resp.text.strip() != "Ok.":
            raise RuntimeError(
                f"Login bei qBittorrent fehlgeschlagen (Antwort: {resp.text!r}). "
                f"Host/User/Passwort pruefen, oder ob die WebUI-Auth-Whitelist "
                f"die Quell-IP evtl. ohne Login durchlaesst (dann sollte 'Ok.' kommen)."
            )

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def _post(self, path, data=None):
        url = f"{self.base_url}{path}"
        resp = self.session.post(url, data=data, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def get_categories(self):
        """-> dict: {category_name: {'name':..., 'savePath':...}}"""
        return self._get("/api/v2/torrents/categories").json()

    def get_torrents(self):
        """-> list of torrent dicts (hash, name, category, save_path, state, auto_tmm, ...)"""
        return self._get("/api/v2/torrents/info").json()

    def set_auto_management(self, hashes, enable):
        self._post("/api/v2/torrents/setAutoManagement", {
            "hashes": "|".join(hashes),
            "enable": "true" if enable else "false",
        })

    def pause_all(self):
        """
        Pausiert ALLE Torrents in qBittorrent (hashes=all).
        Aeltere qBittorrent-WebUI-API-Versionen nutzen dafuer den Endpunkt
        '/torrents/pause', neuere (ab WebUI-API 2.11, qBittorrent >= 4.6)
        haben ihn zu '/torrents/stop' umbenannt. Wir versuchen zuerst den
        alten Namen und fallen bei 404 auf den neuen zurueck.
        """
        try:
            self._post("/api/v2/torrents/pause", {"hashes": "all"})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self._post("/api/v2/torrents/stop", {"hashes": "all"})
            else:
                raise

    def resume_all(self):
        """Startet ALLE Torrents in qBittorrent wieder (hashes=all), siehe pause_all()."""
        try:
            self._post("/api/v2/torrents/resume", {"hashes": "all"})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self._post("/api/v2/torrents/start", {"hashes": "all"})
            else:
                raise

    def set_location(self, hashes, location):
        self._post("/api/v2/torrents/setLocation", {
            "hashes": "|".join(hashes),
            "location": location,
        })

    def recheck(self, hashes):
        self._post("/api/v2/torrents/recheck", {
            "hashes": "|".join(hashes),
        })

    def get_torrents_by_hashes(self, hashes):
        info = self.get_torrents()
        wanted = set(hashes)
        return [t for t in info if t["hash"] in wanted]


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def norm(path):
    if not path:
        return ""
    return os.path.normpath(path).rstrip("/")


def build_reverse_category_lookup(categories):
    """
    Baut eine Zuordnung normalisierter Pfad -> Kategoriename, damit wir
    erkennen koennen, in welcher (falschen) Kategorie ein Torrent aktuell
    physisch liegt - nicht nur DASS der Pfad falsch ist.
    """
    lookup = {}
    for name, info in categories.items():
        sp = norm(info.get("savePath"))
        if sp:
            lookup[sp] = name
    return lookup


def guess_current_category(actual_path, reverse_lookup):
    """
    Ordnet einen tatsaechlichen save_path einer bekannten Kategorie zu,
    falls er exakt (oder als Praefix) zu deren Speicherort passt.
    Gibt None zurueck, wenn der Pfad zu keiner bekannten Kategorie passt
    (z.B. weil die Datei direkt im Root-Verzeichnis liegt).
    """
    actual = norm(actual_path)
    if actual in reverse_lookup:
        return reverse_lookup[actual]
    # Fallback: Praefix-Match, falls der Torrent-Ordner selbst noch
    # unter dem Kategorie-Pfad haengt (z.B. verschachtelte Unterordner)
    for path, name in reverse_lookup.items():
        if path and (actual == path or actual.startswith(path + os.sep)):
            return name
    return None


def build_content_name_index(torrents):
    """
    Baut einen Index Dateiname/Ordnername -> Liste von Torrents, damit wir
    einen physisch auf der Platte gefundenen Ordner/Datei zurueck auf einen
    Torrent in qBittorrent mappen koennen (Name-Match). Nutzt sowohl den
    Torrent-Namen als auch den Basename von content_path, da beide je nach
    Torrent-Struktur der tatsaechliche oberste Ordnername auf der Platte
    sein koennen.
    """
    index = {}
    for t in torrents:
        keys = set()
        name = t.get("name")
        if name:
            keys.add(name)
        content_path = t.get("content_path")
        if content_path:
            keys.add(os.path.basename(content_path.rstrip("/\\")))
        for k in keys:
            if not k:
                continue
            index.setdefault(k, []).append(t)
    return index


def build_category_local_dirs(categories, mappings):
    """Kategorie -> lokal erreichbarer Ordner (savePath via mappings uebersetzt)."""
    result = {}
    for cat_name, info in categories.items():
        sp = norm(info.get("savePath"))
        if not sp:
            continue
        result[cat_name] = translate_path(sp, mappings)
    return result


def scan_physical_mismatches(category_local_dirs, extra_scan_dirs, content_index, skip_prefixes,
                              logger, scan_category_dirs=True):
    """
    Durchsucht physisch auf der Platte:
      1) JEDEN bekannten Kategorie-Ordner (oberste Ebene = Torrent-Wurzel-
         ordner/-Dateien) und meldet einen Mismatch, wenn ein gefundener
         Torrent-Inhalt eigentlich in eine ANDERE Kategorie gehoert.
         (nur wenn scan_category_dirs=True)
      2) Zusaetzliche, NICHT an eine Kategorie gebundene "Extra-Ordner"
         (z.B. orphaned_data) - alles, was dort gefunden und einem
         bekannten Torrent zugeordnet werden kann, gilt IMMER als Mismatch
         (da diese Ordner selbst nie ein gueltiges Torrent-Ziel sind) und
         wird in den zur Torrent-Kategorie gehoerenden Ordner verschoben.
    category_local_dirs wird in JEDEM Fall benoetigt, um fuer einen
    gefundenen Torrent den korrekten Ziel-Ordner nachzuschlagen - auch wenn
    scan_category_dirs=False ist (dann werden nur die category_local_dirs
    NICHT selbst durchsucht, aber weiterhin als Ziel-Nachschlagewerk
    verwendet).
    In beiden Faellen unabhaengig davon, was die API fuer den Torrent als
    save_path meldet (das Feld wird von qBittorrent nicht immer
    zuverlaessig aktualisiert, wenn ein Move fehlschlaegt/abgebrochen wird).

    Gibt zurueck: dict cat_name -> Liste von Torrent-Dicts (Kopien, mit
    zusaetzlichem Key '_physical_src' = tatsaechlich gefundener lokaler Pfad
    und '_physical_found_in_category' = Label des Fund-Ordners).
    """
    found = {}
    seen_hashes = set()

    def process_dir(local_dir, dir_label, dir_is_category):
        if not local_dir or not os.path.isdir(local_dir):
            if local_dir:
                logger.warning(f"Scan-Ordner existiert nicht (uebersprungen): {local_dir}")
            return
        try:
            entries = os.listdir(local_dir)
        except OSError as e:
            logger.warning(f"Konnte Ordner nicht lesen: {local_dir} ({e})")
            return

        for entry in entries:
            full_path = os.path.join(local_dir, entry)
            candidates = content_index.get(entry)
            if not candidates:
                continue  # kein bekannter Torrent mit diesem Namen - unangetastet lassen

            for t in candidates:
                t_cat = t.get("category") or ""
                if not t_cat or t_cat not in category_local_dirs:
                    continue

                expected_local_dir = category_local_dirs[t_cat]
                if dir_is_category and norm(expected_local_dir) == norm(local_dir):
                    continue  # liegt schon im richtigen physischen Ordner

                if skip_prefixes and matches_skip_prefix(t.get("save_path"), skip_prefixes):
                    continue

                if t["hash"] in seen_hashes:
                    continue
                seen_hashes.add(t["hash"])

                t_copy = dict(t)
                t_copy["_physical_src"] = full_path
                t_copy["_physical_found_in_category"] = dir_label
                found.setdefault(t_cat, []).append(t_copy)

    if scan_category_dirs:
        for cat_name, local_dir in category_local_dirs.items():
            process_dir(local_dir, cat_name, dir_is_category=True)

    for label, local_dir in extra_scan_dirs.items():
        process_dir(local_dir, label, dir_is_category=False)

    return found


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def translate_path(container_path, mappings):
    """
    Uebersetzt einen von qBittorrent (im Docker-Container) gemeldeten Pfad
    in den fuer DIESES Script lokal erreichbaren Pfad, anhand der Liste von
    (container_root, local_root) Mappings (siehe resolve_path_mapping - schon
    nach Praefix-Laenge absteigend sortiert, d.h. das spezifischste Mapping
    gewinnt). Ist kein Mapping gesetzt oder passt keins, wird der Pfad
    unveraendert zurueckgegeben (Annahme: Script laeuft im selben
    Mount-Namespace wie qBittorrent).
    """
    if not container_path:
        return container_path
    if not mappings:
        return container_path

    p = norm(container_path)
    for c_root, local_root in mappings:
        if p == c_root or p.startswith(c_root + os.sep):
            rel = p[len(c_root):].lstrip(os.sep)
            return os.path.join(local_root, rel) if rel else local_root
    # Pfad liegt ausserhalb aller gemappten Bereiche - unveraendert zurueckgeben,
    # aber Aufrufer sollte das ggf. als Warnung behandeln.
    return container_path


def human_size(num_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}PB"


def merge_file(src_file, dst_file, logger, dry_run):
    """
    Verschiebt eine einzelne Datei von src_file nach dst_file.
    Existiert dst_file bereits, gewinnt die GROESSERE Datei:
      - Quelle groesser  -> Ziel wird ueberschrieben
      - Ziel groesser/gleich -> Quelle wird verworfen (geloescht)
    Gibt True zurueck, wenn die Datei danach an dst_file liegt (verschoben
    oder war schon dort korrekt), False bei Fehler.
    """
    try:
        if os.path.exists(dst_file):
            src_size = os.path.getsize(src_file)
            dst_size = os.path.getsize(dst_file)
            if src_size > dst_size:
                logger.info(f"        Konflikt: {os.path.basename(dst_file)} - "
                            f"Quelle groesser ({human_size(src_size)} > {human_size(dst_size)}) "
                            f"-> Ziel wird ueberschrieben")
                if not dry_run:
                    os.remove(dst_file)
                    shutil.move(src_file, dst_file)
            else:
                logger.info(f"        Konflikt: {os.path.basename(dst_file)} - "
                            f"Ziel groesser/gleich ({human_size(dst_size)} >= {human_size(src_size)}) "
                            f"-> Quelle wird verworfen")
                if not dry_run:
                    os.remove(src_file)
        else:
            logger.debug(f"        Verschiebe: {src_file} -> {dst_file}")
            if not dry_run:
                os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                shutil.move(src_file, dst_file)
        return True
    except OSError as e:
        logger.error(f"        Fehler bei {src_file} -> {dst_file}: {e}")
        return False


def cleanup_empty_dirs(root, logger, dry_run):
    """Entfernt nach dem Merge leere Verzeichnisse unterhalb (und inkl.) root."""
    if dry_run or not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                logger.debug(f"        Leeres Verzeichnis entfernt: {dirpath}")
        except OSError:
            pass


def merge_move(local_src, local_dst, logger, dry_run):
    """
    Verschiebt den kompletten Torrent-Inhalt (Datei oder Ordner) von
    local_src nach local_dst und loest dabei Namenskonflikte per
    "groessere Datei gewinnt" auf. Gibt (ok: bool, files_moved: int) zurueck.
    """
    if norm(local_src) == norm(local_dst):
        return True, 0

    if not os.path.exists(local_src):
        logger.warning(f"        Quelle existiert lokal nicht (mehr): {local_src} - ueberspringe Datei-Merge")
        return os.path.exists(local_dst), 0

    ok_all = True
    moved = 0

    if os.path.isfile(local_src):
        if not dry_run:
            os.makedirs(os.path.dirname(local_dst), exist_ok=True)
        ok = merge_file(local_src, local_dst, logger, dry_run)
        ok_all = ok_all and ok
        moved += 1 if ok else 0
    else:
        for dirpath, dirnames, filenames in os.walk(local_src):
            rel = os.path.relpath(dirpath, local_src)
            for fname in filenames:
                src_file = os.path.join(dirpath, fname)
                dst_file = os.path.join(local_dst, rel, fname) if rel != "." else os.path.join(local_dst, fname)
                ok = merge_file(src_file, dst_file, logger, dry_run)
                ok_all = ok_all and ok
                moved += 1 if ok else 0
        cleanup_empty_dirs(local_src, logger, dry_run)

    return ok_all, moved


def setup_logging(log_file, verbose):
    logger = logging.getLogger("fix_torrent_paths")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# --------------------------------------------------------------------------
# Hauptlogik
# --------------------------------------------------------------------------

def main():
    args = parse_args()
    logger = setup_logging(args.log_file, args.verbose)

    # --- Zugangsdaten ermitteln ---
    host, user, password = resolve_credentials(args)
    mappings = resolve_path_mapping(args)
    skip_prefixes = resolve_skip_prefixes(args)
    extra_scan_dirs = resolve_extra_scan_dirs(args)
    logger.debug(f"Bereinigter Host: {host!r}  User: {user!r}")

    if skip_prefixes:
        logger.info(f"Ignoriere Torrents mit aktuellem Speicherort unter: {', '.join(skip_prefixes)}")

    if extra_scan_dirs:
        logger.info(f"Zusaetzliche Scan-Ordner (werden auf bekannte Torrents durchsucht): "
                    f"{', '.join(extra_scan_dirs.values())}")

    if not mappings:
        logger.info("Kein --container-root/--local-root bzw. --root-map gesetzt: es wird "
                     "angenommen, dass die von qBittorrent gemeldeten Pfade 1:1 lokal fuer "
                     "dieses Script erreichbar sind. Das ist der korrekte/erwartete Fall, wenn "
                     "qBittorrent lokal im selben Mount-Namespace wie dieses Script laeuft "
                     "(kein Docker-Mapping noetig). Laeuft qBittorrent dagegen in Docker mit "
                     "ANDEREN Mounts als dieses Script, MUESSEN --container-root/--local-root "
                     "bzw. --root-map gesetzt werden, sonst werden falsche (nicht existierende) "
                     "Pfade angefasst!")
    else:
        for c_root, l_root in mappings:
            logger.debug(f"Pfad-Mapping: {c_root}  ->  {l_root}")

    only_categories = None
    if args.categories:
        only_categories = {c.strip() for c in args.categories.split(",") if c.strip()}
    exclude_categories = {c.strip() for c in args.exclude_categories.split(",") if c.strip()}

    def category_allowed(cat_name):
        if only_categories is not None and cat_name not in only_categories:
            return False
        if cat_name in exclude_categories:
            return False
        return True

    logger.info("=" * 70)
    logger.info(f"Start {'(DRY-RUN)' if args.dry_run else ''} - Host: {host}")
    logger.info("=" * 70)

    client = QbitClient(host, user, password, use_https=args.https)

    categories = client.get_categories()
    torrents = client.get_torrents()
    reverse_lookup = build_reverse_category_lookup(categories)

    logger.info(f"{len(torrents)} Torrents gefunden, {len(categories)} Kategorien in qBittorrent.")

    # Kategorie -> Liste betroffener Torrents (save_path stimmt nicht mit savePath ueberein)
    mismatches_by_cat = {}
    skipped_no_savepath = 0
    skipped_no_category = 0
    skipped_prefix = 0
    already_ok = 0

    for t in torrents:
        cat_name = t.get("category") or ""
        if not cat_name:
            skipped_no_category += 1
            continue

        if not category_allowed(cat_name):
            continue

        cat_info = categories.get(cat_name)
        if not cat_info:
            skipped_no_category += 1
            continue

        expected = norm(cat_info.get("savePath"))
        if not expected:
            skipped_no_savepath += 1
            continue

        actual = norm(t.get("save_path"))

        if actual == expected:
            already_ok += 1
            continue

        if skip_prefixes and matches_skip_prefix(actual, skip_prefixes):
            skipped_prefix += 1
            continue

        mismatches_by_cat.setdefault(cat_name, []).append(t)

    total_mismatched = sum(len(v) for v in mismatches_by_cat.values())

    logger.info("-" * 70)
    logger.info(f"Bereits korrekt platziert (laut API): {already_ok}")
    logger.info(f"Ohne Kategorie            : {skipped_no_category}")
    logger.info(f"Kategorie ohne Speicherort: {skipped_no_savepath}")
    logger.info(f"Ignoriert (Skip-Praefix)  : {skipped_prefix}")
    logger.info(f"Falsch platziert (laut API-save_path): {total_mismatched}")
    logger.info("-" * 70)

    # --------------------------------------------------------------------
    # Zusaetzlich: Dateisystem physisch scannen. qBittorrent aktualisiert
    # save_path nicht immer zuverlaessig, wenn ein interner Move fehlschlaegt
    # oder abgebrochen wird - dann sieht der Torrent laut API "korrekt"
    # platziert aus, obwohl die Dateien tatsaechlich noch im falschen
    # Kategorie-Ordner liegen. Das faengt dieser Scan zusaetzlich ab.
    # --------------------------------------------------------------------
    physical_extra_count = 0
    if not args.no_scan_filesystem or extra_scan_dirs:
        category_local_dirs = build_category_local_dirs(categories, mappings)
        content_index = build_content_name_index(torrents)
        physical_mismatches = scan_physical_mismatches(
            category_local_dirs, extra_scan_dirs, content_index, skip_prefixes, logger,
            scan_category_dirs=not args.no_scan_filesystem
        )
        for cat_name, extra_list in physical_mismatches.items():
            if not category_allowed(cat_name):
                continue
            existing_hashes = {t["hash"] for t in mismatches_by_cat.get(cat_name, [])}
            for t in extra_list:
                if t["hash"] in existing_hashes:
                    continue
                mismatches_by_cat.setdefault(cat_name, []).append(t)
                existing_hashes.add(t["hash"])
                physical_extra_count += 1

        logger.info(f"Zusaetzlich per Dateisystem-Scan gefunden (physisch falsch, "
                    f"API-save_path taeuschte 'korrekt' vor): {physical_extra_count}")
        logger.info("-" * 70)

    total_mismatched = sum(len(v) for v in mismatches_by_cat.values())

    if not args.no_scan_filesystem or extra_scan_dirs:
        logger.info(f"Falsch platziert insgesamt (API + Dateisystem-Scan): {total_mismatched}")
        logger.info("-" * 70)

    if not mismatches_by_cat:
        logger.info("Nichts zu tun. Fertig.")
        return

    # Uebersicht: welche "von -> nach"-Verschiebungen kommen wie oft vor
    move_pairs = {}
    for cat_name, cat_torrents in mismatches_by_cat.items():
        for t in cat_torrents:
            current_cat = guess_current_category(t.get("save_path"), reverse_lookup) or "(root/unbekannt)"
            key = (current_cat, cat_name)
            move_pairs[key] = move_pairs.get(key, 0) + 1

    logger.info("Uebersicht der noetigen Verschiebungen (von -> nach: Anzahl):")
    for (src, dst), count in sorted(move_pairs.items(), key=lambda x: -x[1]):
        logger.info(f"    {src}  ->  {dst}: {count}")
    logger.info("-" * 70)

    should_pause_all = (not args.dry_run) and (not args.no_pause_all_during_run)
    if should_pause_all:
        logger.info("Pausiere ALLE Torrents in qBittorrent, bevor Dateien verschoben werden ...")
        client.pause_all()

    try:
        for cat_name, cat_torrents in sorted(mismatches_by_cat.items()):
            expected = norm(categories[cat_name].get("savePath"))
            logger.info("")
            logger.info(f"### Kategorie '{cat_name}' -> Ziel: {expected}  ({len(cat_torrents)} Torrent(s) betroffen)")
            for t in cat_torrents:
                if "_physical_src" in t:
                    where = (f"PHYSISCH gefunden in Kategorie-Ordner '{t['_physical_found_in_category']}' "
                             f"({t['_physical_src']}) - API meldet save_path={t.get('save_path')} "
                             f"(sieht laut API evtl. faelschlich korrekt aus)")
                else:
                    current_cat = guess_current_category(t.get("save_path"), reverse_lookup)
                    if current_cat and current_cat != cat_name:
                        where = f"liegt aktuell in Kategorie '{current_cat}' ({t.get('save_path')})"
                    elif current_cat is None:
                        where = f"liegt aktuell in keinem bekannten Kategorie-Ordner ({t.get('save_path')})"
                    else:
                        where = f"aktuell: {t.get('save_path')}"
                logger.info(f"    - {t['name']}  [{where}] -> soll nach '{cat_name}'")

            if args.dry_run:
                # Im Dry-Run trotzdem den geplanten Datei-Merge inkl. Konflikt-
                # Erkennung anzeigen (nur Lesezugriffe, es wird nichts veraendert).
                for t in cat_torrents:
                    content_path = t.get("content_path") or t.get("save_path")
                    if "_physical_src" in t:
                        # Per Dateisystem-Scan gefunden: den tatsaechlich
                        # verifizierten physischen Pfad nehmen, NICHT den
                        # (moeglicherweise irrefuehrenden) API-content_path.
                        local_src = t["_physical_src"]
                    else:
                        local_src = translate_path(content_path, mappings)
                    local_dst = translate_path(
                        os.path.join(expected, os.path.basename(content_path)),
                        mappings
                    )
                    logger.debug(f"    [dry-run] Merge geplant: {local_src} -> {local_dst}")
                    merge_move(local_src, local_dst, logger, dry_run=True)
                continue

            for batch in chunked(cat_torrents, args.batch_size):
                hashes = [t["hash"] for t in batch]

                # 0) Dateien lokal bereits in den Zielordner verschieben/mergen,
                #    BEVOR qBittorrent ueberhaupt involviert wird. Konflikte
                #    (gleicher Dateiname im Ziel) werden per Groessenvergleich
                #    geloest: die groessere Datei bleibt erhalten.
                merge_failed = set()
                for t in batch:
                    content_path = t.get("content_path") or t.get("save_path")
                    if "_physical_src" in t:
                        local_src = t["_physical_src"]
                    else:
                        local_src = translate_path(content_path, mappings)
                    local_dst = translate_path(
                        os.path.join(expected, os.path.basename(content_path)),
                        mappings
                    )
                    logger.info(f"    Merge: {local_src} -> {local_dst}")
                    ok, moved = merge_move(local_src, local_dst, logger, dry_run=False)
                    if not ok:
                        merge_failed.add(t["hash"])
                        logger.warning(f"    Merge fuer '{t['name']}' unvollstaendig/fehlerhaft - "
                                        f"wird NICHT gerecheckt/reaktiviert, bitte manuell pruefen.")

                hashes = [h for h in hashes if h not in merge_failed]
                if not hashes:
                    continue

                # 1) AutoTMM deaktivieren, um den Recalculation-Race zu vermeiden
                logger.debug(f"    AutoTMM deaktivieren fuer {len(hashes)} Torrent(s)")
                client.set_auto_management(hashes, enable=False)
                time.sleep(1)

                # 2) qBittorrent ueber den neuen (jetzt bereits physisch korrekten)
                #    Speicherort informieren
                logger.info(f"    setLocation -> {expected} fuer {len(hashes)} Torrent(s)")
                client.set_location(hashes, expected)

                # 3) Warten bis der Move abgeschlossen ist
                deadline = time.time() + args.poll_timeout
                pending = set(hashes)
                moved_ok = []
                while pending and time.time() < deadline:
                    time.sleep(args.poll_interval)
                    current = {t["hash"]: t for t in client.get_torrents_by_hashes(list(pending))}
                    for h in list(pending):
                        info = current.get(h)
                        if info is None:
                            # Torrent nicht mehr gefunden (entfernt?) - aus der Warteschlange nehmen
                            pending.discard(h)
                            continue
                        state = info.get("state", "")
                        if norm(info.get("save_path")) == expected and "moving" not in state:
                            moved_ok.append(h)
                            pending.discard(h)

                if pending:
                    logger.warning(f"    Timeout beim Warten auf Move fuer {len(pending)} Torrent(s): "
                                    f"{sorted(pending)}. Werden NICHT gerecheckt/reaktiviert - bitte manuell pruefen.")

                if moved_ok:
                    logger.info(f"    {len(moved_ok)} Torrent(s) erfolgreich verschoben.")

                    if not args.no_recheck:
                        logger.info(f"    Starte Recheck fuer {len(moved_ok)} Torrent(s)")
                        client.recheck(moved_ok)

                    if not args.no_reenable_autotmm:
                        logger.debug(f"    AutoTMM wieder aktivieren fuer {len(moved_ok)} Torrent(s)")
                        time.sleep(1)
                        client.set_auto_management(moved_ok, enable=True)
    finally:
        if should_pause_all:
            logger.info("")
            logger.info("Starte ALLE Torrents in qBittorrent wieder (Verschieben/Recheck abgeschlossen) ...")
            try:
                client.resume_all()
            except Exception as e:
                logger.error(f"KONNTE Torrents nicht wieder starten ({e})! "
                              f"Bitte manuell in qBittorrent pruefen und ggf. alle Torrents starten.")
                raise

    logger.info("")
    logger.info("=" * 70)
    logger.info(f"Fertig. {'(DRY-RUN, es wurde nichts veraendert)' if args.dry_run else ''}")
    logger.info("=" * 70)


if __name__ == "__main__":
    start = datetime.now()
    main()
    print(f"\nLaufzeit: {datetime.now() - start}")
