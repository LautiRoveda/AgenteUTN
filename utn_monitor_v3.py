#!/usr/bin/env python3
"""
UTN FRC Autogestión - Monitor de Notas (v3).
Combina dos fuentes:
  1. Autogestión 3 (a3): iframe NOTAS → mensajes de docentes clásicos.
  2. Autogestión 4 (a4): timeline principal → todo lo demás (encuestas,
     adjuntos globales, invitaciones) + los mismos mensajes de docentes.
Dedup cruzado: los mensajes de docente aparecen en ambas fuentes; se usa
un ID canónico basado en `materia+fecha+autor` (igual al de a3) para no
duplicar, y un `a4:<id_mensaje>` para los que sólo viven en a4.
Estado compartido con v1 (`utn_seen_messages.json`): los IDs a3 ya vistos
se respetan.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)
# ── Configuración ─────────────────────────────────────────────────────────────
def _require_env(name: str, hint: str = "") -> str:
    val = (os.environ.get(name) or "").strip()
    if not val:
        sys.stderr.write(
            f"ERROR: variable de entorno {name} no definida o vacía.\n"
            f"  → Copiá .env.example a .env y completá tus credenciales,\n"
            f"    o exportá {name} antes de correr el script.\n"
        )
        if hint:
            sys.stderr.write(f"  → {hint}\n")
        sys.exit(1)
    return val

UTN_USER       = _require_env("UTN_USER", "Tu legajo UTN (ej. 123456).")
UTN_DOMAIN     = os.environ.get("UTN_DOMAIN", "sistemas").strip() or "sistemas"
UTN_PASS       = _require_env("UTN_PASS", "Tu contraseña de Autogestión UTN.")
TELEGRAM_TOKEN = _require_env(
    "TELEGRAM_TOKEN",
    "Token del bot de Telegram que te dio @BotFather.",
)
UTN_LOGIN      = "https://www.frc.utn.edu.ar/logon.frc"
UTN_LOGOUT     = "https://www.frc.utn.edu.ar/logout.frc"
UTN_A3_DASH    = "https://www.frc.utn.edu.ar/academico3/defaultreduced.frc"
UTN_A3_POPUP   = "https://www.frc.utn.edu.ar/academico3/transacciones/IMensajes2.frc?p=1"
UTN_A4_DASH    = "https://a4.frc.utn.edu.ar/4/"
TELEGRAM_API   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
BASE_DIR     = Path(__file__).resolve().parent
STATE_FILE   = BASE_DIR / "utn_seen_messages.json"
GRADES_FILE  = BASE_DIR / "utn_grades_state.json"
CHAT_ID_FILE = BASE_DIR / "utn_telegram_chat_id.txt"
LOG_FILE     = BASE_DIR / "utn_monitor_v3.log"
DEBUG_DIR    = BASE_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)
MAX_ATTEMPTS     = 3
RETRY_DELAY_BASE = 5
NAV_TIMEOUT_MS   = 45_000
DEFAULT_TIMEOUT  = 30_000
TG_INTER_MSG_SLEEP = 0.4
MAX_DOC_SIZE_MB = 49
MAX_DOC_SIZE    = MAX_DOC_SIZE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = 45
UTN_A4_BASE = "https://a4.frc.utn.edu.ar/4"
GRADE_TITULOS_URL = UTN_A4_BASE + "/academico/notas/titulos/{cid}"
GRADE_NOTAS_URL   = UTN_A4_BASE + "/cursado/materias/notas/{cid}"
_FILE_EXTS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".txt", ".csv", ".rtf",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
)
# ── Logging ───────────────────────────────────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
log = logging.getLogger("utn3")
log.setLevel(logging.INFO)
if not log.handlers:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
# ── Excepciones ──────────────────────────────────────────────────────────────
class LoginFailed(Exception):
    """Credenciales rechazadas: NO reintentar, alertar al usuario."""
class TransientError(Exception):
    """Falla temporal: reintentar, no alertar."""
# ── Telegram ──────────────────────────────────────────────────────────────────
def _tg_request(method: str, *, get: bool = False, payload: Optional[dict] = None) -> Optional[dict]:
    url = f"{TELEGRAM_API}/{method}"
    for i in range(3):
        try:
            r = requests.get(url, timeout=15) if get else requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                log.warning("Telegram %s no-ok: %s", method, data.get("description"))
                return None
            return data
        except (requests.RequestException, ValueError) as e:
            log.warning("Telegram %s intento %d falló: %s", method, i + 1, e)
            time.sleep(2 * (i + 1))
    return None
def get_chat_id() -> Optional[str]:
    if CHAT_ID_FILE.exists():
        cid = CHAT_ID_FILE.read_text(encoding="utf-8").strip()
        if cid:
            return cid
    log.info("Buscando chat_id via getUpdates...")
    data = _tg_request("getUpdates", get=True)
    results = (data or {}).get("result", [])
    if not results:
        log.warning("Enviá un mensaje al bot primero.")
        return None
    chat_id = str(results[-1]["message"]["chat"]["id"])
    CHAT_ID_FILE.write_text(chat_id, encoding="utf-8")
    log.info("Chat ID: %s", chat_id)
    return chat_id
def send_telegram(chat_id: str, text: str) -> bool:
    return _tg_request(
        "sendMessage",
        payload={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    ) is not None
def send_telegram_document(
    chat_id: str, filename: str, content: bytes, mime: str, caption: str = ""
) -> bool:
    """Sube un archivo (bytes) como documento a Telegram."""
    url = f"{TELEGRAM_API}/sendDocument"
    for i in range(3):
        try:
            r = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1000],
                    "parse_mode": "HTML",
                },
                files={"document": (filename, content, mime or "application/octet-stream")},
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                return True
            log.warning("sendDocument no-ok: %s", data.get("description"))
        except (requests.RequestException, ValueError) as e:
            log.warning("sendDocument intento %d falló: %s", i + 1, e)
        time.sleep(2 * (i + 1))
    return False
# ── Descarga de adjuntos (usando cookies del login Playwright) ───────────────
def _make_requests_session(cookies: list[dict]) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "es-AR,es;q=0.9",
    })
    for c in cookies or []:
        try:
            s.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
        except Exception:
            continue
    return s
def _looks_like_file(url: str) -> bool:
    try:
        path = urllib.parse.urlparse(url).path.lower()
    except Exception:
        return False
    if any(path.endswith(ext) for ext in _FILE_EXTS):
        return True
    if "/descargar/" in path or "/download/" in path or "/archivo/" in path:
        return True
    return False
def _filename_from(url: str, content_disp: Optional[str]) -> str:
    if content_disp:
        m = re.search(
            r"filename\*?=(?:UTF-8'')?\"?([^;\"]+)\"?",
            content_disp, re.IGNORECASE,
        )
        if m:
            return urllib.parse.unquote(m.group(1)).strip(' "')
    base = urllib.parse.unquote(urllib.parse.urlparse(url).path).rstrip("/").split("/")[-1]
    return base or "archivo"
def _guess_extension(content: bytes) -> str:
    if not content:
        return ""
    if content.startswith(b"%PDF"):
        return ".pdf"
    if content[:4] == b"PK\x03\x04":
        head = content[:4096]
        if b"word/" in head:
            return ".docx"
        if b"xl/" in head:
            return ".xlsx"
        if b"ppt/" in head:
            return ".pptx"
        return ".zip"
    if content[:4] == b"\xd0\xcf\x11\xe0":
        return ".doc"
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"GIF8":
        return ".gif"
    if content.startswith(b"{\\rtf"):
        return ".rtf"
    return ""
def _best_filename(candidates: list[str], content: bytes) -> str:
    best = ""
    for c in candidates:
        if not c:
            continue
        c = c.strip()
        if not c:
            continue
        if not best:
            best = c
        elif "." not in best and "." in c:
            best = c
    if not best:
        best = "archivo"
    if "." not in best:
        ext = _guess_extension(content)
        if ext:
            best = f"{best}{ext}"
    return best
def download_file(
    session: requests.Session,
    url: str,
    preferred_filename: Optional[str] = None,
) -> Optional[tuple[str, bytes, str]]:
    """Descarga un archivo autenticado. Devuelve (filename, bytes, mime) o None."""
    headers = {"Referer": UTN_A4_DASH}
    try:
        r = session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT,
                        allow_redirects=True, headers=headers)
        if r.status_code == 405:
            log.info("GET 405 en %s; reintentando con POST", url)
            r.close()
            r = session.post(url, stream=True, timeout=DOWNLOAD_TIMEOUT,
                             allow_redirects=True, headers=headers)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Descarga falló %s: %s", url, e)
        return None
    ct = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ct:
        log.warning("URL %s devolvió HTML (sesión vencida o inválida)", url)
        r.close()
        return None
    buf = bytearray()
    for chunk in r.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > MAX_DOC_SIZE:
            log.warning(
                "Archivo %s excede %d MB — se omite (queda sólo el link)",
                url, MAX_DOC_SIZE_MB,
            )
            r.close()
            return None
    cd_name = _filename_from(url, r.headers.get("Content-Disposition"))
    if preferred_filename:
        filename = preferred_filename.strip() or cd_name
        if "." not in filename and "." in cd_name:
            filename = f"{filename}.{cd_name.rsplit('.', 1)[1]}"
    else:
        filename = cd_name
    mime = ct.split(";")[0].strip() or "application/octet-stream"
    return filename, bytes(buf), mime
# ── Estado ────────────────────────────────────────────────────────────────────
def load_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as e:
        log.error("Estado corrupto, empezando vacío: %s", e)
        return set()
def save_seen(seen: set[str]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)
def _md5(s: str) -> str:
    return hashlib.md5(s.strip().encode("utf-8")).hexdigest()
def ids_for_msg(msg: dict) -> set[str]:
    """
    Devuelve todos los IDs con los que este mensaje puede aparecer en `seen`,
    para permitir dedup cruzado a3↔a4.
    """
    ids: set[str] = set()
    a4_id = msg.get("a4_id")
    if a4_id:
        ids.add(f"a4:{a4_id}")
    if msg.get("materia") and msg.get("fecha") and msg.get("autor"):
        header = f"{msg['materia']}-Publicado: {msg['fecha']}, {msg['autor']}"
        ids.add(_md5(header))
    elif msg.get("header"):
        ids.add(_md5(msg["header"]))
    body = (msg.get("cuerpo") or "").strip()
    if body:
        norm = re.sub(r"\s+", " ", body[:200]).lower()
        ids.add("c:" + _md5(norm))
    return ids
# ── Estado: notas A4 (calificaciones) ─────────────────────────────────────────
def load_grades_state() -> dict:
    """Snapshot anterior de notas: {cursoId: {nota1: '...', ..., notafinal: '...'}}"""
    if not GRADES_FILE.exists():
        return {}
    try:
        return json.loads(GRADES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("Estado de notas corrupto, empezando vacío: %s", e)
        return {}
def save_grades_state(state: dict) -> None:
    tmp = GRADES_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, GRADES_FILE)
def is_empty_grade(v) -> bool:
    """0, 0.00, '.', '-' o vacío se consideran 'sin nota cargada'."""
    if v is None:
        return True
    s = str(v).strip()
    return s in ("", "0", "0.00", "0,00", ".", "-")
def grade_notif_id(notif: dict) -> str:
    """ID único por (curso, instancia, valor) para deduplicar entre corridas."""
    key = f"{notif['curso_id']}|{notif['titulo']}|{notif['valor']}"
    return "g:" + _md5(key)
# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_a3_notas_div(html: str) -> list[dict]:
    """a3: div.txtCmn.fCmn con <strong>MATERIA</strong> + <blockquote>."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find("div", class_=lambda c: c and "txtCmn" in c and "fCmn" in c) or soup
    msgs: list[dict] = []
    for bq in container.find_all("blockquote"):
        cuerpo = bq.get_text(separator="\n", strip=True)
        if not cuerpo:
            continue
        strongs: list[str] = []
        node = bq.previous_sibling
        while node is not None:
            name = getattr(node, "name", None)
            if name == "blockquote":
                break
            if name == "strong":
                cls = node.get("class") or []
                if "dikdor" not in cls:
                    strongs.insert(0, node.get_text(strip=True))
            node = node.previous_sibling
        materia = strongs[0] if strongs else "UTN"
        fecha   = strongs[1] if len(strongs) > 1 else ""
        autor   = strongs[2] if len(strongs) > 2 else ""
        msgs.append({
            "source":  "a3",
            "header":  f"{materia}-Publicado: {fecha}, {autor}",
            "materia": materia, "fecha": fecha, "autor": autor,
            "cuerpo":  cuerpo,
            "links":   [],
        })
    return msgs
def parse_a3_old_html(html: str) -> list[dict]:
    """Fallback del popup IMensajes2: regex sobre texto plano."""
    soup  = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip()]
    msgs: list[dict] = []
    i = 0
    header_re = re.compile(r"^([A-Z0-9]+(?:K[0-9]+)?)-Publicado:\s*(\d{2}/\d{2}/\d{4}),\s*(.+)$")
    while i < len(lines):
        m = header_re.match(lines[i])
        if not m:
            i += 1
            continue
        materia, fecha, autor = m.group(1), m.group(2), m.group(3).strip()
        body: list[str] = []
        i += 1
        while i < len(lines) and not re.match(r"^[A-Z0-9]+-Publicado:", lines[i]):
            body.append(lines[i])
            i += 1
        msgs.append({
            "source":  "a3",
            "header":  f"{materia}-Publicado: {fecha}, {autor}",
            "materia": materia, "fecha": fecha, "autor": autor,
            "cuerpo":  "\n".join(body).strip(),
            "links":   [],
        })
    return msgs
def parse_a4_timeline(html: str) -> list[dict]:
    """
    a4: <ul id="listaMensajes"> con <li id="idMensaje####">.
    Contempla:
      - Mensajes de docente: <i class="fa-user"> + h3 con autor + body + footer con materia.
      - Info/encuestas/adjuntos: <i class="fa-thumb-tack"> + h3 con todo el texto, sin docente ni materia.
    """
    soup = BeautifulSoup(html, "html.parser")
    ul = soup.find("ul", id="listaMensajes")
    if not ul:
        return []
    msgs: list[dict] = []
    id_re = re.compile(r"^idMensaje(\d+)$")
    for li in ul.find_all("li", id=id_re):
        a4_id = id_re.match(li.get("id", "")).group(1)
        time_span = li.find("span", class_="time")
        fecha_full = (time_span.get("title") or "").strip() if time_span else ""
        m = re.search(r"(\d{2}/\d{2}/\d{4})", fecha_full)
        fecha = m.group(1) if m else ""
        h3 = li.find("h3", class_="timeline-header")
        autor_el = h3.find("a", class_="busquedaUsuario") if h3 else None
        autor = autor_el.get_text(strip=True) if autor_el else ""
        body_el = li.find("div", class_="timeline-body")
        if body_el:
            cuerpo = body_el.get_text("\n", strip=True)
        elif h3:
            h3_copy = BeautifulSoup(str(h3), "html.parser").find("h3")
            for bad in h3_copy.find_all(["button", "ul"]):
                bad.decompose()
            for bad in h3_copy.find_all("div", class_="btn-group"):
                bad.decompose()
            cuerpo = h3_copy.get_text("\n", strip=True)
        else:
            cuerpo = ""
        materia = ""
        materia_full = ""
        footer = li.find("div", class_="timeline-footer")
        if footer:
            alcance = footer.find("a", class_="alcance")
            if alcance:
                materia_full = alcance.get_text(strip=True)
                mm = re.match(r"\(([^)]+)\)", materia_full)
                materia = mm.group(1) if mm else materia_full
        attachments: list[dict] = []
        desc_lines: list[str] = []
        att_by_url: dict[str, dict] = {}
        for tbl in li.find_all("table", id=re.compile(r"^tablaArchivoDescarga")):
            for a in tbl.find_all("a", class_="archivosDescarga", href=True):
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                absolute = urllib.parse.urljoin(UTN_A4_DASH, href)
                txt = a.get_text(strip=True) or ""
                title_attr = (a.get("title") or "").strip()
                download_attr = (a.get("download") or "").strip()
                existing = att_by_url.get(absolute)
                if existing is None:
                    att_by_url[absolute] = {
                        "url": absolute,
                        "rel_href": href,
                        "filename": txt,
                        "title": title_attr,
                        "download_attr": download_attr,
                    }
                else:
                    if not existing["filename"] and txt:
                        existing["filename"] = txt
                    if not existing["title"] and title_attr:
                        existing["title"] = title_attr
                    if not existing["download_attr"] and download_attr:
                        existing["download_attr"] = download_attr
            for td in tbl.find_all("td"):
                if td.find("a", class_="archivosDescarga") or td.find("img"):
                    continue
                td_txt = td.get_text(" ", strip=True)
                if td_txt and td_txt not in desc_lines:
                    desc_lines.append(td_txt)
        attachments = list(att_by_url.values())
        if desc_lines:
            desc = "\n".join(desc_lines)
            if cuerpo and desc not in cuerpo:
                cuerpo = f"{cuerpo}\n\n{desc}"
            elif not cuerpo:
                cuerpo = desc
        links: list[str] = []
        for a in li.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            absolute = urllib.parse.urljoin(UTN_A4_DASH, href)
            if absolute not in links:
                links.append(absolute)
        if materia and fecha and autor:
            header = f"{materia}-Publicado: {fecha}, {autor}"
        else:
            header = f"A4#{a4_id}"
        if not cuerpo and not materia and not autor:
            continue
        msgs.append({
            "source":       "a4",
            "a4_id":        a4_id,
            "header":       header,
            "materia":      materia,
            "materia_full": materia_full,
            "fecha":        fecha,
            "fecha_full":   fecha_full,
            "autor":        autor,
            "cuerpo":       cuerpo,
            "links":        links,
            "attachments":  attachments,
        })
    return msgs
# ── Playwright ────────────────────────────────────────────────────────────────
def _save_debug(page: Page, tag: str) -> None:
    ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = DEBUG_DIR / f"{ts}-{tag}"
    try:
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        log.info("Debug guardado: %s(.png/.html)", stem)
    except Exception as e:
        log.debug("No se pudo guardar debug (%s): %s", tag, e)
def _do_login(page: Page) -> None:
    log.info("Cargando login...")
    page.goto(UTN_LOGIN, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.wait_for_selector('input[name="txtUsuario"]', timeout=20_000)
    page.fill('input[name="txtUsuario"]', UTN_USER)
    page.fill('input[name="pwdClave"]',   UTN_PASS)
    try:
        page.select_option('select[name="txtDominios"]', UTN_DOMAIN)
    except (PWTimeout, Exception):
        pass
    log.info("Enviando formulario...")
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS):
            page.click('input[name="btnEnviar"], input[type="submit"], button[type="submit"]')
    except PWTimeout as e:
        raise TransientError(f"Timeout post-login: {e}")
    url     = page.url
    content = page.content()
    rejected = (
        ("logon" in url and "e=" in url)
        or 'name="txtUsuario"' in content
        or "Usuario o clave" in content
    )
    if rejected:
        _save_debug(page, "login-rejected")
        raise LoginFailed(f"Login rechazado por UTN (url={url})")
    log.info("Login OK → %s", url)
def _extract_a3(page: Page) -> list[dict]:
    """Lee el iframe NOTAS del dashboard academico3."""
    try:
        page.goto(UTN_A3_DASH, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except PWTimeout as e:
        raise TransientError(f"No cargó dashboard a3: {e}")
    try:
        page.wait_for_selector(
            'iframe[src*="tipo=NOTAS"]', state="attached", timeout=15_000
        )
    except PWTimeout as e:
        _save_debug(page, "no-iframe-notas")
        raise TransientError(f"No apareció iframe NOTAS: {e}")
    notas_frame = None
    for _ in range(15):
        for fr in page.frames:
            if "tipo=NOTAS" in (fr.url or ""):
                notas_frame = fr
                break
        if notas_frame:
            break
        page.wait_for_timeout(500)
    if notas_frame is None:
        _save_debug(page, "frame-notas-missing")
        raise TransientError("Frame NOTAS no accesible")
    try:
        notas_frame.wait_for_load_state("domcontentloaded", timeout=15_000)
        notas_frame.wait_for_selector(
            "div.txtCmn, div.fCmn", state="attached", timeout=10_000
        )
    except PWTimeout:
        pass
    html = notas_frame.content()
    msgs = parse_a3_notas_div(html)
    if msgs:
        log.info("a3 iframe: %d mensajes", len(msgs))
        return msgs
    msgs = parse_a3_old_html(html)
    if msgs:
        log.info("a3 iframe (old parser): %d mensajes", len(msgs))
        return msgs
    log.info("a3: probando popup IMensajes2...")
    try:
        page.goto(UTN_A3_POPUP, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        content = page.content()
    except PWTimeout as e:
        raise TransientError(f"No cargó popup a3: {e}")
    msgs = parse_a3_notas_div(content) or parse_a3_old_html(content)
    if msgs:
        log.info("a3 popup: %d mensajes", len(msgs))
        return msgs
    if "txtCmn" not in html and "Publicado" not in html:
        _save_debug(page, "a3-no-msgs")
        raise TransientError("a3 sin contenido esperado")
    return []
def _extract_a4(page: Page) -> list[dict]:
    """Lee ul#listaMensajes de Autogestión 4."""
    try:
        page.goto(UTN_A4_DASH, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except PWTimeout as e:
        raise TransientError(f"No cargó a4: {e}")
    try:
        page.wait_for_selector("ul#listaMensajes", state="attached", timeout=15_000)
        page.wait_for_selector(
            'ul#listaMensajes li[id^="idMensaje"]',
            state="attached", timeout=15_000,
        )
    except PWTimeout:
        _save_debug(page, "a4-no-timeline")
        log.warning("a4: listaMensajes no tuvo ítems en 15s, parseo lo que haya")
    html = page.content()
    msgs = parse_a4_timeline(html)
    log.info("a4 timeline: %d mensajes", len(msgs))
    return msgs
def _logout(page: Page) -> None:
    try:
        page.goto(UTN_LOGOUT, wait_until="domcontentloaded", timeout=15_000)
    except Exception as e:
        log.debug("Logout ignorado: %s", e)
def _extract_grades(page: Page) -> tuple[list[dict], dict]:
    """
    Lee las calificaciones de cada materia inscripta del año vigente desde a4.
    Asume que la page YA tiene sesión iniciada y que recién se cargó UTN_A4_DASH
    (lo deja _extract_a4). Devuelve (notificaciones, estado_actualizado).
      - notificaciones: dicts con tipo (nueva|modificada), curso_id, curso_nombre,
                        titulo (instancia), valor [, valor_anterior].
      - estado: {cursoId: {nota1: '...', ..., notafinal: '...'}}.
    """
    # Lista de cursos del panel "Materias (YYYY)" — siempre el año vigente por defecto.
    js = (
        "() => Array.from(document.querySelectorAll('li[id^=\"idCurso\"]'))"
        ".map(li => ({"
        " id: li.id.replace(/^idCurso/, ''),"
        " nombre: ((li.querySelector('a') || {}).textContent || '')"
        "         .replace(/\\s+/g, ' ').trim()"
        "}))"
    )
    try:
        cursos = page.evaluate(js)
    except Exception as e:
        log.warning("a4-notas: no se pudo evaluar lista de cursos: %s", e)
        return [], {}
    if not cursos:
        log.info("a4-notas: panel Materias vacío.")
        return [], {}
    log.info("a4-notas: %d curso(s) inscriptos.", len(cursos))
    previous = load_grades_state()
    is_first_run = not previous
    new_state = dict(previous)
    notifs: list[dict] = []
    for c in cursos:
        cid = c.get("id") or ""
        nombre = c.get("nombre") or cid
        if not cid:
            continue
        try:
            tr = page.request.get(GRADE_TITULOS_URL.format(cid=cid))
            if tr.status != 200:
                log.debug("a4-notas: titulos %s status %s", cid, tr.status)
                continue
            titulos_text = tr.text()
            titulos = titulos_text.split("|")
            if titulos and titulos[-1] == "":
                titulos = titulos[:-1]
            nr = page.request.get(GRADE_NOTAS_URL.format(cid=cid))
            if nr.status == 204:
                # Sin notas cargadas: preservar estado previo (no pisar con vacío).
                new_state[cid] = previous.get(cid, {})
                continue
            if nr.status != 200:
                log.debug("a4-notas: notas %s status %s", cid, nr.status)
                continue
            try:
                data = nr.json()
            except Exception as je:
                log.debug("a4-notas: JSON parse %s falló: %s", cid, je)
                continue
            if not isinstance(data, list) or not data:
                new_state[cid] = previous.get(cid, {})
                continue
            row = data[0]
            current = {k: str(v) for k, v in row.items() if k.startswith("nota")}
            new_state[cid] = current
            prev_curso = previous.get(cid, {})
            for key, valor_nuevo in current.items():
                titulo = ""
                m = re.match(r"^nota(\d+)$", key)
                if m:
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(titulos):
                        titulo = titulos[idx].strip()
                elif key == "notafinal":
                    titulo = "Promedio"
                if not titulo or titulo == ".":
                    continue
                if is_empty_grade(valor_nuevo):
                    continue
                valor_anterior = prev_curso.get(key, "")
                if is_empty_grade(valor_anterior):
                    if not is_first_run:
                        notifs.append({
                            "tipo": "nueva",
                            "curso_id": cid,
                            "curso_nombre": nombre,
                            "titulo": titulo,
                            "valor": valor_nuevo,
                        })
                elif str(valor_anterior) != str(valor_nuevo):
                    notifs.append({
                        "tipo": "modificada",
                        "curso_id": cid,
                        "curso_nombre": nombre,
                        "titulo": titulo,
                        "valor": valor_nuevo,
                        "valor_anterior": valor_anterior,
                    })
        except Exception as e:
            log.warning("a4-notas: %s falló: %s", cid, e)
            continue
    if is_first_run:
        log.info("a4-notas: primera corrida — snapshot inicial guardado, sin notif.")
    else:
        log.info("a4-notas: %d novedad(es) detectadas.", len(notifs))
    return notifs, new_state
def _download_via_browser(
    page: Page, a4_id: str, rel_href: str,
    timeout_ms: int = 60_000, attempts: int = 2,
) -> Optional[tuple[str, bytes]]:
    """
    Hace click en el `<a class="archivosDescarga">` específico y captura la
    descarga que dispara el JS de UTN (que arma un POST con `A4-Token`,
    `A4-TimeStamp` y `A4-Data` imposibles de reproducir desde requests).
    Devuelve (suggested_filename, bytes) o None.
    """
    safe_href = rel_href.replace('"', '\\"')
    selector = f'li#idMensaje{a4_id} a.archivosDescarga[href="{safe_href}"]'
    for attempt in range(1, attempts + 1):
        tmp_path = DEBUG_DIR / f"_dl_{a4_id}_{int(time.time()*1000)}.bin"
        try:
            with page.expect_download(timeout=timeout_ms) as dl_info:
                page.click(selector, timeout=timeout_ms)
            dl = dl_info.value
            dl.save_as(str(tmp_path))
            content = tmp_path.read_bytes()
            return (dl.suggested_filename or "", content)
        except Exception as e:
            log.warning(
                "Descarga intento %d/%d falló (%s %s): %s",
                attempt, attempts, a4_id, rel_href, e,
            )
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
        if attempt < attempts:
            time.sleep(3)
    return None
def _send_attachments_for(
    chat_id: str, page: Page, req_session: requests.Session, m: dict
) -> None:
    """Baja los adjuntos de `m` y los reenvía por Telegram."""
    attachments = m.get("attachments") or []
    materia = m.get("materia") or ""
    a4_id = m.get("a4_id") or ""
    if attachments and a4_id:
        for att in attachments:
            rel = att.get("rel_href") or ""
            if not rel:
                continue
            log.info("📥 Bajando adjunto (browser): %s", att["url"])
            dl = _download_via_browser(page, a4_id, rel)
            if not dl:
                continue
            suggested, content = dl
            if len(content) > MAX_DOC_SIZE:
                log.warning(
                    "Archivo %s excede %d MB — se omite",
                    att["url"], MAX_DOC_SIZE_MB,
                )
                continue
            fname = _best_filename(
                [
                    att.get("filename"),
                    att.get("title"),
                    att.get("download_attr"),
                    suggested,
                    _filename_from(att["url"], None),
                ],
                content,
            )
            mime = "application/octet-stream"
            caption = f"📎 <b>{_esc(fname)}</b>"
            if materia:
                caption += f"\n📌 {_esc(materia)}"
            if send_telegram_document(chat_id, fname, content, mime, caption):
                log.info("📎 Adjunto enviado: %s (%d KB)", fname, len(content) // 1024)
            else:
                log.warning("No se pudo enviar adjunto: %s", fname)
            time.sleep(TG_INTER_MSG_SLEEP)
        return
    file_urls = [u for u in (m.get("links") or []) if _looks_like_file(u)]
    for url in file_urls:
        log.info("📥 Bajando adjunto (http): %s", url)
        dl = download_file(req_session, url)
        if not dl:
            continue
        fname, content, mime = dl
        caption = f"📎 <b>{_esc(fname)}</b>"
        if materia:
            caption += f"\n📌 {_esc(materia)}"
        if send_telegram_document(chat_id, fname, content, mime, caption):
            log.info("📎 Adjunto enviado: %s (%d KB)", fname, len(content) // 1024)
        else:
            log.warning("No se pudo enviar adjunto: %s", fname)
        time.sleep(TG_INTER_MSG_SLEEP)
def fetch_and_notify(chat_id: str, seen: set[str]) -> int:
    """
    Login + extracción a3/a4 + notas + dedup + envío Telegram (texto + adjuntos +
    notificaciones de calificaciones) en una sola sesión Playwright.
    Muta `seen` in-place. Devuelve el número de notificaciones enviadas
    (mensajes + notas).
    """
    sent = 0
    grade_state_to_save: Optional[dict] = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx: BrowserContext = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="es-AR",
            accept_downloads=True,
        )
        ctx.set_default_timeout(DEFAULT_TIMEOUT)
        ctx.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page = ctx.new_page()
        try:
            _do_login(page)
            a3_msgs: list[dict] = []
            try:
                a3_msgs = _extract_a3(page)
            except TransientError as e:
                log.warning("a3 falló (sigo con a4): %s", e)
            a4_msgs: list[dict] = []
            try:
                a4_msgs = _extract_a4(page)
            except TransientError as e:
                log.warning("a4 falló: %s", e)
            if not a3_msgs and not a4_msgs:
                # Aun sin mensajes intentamos las notas; si no hay nada en absoluto,
                # consideramos transitorio.
                log.warning("Ambas fuentes de mensajes vacías — pruebo notas igual.")
            msgs = a3_msgs + a4_msgs
            log.info("%d mensaje(s) combinados (antes de dedup).", len(msgs))
            nuevos = _pick_new(msgs, seen)
            if nuevos:
                log.info("%d mensaje(s) nuevo(s) a notificar.", len(nuevos))
            else:
                log.info("Sin mensajes nuevos.")
            cookies = []
            try:
                cookies = ctx.cookies()
            except Exception as e:
                log.debug("No pude capturar cookies: %s", e)
            req_session = _make_requests_session(cookies)
            for ids, m in nuevos:
                if not send_telegram(chat_id, format_msg(m)):
                    log.warning("Error enviando: %s", m["header"][:80])
                    continue
                seen |= ids
                sent += 1
                log.info("✉️  Enviado [%s]: %s", m.get("source"), m["header"][:80])
                try:
                    _send_attachments_for(chat_id, page, req_session, m)
                except Exception as e:
                    log.warning("Error en adjuntos de %s: %s", m.get("a4_id") or m["header"][:40], e)
                time.sleep(TG_INTER_MSG_SLEEP)
            # ── Calificaciones (a4) ────────────────────────────────────────
            # Dejamos para el final porque requiere estar en a4. _extract_a4
            # ya nos dejó ahí; si _extract_a4 falló navegamos manualmente.
            if "a4.frc.utn.edu.ar" not in (page.url or ""):
                try:
                    page.goto(UTN_A4_DASH, wait_until="domcontentloaded",
                              timeout=DEFAULT_TIMEOUT)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    log.warning("a4-notas: no pude volver al dashboard a4: %s", e)
            try:
                grade_notifs, new_grade_state = _extract_grades(page)
                grade_state_to_save = new_grade_state
            except Exception as e:
                log.warning("a4-notas: extracción falló: %s", e)
                grade_notifs = []
            for n in grade_notifs:
                nid = grade_notif_id(n)
                if nid in seen:
                    continue
                if not send_telegram(chat_id, format_grade_msg(n)):
                    log.warning("Error enviando nota: %s — %s",
                                n["curso_nombre"], n["titulo"])
                    continue
                seen.add(nid)
                sent += 1
                log.info("📊 Nota notificada: %s — %s: %s",
                         n["curso_nombre"], n["titulo"], n["valor"])
                time.sleep(TG_INTER_MSG_SLEEP)
            _logout(page)
        finally:
            ctx.close()
            browser.close()
    if grade_state_to_save is not None:
        try:
            save_grades_state(grade_state_to_save)
        except Exception as e:
            log.warning("No pude guardar estado de notas: %s", e)
    return sent
def fetch_and_notify_with_retry(chat_id: str, seen: set[str]) -> tuple[Optional[int], Optional[str]]:
    last_error = "desconocido"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            sent = fetch_and_notify(chat_id, seen)
            return sent, None
        except LoginFailed as e:
            log.error("Login falló: %s", e)
            return None, "login"
        except (TransientError, PWTimeout) as e:
            last_error = str(e)
            log.warning("Intento %d/%d transitorio: %s", attempt, MAX_ATTEMPTS, e)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.exception("Intento %d/%d error inesperado", attempt, MAX_ATTEMPTS)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY_BASE * attempt)
    return None, f"transient:{last_error}"
# ── Formato Telegram ──────────────────────────────────────────────────────────
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def format_msg(msg: dict) -> str:
    is_a4_only = msg.get("source") == "a4" and not (msg.get("materia") and msg.get("autor"))
    titulo = "📢 <b>Nuevo aviso (A4)</b>" if is_a4_only else "📚 <b>Nueva nota en UTN</b>"
    parts = [titulo + "\n"]
    if msg.get("materia"):
        mat = msg.get("materia_full") or msg["materia"]
        parts.append(f"📌 <b>Materia:</b> {_esc(mat)}")
    if msg.get("autor"):
        parts.append(f"👤 <b>Docente:</b> {_esc(msg['autor'])}")
    if msg.get("fecha_full"):
        parts.append(f"📅 <b>Fecha:</b> {_esc(msg['fecha_full'])}")
    elif msg.get("fecha"):
        parts.append(f"📅 <b>Fecha:</b> {_esc(msg['fecha'])}")
    cuerpo = msg.get("cuerpo") or ""
    if cuerpo:
        if len(cuerpo) > 3000:
            cuerpo = cuerpo[:3000] + "…"
        parts.append(f"\n💬 {_esc(cuerpo)}")
    if not msg.get("attachments"):
        links = msg.get("links") or []
        if links:
            parts.append("\n🔗 " + " · ".join(
                f'<a href="{_esc(l)}">link{idx+1}</a>' for idx, l in enumerate(links[:5])
            ))
    parts.append(f"\n<i>fuente: {msg.get('source','?')}</i>")
    return "\n".join(parts)
def format_grade_msg(notif: dict) -> str:
    """Notificación Telegram para una calificación nueva o modificada."""
    if notif.get("tipo") == "nueva":
        cabezal = "📊 <b>Nueva calificación cargada</b>"
        nota_line = f"💯 <b>Nota:</b> {_esc(str(notif['valor']))}"
    else:
        cabezal = "📊 <b>Calificación modificada</b>"
        nota_line = (
            f"💯 <b>Nota:</b> {_esc(str(notif.get('valor_anterior','')))} "
            f"→ {_esc(str(notif['valor']))}"
        )
    return (
        f"{cabezal}\n\n"
        f"📌 <b>Materia:</b> {_esc(notif['curso_nombre'])}\n"
        f"📝 <b>Instancia:</b> {_esc(notif['titulo'])}\n"
        f"{nota_line}\n"
        f"\n<i>fuente: a4-notas</i>"
    )
# ── Main ──────────────────────────────────────────────────────────────────────
def _pick_new(msgs: list[dict], seen: set[str]) -> list[tuple[set[str], dict]]:
    """
    Para cada mensaje, calcula su set de IDs equivalentes; si NINGUNO está en
    seen, es nuevo. Además filtra duplicados dentro del mismo batch (a3 vs a4)
    tomando la versión más rica (la que tenga más links o cuerpo más largo).
    """
    nuevos: list[tuple[set[str], dict]] = []
    batch_ids: set[str] = set()
    def score(m: dict) -> tuple[int, int]:
        rich = int(bool(m.get("materia") and m.get("autor") and m.get("fecha")))
        return (rich, len(m.get("cuerpo") or "") + 10 * len(m.get("links") or []))
    for msg in sorted(msgs, key=score, reverse=True):
        ids = ids_for_msg(msg)
        if ids & seen:
            continue
        if ids & batch_ids:
            continue
        batch_ids |= ids
        nuevos.append((ids, msg))
    return nuevos
def main() -> int:
    log.info("=" * 50)
    log.info("Revisando UTN Notas (v3: a3 + a4 + calificaciones)...")
    chat_id = get_chat_id()
    if not chat_id:
        log.error("Sin chat_id de Telegram; no puedo notificar.")
        return 1
    seen = load_seen()
    sent, reason = fetch_and_notify_with_retry(chat_id, seen)
    if sent is None:
        if reason == "login":
            send_telegram(chat_id, "⚠️ <b>UTN Monitor:</b> login rechazado. Revisá usuario/clave.")
            return 2
        log.warning("Fallo transitorio, no se notifica: %s", reason)
        save_seen(seen)
        return 3
    save_seen(seen)
    log.info("Total IDs conocidos: %d", len(seen))
    return 0
if __name__ == "__main__":
    sys.exit(main())
