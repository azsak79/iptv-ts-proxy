"""Csatorna-katalógus: a rendszer egyetlen igazságforrása.

Fájlformátum: a régi pipeline `katalog.json`-jával kompatibilis
  {"updated": "...", "channels": [{"chno","group","logo","name","tvg_id","urls":[...],"megjegyzes"?}, ...]}
Opcionális új kulcsok csatornánként (a régi eszközök figyelmen kívül hagyják):
  "id"        stabil csatornaazonosító (a kliensek URL-je: /ch/<id>.ts), szerkesztéskor NEM változik
  "enabled"   false = kikapcsolt (nincs playlistben, nem megy az adás)
  "url_meta"  {url: {"disabled": true, "label": "..."}}
  "pin"       rögzített forrás-URL (az automatikus kiválasztás nem váltja le)
Az `urls` sorrendje számít: az első az aktív (a legjobbnak mért) forrás.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time

IPRE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-fA-F:]+$")
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
EDIT_FIELDS = ("name", "chno", "group", "logo", "tvg_id", "urls", "url_meta", "enabled", "pin", "megjegyzes")


class CatalogError(ValueError):
    pass


def host_of(url):
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def is_ip_url(url):
    return bool(IPRE.match(host_of(url)))


def slug_id(rec, taken):
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", rec.get("tvg_id") or rec.get("name") or "").strip("-").lower()
    if not base:
        base = hashlib.sha1((rec.get("name") or "x").encode()).hexdigest()[:8]
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    taken.add(cid)
    return cid


def clean_attr(v):
    s = str(v if v is not None else "")
    return re.sub(r"[\r\n]+", " ", s).replace('"', "'").strip()


def extinf(c, logo=None):
    """Azonos a régi build.py formátumával."""
    parts = [f'tvg-id="{c.get("tvg_id", "")}"', f'tvg-name="{c["name"]}"']
    lg = logo if logo is not None else c.get("logo")
    if lg:
        parts.append(f'tvg-logo="{lg}"')
    if c.get("chno"):
        parts.append(f'tvg-chno="{c["chno"]}"')
    parts.append(f'group-title="{c.get("group", "")}"')
    return "#EXTINF:-1 " + " ".join(parts) + "," + c["name"]


def parse_m3u(text):
    """[(attrs, name, url)]"""
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            cur = (dict(ATTR_RE.findall(line)), line.rsplit(",", 1)[-1].strip())
        elif line and not line.startswith("#") and cur:
            out.append((cur[0], cur[1], line))
            cur = None
    return out


class Catalog:
    def __init__(self, path, hist_dir, keep=60):
        self.path, self.hist_dir, self.keep = path, hist_dir, keep
        self.channels = []
        self.updated = ""
        self.lock = threading.RLock()

    # ---------------------------------------------------------- betöltés / mentés
    def exists(self):
        return os.path.isfile(self.path)

    def load(self):
        with self.lock:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.channels = d.get("channels", [])
            self.updated = d.get("updated", "")
            return self._normalize()

    def _normalize(self):
        """Hiányzó mezők pótlása; visszaadja, hány azonosítót kellett újonnan kiosztani."""
        taken = {c["id"] for c in self.channels if c.get("id")}
        new = 0
        for c in self.channels:
            c["name"] = str(c.get("name", "")).strip()
            c.setdefault("urls", [])
            c.setdefault("group", "")
            c.setdefault("chno", "")
            c.setdefault("logo", "")
            c.setdefault("tvg_id", "")
            c["enabled"] = c.get("enabled", True) is not False
            c["url_meta"] = c.get("url_meta") or {}
            if not c.get("id"):
                c["id"] = slug_id(c, taken)
                new += 1
        return new

    def _dump_channels(self):
        out = []
        for c in self.channels:
            r = {"id": c["id"], "chno": c.get("chno", ""), "group": c.get("group", ""), "logo": c.get("logo", ""),
                 "name": c["name"], "tvg_id": c.get("tvg_id", ""), "urls": list(c.get("urls", []))}
            if c.get("enabled") is False:
                r["enabled"] = False
            if c.get("url_meta"):
                r["url_meta"] = c["url_meta"]
            if c.get("pin"):
                r["pin"] = c["pin"]
            if c.get("megjegyzes"):
                r["megjegyzes"] = c["megjegyzes"]
            out.append(r)
        return out

    def save(self, reason="szerkesztés"):
        with self.lock:
            self.updated = time.strftime("%Y-%m-%dT%H:%M:%S")
            data = json.dumps({"updated": self.updated, "channels": self._dump_channels()},
                              ensure_ascii=False, indent=1)
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            try:
                old = open(self.path, encoding="utf-8").read()
            except FileNotFoundError:
                old = None
            if old is not None and self._same(old, data):
                return False
            if old is not None:
                self._snapshot(reason)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data + "\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
            return True

    @staticmethod
    def _same(a, b):
        try:
            return json.loads(a).get("channels") == json.loads(b).get("channels")
        except Exception:  # noqa: BLE001
            return False

    def _snapshot(self, reason):
        os.makedirs(self.hist_dir, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-")[:30] or "mentes"
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.json"
        shutil.copy2(self.path, os.path.join(self.hist_dir, name))
        files = sorted(os.listdir(self.hist_dir))
        for old in files[:-self.keep]:
            try:
                os.remove(os.path.join(self.hist_dir, old))
            except OSError:
                pass

    def history(self):
        try:
            files = sorted(os.listdir(self.hist_dir), reverse=True)
        except FileNotFoundError:
            return []
        out = []
        for f in files:
            p = os.path.join(self.hist_dir, f)
            try:
                n = len(json.load(open(p, encoding="utf-8")).get("channels", []))
            except Exception:  # noqa: BLE001
                n = None
            out.append({"name": f, "size": os.path.getsize(p), "channels": n})
        return out

    def restore(self, name):
        if not re.match(r"^[\w.\-]+\.json$", name):
            raise CatalogError("érvénytelen fájlnév")
        p = os.path.join(self.hist_dir, name)
        if not os.path.isfile(p):
            raise CatalogError("nincs ilyen mentés")
        with self.lock:
            self._snapshot("visszaallitas-elott")
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            self.channels = d.get("channels", [])
            self._normalize()
            self.save("visszaallitas")

    # ---------------------------------------------------------- lekérdezés
    def get(self, cid):
        for c in self.channels:
            if c["id"] == cid:
                return c
        return None

    def groups(self):
        return sorted({c.get("group", "") for c in self.channels if c.get("group")})

    def active_url(self, c, ip_only=True):
        """Az első engedélyezett (és ha kell, IP-alapú) forrás: a régi build.py pick_url-je, tiltásokkal."""
        meta = c.get("url_meta") or {}
        for u in c.get("urls", []):
            if (meta.get(u) or {}).get("disabled"):
                continue
            if ip_only and not is_ip_url(u):
                continue
            return u
        return None

    # ---------------------------------------------------------- ellenőrzés / normalizálás
    def _clean_urls(self, urls):
        out = []
        for u in urls or []:
            u = str(u).strip()
            if not u:
                continue
            if not re.match(r"^https?://[^\s]+$", u):
                raise CatalogError(f"érvénytelen URL: {u[:80]}")
            if u not in out:
                out.append(u)
        return out

    def _clean(self, d, partial=False):
        r = {}
        if "name" in d or not partial:
            n = clean_attr(d.get("name"))
            if not n:
                raise CatalogError("a név kötelező")
            if len(n) > 120:
                raise CatalogError("a név legfeljebb 120 karakter")
            r["name"] = n
        for k in ("chno", "group", "tvg_id"):
            if k in d:
                r[k] = clean_attr(d[k])[:120]
        if "logo" in d:
            lg = str(d["logo"] or "").strip()
            if lg and not re.match(r"^https?://\S+$", lg):
                raise CatalogError("a logó http(s) URL legyen")
            r["logo"] = lg
        if "urls" in d:
            r["urls"] = self._clean_urls(d["urls"])
        if "enabled" in d:
            r["enabled"] = bool(d["enabled"])
        if "megjegyzes" in d:
            r["megjegyzes"] = str(d["megjegyzes"] or "").strip()[:500]
        if "url_meta" in d:
            m = {}
            for u, v in (d["url_meta"] or {}).items():
                v = v or {}
                e = {}
                if v.get("disabled"):
                    e["disabled"] = True
                if v.get("label"):
                    e["label"] = str(v["label"]).strip()[:60]
                if e:
                    m[u] = e
            r["url_meta"] = m
        if "pin" in d:
            pn = str(d["pin"] or "").strip()
            r["pin"] = pn
        return r

    # ---------------------------------------------------------- szerkesztés
    def add(self, d, position=None):
        with self.lock:
            r = self._clean(d)
            c = {"group": "", "chno": "", "logo": "", "tvg_id": "", "urls": [], "enabled": True, "url_meta": {}}
            c.update(r)
            if any(x["name"].lower() == c["name"].lower() for x in self.channels):
                raise CatalogError(f"már van „{c['name']}” nevű csatorna")
            c["id"] = slug_id(c, {x["id"] for x in self.channels})
            self._fix_pin(c)
            if position is None or position >= len(self.channels):
                self.channels.append(c)
            else:
                self.channels.insert(max(position, 0), c)
            self.save(f"uj-{c['id']}")
            return c

    def update(self, cid, d):
        with self.lock:
            c = self.get(cid)
            if not c:
                raise CatalogError("nincs ilyen csatorna")
            r = self._clean({k: v for k, v in d.items() if k in EDIT_FIELDS}, partial=True)
            if "name" in r and any(x is not c and x["name"].lower() == r["name"].lower() for x in self.channels):
                raise CatalogError(f"már van „{r['name']}” nevű csatorna")
            c.update(r)
            self._fix_pin(c)
            self.save(f"szerk-{cid}")
            return c

    def _fix_pin(self, c):
        if c.get("pin") and c["pin"] not in c.get("urls", []):
            c["pin"] = ""
        meta = c.get("url_meta") or {}
        c["url_meta"] = {u: v for u, v in meta.items() if u in c.get("urls", [])}

    def set_urls(self, cid, urls, reason="automatikus"):
        """Az URL-sorrend automatikus módosítása (forrásválasztás / átváltás)."""
        with self.lock:
            c = self.get(cid)
            if not c:
                return False
            c["urls"] = self._clean_urls(urls)
            self._fix_pin(c)
            self.save(reason)
            return True

    def set_urls_many(self, mapping, reason="automatikus"):
        with self.lock:
            for cid, urls in mapping.items():
                c = self.get(cid)
                if c:
                    c["urls"] = self._clean_urls(urls)
                    self._fix_pin(c)
            self.save(reason)

    def delete(self, cid):
        with self.lock:
            c = self.get(cid)
            if not c:
                raise CatalogError("nincs ilyen csatorna")
            self.channels.remove(c)
            self.save(f"torles-{cid}")
            return c

    def reorder(self, ids):
        with self.lock:
            by = {c["id"]: c for c in self.channels}
            if set(ids) != set(by) or len(ids) != len(by):
                raise CatalogError("a sorrendnek pontosan az összes csatornát tartalmaznia kell")
            self.channels = [by[i] for i in ids]
            self.save("sorrend")

    def renumber(self):
        """A csatornaszámok (chno) 1..N a jelenlegi sorrend szerint."""
        with self.lock:
            for i, c in enumerate(self.channels, 1):
                c["chno"] = str(i)
            self.save("ujraszamozas")

    def bulk(self, ids, action, value=None):
        with self.lock:
            sel = [self.get(i) for i in ids]
            sel = [c for c in sel if c]
            if not sel:
                raise CatalogError("nincs kijelölt csatorna")
            if action in ("enable", "disable"):
                for c in sel:
                    c["enabled"] = action == "enable"
            elif action == "set_group":
                g = clean_attr(value)
                for c in sel:
                    c["group"] = g
            elif action == "delete":
                for c in sel:
                    self.channels.remove(c)
            else:
                raise CatalogError("ismeretlen művelet")
            self.save(f"tomeges-{action}")
            return len(sel)

    # ---------------------------------------------------------- import / export
    def import_m3u(self, text, mode="add_new"):
        """mode: add_new = csak az új csatornák; merge = + az ismert csatornákhoz új forrás-URL-ek."""
        entries = parse_m3u(text)
        if not entries:
            raise CatalogError("nem található csatorna az M3U-ban")
        rep = {"added": [], "merged": [], "skipped": [], "invalid": 0}
        with self.lock:
            by_tvg = {c["tvg_id"].lower(): c for c in self.channels if c.get("tvg_id")}
            by_name = {c["name"].lower(): c for c in self.channels}
            for attrs, name, url in entries:
                if not re.match(r"^https?://\S+$", url):
                    rep["invalid"] += 1
                    continue
                name = clean_attr(attrs.get("tvg-name") or name)
                tid = clean_attr(attrs.get("tvg-id", ""))
                ex = (by_tvg.get(tid.lower()) if tid else None) or by_name.get(name.lower())
                if ex:
                    if mode == "merge" and url not in ex["urls"]:
                        ex["urls"].append(url)
                        rep["merged"].append(ex["name"])
                    else:
                        rep["skipped"].append(ex["name"])
                    continue
                c = {"name": name, "tvg_id": tid, "group": clean_attr(attrs.get("group-title", "")),
                     "chno": clean_attr(attrs.get("tvg-chno", "")), "logo": attrs.get("tvg-logo", ""),
                     "urls": [url], "enabled": True, "url_meta": {}}
                c["id"] = slug_id(c, {x["id"] for x in self.channels})
                self.channels.append(c)
                by_name[name.lower()] = c
                if tid:
                    by_tvg[tid.lower()] = c
                rep["added"].append(name)
            if rep["added"] or rep["merged"]:
                self.save("import")
        return rep

    def export_json(self):
        return json.dumps({"updated": self.updated, "channels": self._dump_channels()}, ensure_ascii=False, indent=1)

    def export_m3u(self, chans, title, epg_url, direct=True, logo_for=None, url_for=None):
        lines = [f'#EXTM3U x-tvg-url="{epg_url}"', f"#PLAYLIST:{title}"]
        for c in chans:
            lg = logo_for(c) if logo_for else None
            lines.append(extinf(c, lg))
            lines.append(url_for(c) if url_for else self.active_url(c))
        return "\n".join(lines) + "\n"
