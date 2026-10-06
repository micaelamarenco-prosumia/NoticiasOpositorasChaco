# -*- coding: utf-8 -*-
"""
BOT DE TELEGRAM - PORTALES OPOSITORES DEL CHACO
Avisa TODAS las notas nuevas (sin filtro) de 5 portales.

Orden de lectura por portal:
  1) RSS (lo detecta solo; reintenta buscarlo cada 6 h si no lo encuentra)
  2) Portada (detecta links de notas)
  3) Google Noticias (site:dominio) si los dos anteriores fallan
"""

import os
import re
import json
import time
import html
import logging
import threading
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin, quote_plus, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor

import feedparser
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

try:
    from googlenewsdecoder import gnewsdecoder as _decoder
except ImportError:
    try:
        from googlenewsdecoder import new_decoderv1 as _decoder
    except ImportError:
        _decoder = None

# ============================================================
# CONFIGURACIÓN
# ============================================================
# Lo ideal es cargarlos como variables en Railway (pestaña Variables).
# Si preferís, pegalos directamente reemplazando el texto entre comillas.
TOKEN = os.getenv("TELEGRAM_TOKEN", "8993352307:AAGXc1Z_s6WiJsFd_K6lk6YHmhnjCAyJ6cs")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "702800514")

INTERVALO = 90               # segundos entre vueltas
FALLAS_PARA_AVISAR = 10      # vueltas seguidas fallando antes de avisar
VISTA_PREVIA = False         # True = Telegram muestra la vista previa del link
BUSCAR_FEED_CADA = 6 * 3600  # si un portal no tiene RSS, reintenta cada 6 h
MAX_RECORDADAS = 3000        # notas recordadas por portal
ARCHIVO_ESTADO = os.getenv("ARCHIVO_ESTADO", "estado_opositores.json")
TZ = ZoneInfo("America/Argentina/Buenos_Aires")

PORTALES = [
    {"nombre": "362 Noticias",  "dominio": "362noticias.com.ar",  "url": "https://362noticias.com.ar/"},
    {"nombre": "Chaco TV",      "dominio": "chacotv.com",         "url": "https://chacotv.com/"},
    {"nombre": "Radio Clan FM", "dominio": "radioclanfm.com",     "url": "https://radioclanfm.com/"},
    {"nombre": "Chaco Latente", "dominio": "chacolatente.com.ar", "url": "https://chacolatente.com.ar/"},
    {"nombre": "TV Local",      "dominio": "tvlocal.com.ar",      "url": "https://tvlocal.com.ar/"},
    # Perfil de Facebook vía RSS.app: solo se lee el feed (sin portada ni Google),
    # se consulta cada 5 min y el título sale del texto del posteo (no viene cortado).
    {"nombre": "Roberto Espinoza (Facebook)", "dominio": "facebook.com",
     "url": "https://www.facebook.com/roberto.espinoza.75",
     "feed": "https://rss.app/feeds/hDgdQR5V3CDhnFLN.xml",
     "solo_rss": True, "cada": 300, "titulo_desde_texto": True},
]
# Si algún portal tiene un RSS conocido, se puede fijar así:
#   {"nombre": "...", "dominio": "...", "url": "...", "feed": "https://.../feed/"}

# ============================================================
# REGISTROS EN HORA ARGENTINA
# ============================================================
class FormatoAR(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, TZ).strftime("%d/%m %H:%M:%S")

_h = logging.StreamHandler()
_h.setFormatter(FormatoAR("%(asctime)s | %(message)s"))
log = logging.getLogger("bot")
log.setLevel(logging.INFO)
log.addHandler(_h)

def ahora():
    return datetime.now(TZ)

# ============================================================
# HTTP (curl_cffi haciéndose pasar por Chrome)
# ============================================================
class Bloqueado(Exception):
    pass

def bajar(url, timeout=20):
    r = creq.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True)
    if r.status_code == 403:
        raise Bloqueado("error 403 (bloqueado)")
    if r.status_code >= 400:
        raise RuntimeError(f"error {r.status_code}")
    return r

# ============================================================
# TELEGRAM
# ============================================================
def tg(metodo, **datos):
    url = f"https://api.telegram.org/bot{TOKEN}/{metodo}"
    for _ in range(3):
        try:
            r = creq.post(url, json=datos, timeout=45)
            j = r.json()
            if j.get("ok"):
                return j
            if r.status_code == 429:
                espera = j.get("parameters", {}).get("retry_after", 5)
                time.sleep(espera + 1)
                continue
            log.warning(f"Telegram {metodo}: {j.get('description')}")
            return j
        except Exception as e:
            log.warning(f"Telegram {metodo} falló: {e}")
            time.sleep(3)
    return None

def enviar(texto):
    tg("sendMessage", chat_id=CHAT_ID, text=texto, parse_mode="HTML",
       link_preview_options={"is_disabled": not VISTA_PREVIA})
    time.sleep(1.1)  # respetar el límite de Telegram

def formato_nota(portal, titulo, link):
    return (f"📰 <b>{html.escape(portal)}</b>\n"
            f"{html.escape(titulo)}\n"
            f"{html.escape(link)}")

# ============================================================
# NORMALIZACIÓN (para no repetir notas)
# ============================================================
def sin_tildes(t):
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))

def norm_titulo(t):
    t = sin_tildes(html.unescape(t or "")).lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def norm_link(u):
    p = urlparse((u or "").strip())
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    q = [(k, v) for k, v in parse_qsl(p.query)
         if not k.lower().startswith(("utm_", "fbclid", "gclid", "amp"))]
    return host + path + ("?" + urlencode(q) if q else "")

# ============================================================
# MEMORIA (qué notas ya se vieron) - se guarda en un archivo
# ============================================================
class Memoria:
    def __init__(self, archivo):
        self.archivo = archivo
        self.lock = threading.Lock()
        self.d = {"links": {}, "titulos": {}, "iniciadas": []}
        try:
            with open(archivo, encoding="utf-8") as f:
                self.d.update(json.load(f))
            log.info(f"Memoria cargada desde {archivo}")
        except FileNotFoundError:
            log.info("Sin memoria previa: la primera vuelta solo memoriza")
        except Exception as e:
            log.warning(f"No pude leer la memoria ({e}); arranco de cero")

    def visto(self, portal, link, titulo):
        with self.lock:
            return (norm_link(link) in self.d["links"].get(portal, [])
                    or (norm_titulo(titulo) and
                        norm_titulo(titulo) in self.d["titulos"].get(portal, [])))

    def marcar(self, portal, links, titulos):
        with self.lock:
            L = self.d["links"].setdefault(portal, [])
            T = self.d["titulos"].setdefault(portal, [])
            for l in links:
                n = norm_link(l)
                if n and n not in L:
                    L.append(n)
            for t in titulos:
                n = norm_titulo(t)
                if n and n not in T:
                    T.append(n)
            del L[:-MAX_RECORDADAS]
            del T[:-MAX_RECORDADAS]

    def iniciada(self, clave):
        with self.lock:
            return clave in self.d["iniciadas"]

    def marcar_iniciada(self, clave):
        with self.lock:
            if clave not in self.d["iniciadas"]:
                self.d["iniciadas"].append(clave)

    def guardar(self):
        with self.lock:
            try:
                tmp = self.archivo + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.d, f, ensure_ascii=False)
                os.replace(tmp, self.archivo)
            except Exception as e:
                log.warning(f"No pude guardar la memoria: {e}")

memoria = Memoria(ARCHIVO_ESTADO)

# ============================================================
# LECTURA: RSS
# ============================================================
def limpiar(t):
    return BeautifulSoup(html.unescape(t or ""), "html.parser").get_text(" ", strip=True)

def primera_linea(descripcion):
    """Primer renglón del texto de un posteo (el 'titular' en Facebook)."""
    soup = BeautifulSoup(descripcion or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for linea in soup.get_text().split("\n"):
        linea = linea.strip()
        if len(linea) >= 15:
            return linea[:400]
    return ""

def leer_rss(feed_url, titulo_desde_texto=False):
    r = bajar(feed_url)
    f = feedparser.parse(r.content)
    items = []
    for e in f.entries[:50]:
        link = e.get("link") or ""
        titulo = limpiar(e.get("title", ""))
        if titulo_desde_texto:
            titulo = primera_linea(e.get("description", "")) or titulo
        if link and titulo:
            items.append({"link": link, "titulo": titulo})
    if not items:
        raise ValueError("RSS vacío o inválido")
    return items

def descubrir_feed(portal):
    candidatos = []
    try:
        r = bajar(portal["url"])
        soup = BeautifulSoup(r.text, "html.parser")
        for l in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            tipo = (l.get("type") or "").lower()
            href = urljoin(portal["url"], l.get("href", ""))
            if ("rss" in tipo or "atom" in tipo) and "comment" not in href:
                candidatos.append(href)
    except Exception:
        pass
    base = portal["url"].rstrip("/")
    candidatos += [base + "/feed/", base + "/rss", base + "/feed",
                   base + "/rss.xml", base + "/index.xml", base + "/feed.xml"]
    for c in dict.fromkeys(candidatos):
        try:
            leer_rss(c)
            return c
        except Exception:
            continue
    return None

# ============================================================
# LECTURA: PORTADA
# ============================================================
SEGMENTOS_NO_NOTA = {
    "category", "categoria", "categorias", "tag", "tags", "etiqueta", "author",
    "autor", "autores", "page", "pagina", "seccion", "secciones", "section",
    "search", "buscar", "busqueda", "wp-content", "wp-admin", "wp-json", "feed",
    "contacto", "quienes-somos", "nosotros", "login", "registro", "cdn-cgi",
    "politica-de-privacidad", "terminos-y-condiciones", "staff", "publicidad",
}

def parece_nota(url, dominio):
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    host = p.netloc.lower().removeprefix("www.")
    if host != dominio:
        return False
    path = p.path.strip("/")
    if not path:
        return False
    if re.search(r"\.(jpe?g|png|gif|webp|svg|pdf|mp3|mp4|xml|css|js)$", path, re.I):
        return False
    segs = [s.lower() for s in path.split("/")]
    if any(s in SEGMENTOS_NO_NOTA for s in segs):
        return False
    mas_largo = max(segs, key=len)
    if mas_largo.count("-") >= 3:          # slug tipo "zdero-anuncio-el-pago"
        return True
    if re.search(r"\d{4,}", path) and len(path) > 12:  # notas con ID numérico
        return True
    return False

def leer_portada(portal):
    r = bajar(portal["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    textos = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(portal["url"], a["href"]).split("#")[0]
        if not parece_nota(url, portal["dominio"]):
            continue
        t = a.get_text(" ", strip=True) or a.get("title", "")
        if len(t) > len(textos.get(url, "")):
            textos[url] = t
    items = [{"link": u, "titulo": t, "verificar_titulo": True} for u, t in textos.items()]
    if not items:
        raise ValueError("no encontré notas en la portada")
    return items

# ============================================================
# LECTURA: GOOGLE NOTICIAS
# ============================================================
def leer_google(portal):
    q = quote_plus(f"site:{portal['dominio']} when:7d")
    url = f"https://news.google.com/rss/search?q={q}&hl=es-419&gl=AR&ceid=AR:es-419"
    r = bajar(url)
    f = feedparser.parse(r.content)
    items = []
    for e in f.entries[:50]:
        titulo = re.sub(r"\s+-\s+[^-]+$", "", limpiar(e.get("title", "")))
        if e.get("link") and titulo:
            items.append({"link": e.link, "titulo": titulo, "google": True})
    if not items:
        raise ValueError("Google Noticias no devolvió notas")
    return items

def link_original(google_url):
    if not _decoder:
        return None
    try:
        res = _decoder(google_url, interval=1)
        if res and res.get("status"):
            return res.get("decoded_url")
    except Exception as e:
        log.info(f"Decoder de Google falló: {e}")
    return None

def titulo_completo(url):
    try:
        soup = BeautifulSoup(bajar(url, timeout=15).text, "html.parser")
        m = (soup.find("meta", property="og:title")
             or soup.find("meta", attrs={"name": "twitter:title"}))
        if m and m.get("content"):
            return limpiar(m["content"])
        if soup.title:
            return soup.title.get_text(strip=True)
    except Exception:
        pass
    return None

# ============================================================
# REVISAR UN PORTAL
# ============================================================
ESTADO = {p["nombre"]: {"feed": p.get("feed"), "busqueda_feed": 0, "fallas": 0,
                        "ultima_consulta": 0,
                        "caido": False, "via": "-", "ultima_ok": None, "enviadas": 0}
          for p in PORTALES}
PAUSADO = threading.Event()

def revisar(portal):
    nombre = portal["nombre"]
    st = ESTADO[nombre]
    items, via, errores = None, None, []

    # Fuentes con intervalo propio (ej: RSS.app cada 5 min)
    if time.time() - st["ultima_consulta"] < portal.get("cada", 0):
        return {"ok": True, "saltear": True}
    st["ultima_consulta"] = time.time()

    # 1) RSS
    if not st["feed"] and time.time() - st["busqueda_feed"] > BUSCAR_FEED_CADA:
        st["busqueda_feed"] = time.time()
        st["feed"] = descubrir_feed(portal)
        log.info(f"{nombre} | RSS {'encontrado: ' + st['feed'] if st['feed'] else 'no encontrado'}")
    if st["feed"]:
        try:
            items, via = leer_rss(st["feed"], portal.get("titulo_desde_texto", False)), "rss"
        except Exception as e:
            errores.append(f"rss: {e}")

    # 2) Portada
    if items is None and not portal.get("solo_rss"):
        try:
            items, via = leer_portada(portal), "portada"
        except Exception as e:
            errores.append(f"portada: {e}")

    # 3) Google Noticias
    if items is None and not portal.get("solo_rss"):
        try:
            items, via = leer_google(portal), "google"
        except Exception as e:
            errores.append(f"google: {e}")

    if items is None:
        return {"ok": False, "errores": errores}

    clave = f"{nombre}|{via}"
    nuevas = [it for it in items if not memoria.visto(nombre, it["link"], it["titulo"])]

    # Primera lectura de esta fuente: solo memoriza
    if not memoria.iniciada(clave):
        for it in nuevas:
            memoria.marcar(nombre, [it["link"]], [it["titulo"]])
        memoria.marcar_iniciada(clave)
        return {"ok": True, "via": via, "leidas": len(items), "nuevas": [],
                "memorizadas": len(nuevas)}

    a_enviar = []
    for it in reversed(nuevas):  # de la más vieja a la más nueva
        link, titulo = it["link"], it["titulo"]
        links, titulos = [link], [titulo]
        if it.get("google"):
            orig = link_original(link)
            if orig:
                link = orig
                links.append(orig)
                t = titulo_completo(orig)
                if t:
                    titulo = t
            else:
                log.info(f"{nombre} | no pude obtener el link original: {titulo[:60]}")
        elif it.get("verificar_titulo") or (len(titulo) < 25 and not portal.get("solo_rss")):
            t = titulo_completo(link)
            if t:
                titulo = t
        titulos.append(titulo)
        if titulo != it["titulo"] and memoria.visto(nombre, link, titulo):
            memoria.marcar(nombre, links, titulos)
            continue
        memoria.marcar(nombre, links, titulos)
        a_enviar.append((titulo, link))

    return {"ok": True, "via": via, "leidas": len(items), "nuevas": a_enviar,
            "memorizadas": 0}

def revisar_seguro(portal):
    try:
        return revisar(portal)
    except Exception as e:
        return {"ok": False, "errores": [f"error inesperado: {e}"]}

# ============================================================
# VUELTA COMPLETA
# ============================================================
def vuelta():
    with ThreadPoolExecutor(max_workers=len(PORTALES)) as ex:
        resultados = list(ex.map(revisar_seguro, PORTALES))

    for portal, res in zip(PORTALES, resultados):
        nombre = portal["nombre"]
        st = ESTADO[nombre]
        if res.get("saltear"):
            continue
        if res["ok"]:
            if st["caido"]:
                enviar(f"✅ <b>{html.escape(nombre)}</b> se recuperó (vía {res['via']}).")
            st.update(fallas=0, caido=False, via=res["via"],
                      ultima_ok=ahora().strftime("%H:%M"))
            enviadas = 0
            for titulo, link in res["nuevas"]:
                if not PAUSADO.is_set():
                    enviar(formato_nota(nombre, titulo, link))
                    enviadas += 1
            st["enviadas"] += enviadas
            extra = f" | memorizadas {res['memorizadas']} (primera lectura)" if res["memorizadas"] else ""
            pausa = " | EN PAUSA" if PAUSADO.is_set() and res["nuevas"] else ""
            log.info(f"{nombre} | vía {res['via']} | leídas {res['leidas']} | "
                     f"enviadas {enviadas}{extra}{pausa}")
        else:
            st["fallas"] += 1
            log.info(f"{nombre} | FALLÓ ({st['fallas']} seguidas) | " + " ; ".join(res["errores"]))
            if st["fallas"] == FALLAS_PARA_AVISAR:
                st["caido"] = True
                enviar(f"⚠️ <b>{html.escape(nombre)}</b> falla hace {FALLAS_PARA_AVISAR} "
                       f"vueltas seguidas.\n" + html.escape(" ; ".join(res["errores"])[:500]))
    memoria.guardar()

# ============================================================
# COMANDOS DE TELEGRAM (solo responde a tu chat)
# ============================================================
AYUDA = ("Comandos:\n"
         "/estado – cómo está cada portal\n"
         "/portales – lista de portales monitoreados\n"
         "/pausa – deja de mandar notas (las sigue memorizando)\n"
         "/reanudar – vuelve a mandar notas\n"
         "/ayuda – este mensaje")

def responder(cmd):
    if cmd == "/estado":
        lineas = ["<b>Estado</b>" + (" (EN PAUSA)" if PAUSADO.is_set() else "")]
        for p in PORTALES:
            st = ESTADO[p["nombre"]]
            icono = "⚠️" if st["caido"] else ("❌" if st["fallas"] else "✅")
            lineas.append(f"{icono} {html.escape(p['nombre'])}: vía {st['via']}, "
                          f"última lectura {st['ultima_ok'] or '-'}, "
                          f"fallas {st['fallas']}, enviadas {st['enviadas']}")
        enviar("\n".join(lineas))
    elif cmd == "/portales":
        enviar("\n".join(f"• {html.escape(p['nombre'])} – {p['url']}" for p in PORTALES))
    elif cmd == "/pausa":
        PAUSADO.set()
        enviar("⏸ En pausa. Sigo leyendo y memorizando, pero no mando notas.")
    elif cmd == "/reanudar":
        PAUSADO.clear()
        enviar("▶️ Reanudado.")
    elif cmd in ("/ayuda", "/start", "/help"):
        enviar(AYUDA)

def escuchar_comandos():
    offset = None
    r = tg("getUpdates", offset=-1, timeout=0)  # descarta mensajes viejos
    if r and r.get("result"):
        offset = r["result"][-1]["update_id"] + 1
    while True:
        r = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
        if not r or not r.get("ok"):
            time.sleep(5)
            continue
        for u in r["result"]:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue
            texto = (msg.get("text") or "").strip()
            if texto.startswith("/"):
                try:
                    responder(texto.split()[0].split("@")[0].lower())
                except Exception as e:
                    log.warning(f"Error respondiendo comando: {e}")

# ============================================================
# INICIO
# ============================================================
def main():
    if "PEGAR" in TOKEN or "PEGAR" in str(CHAT_ID):
        log.error("Falta cargar TELEGRAM_TOKEN y TELEGRAM_CHAT_ID")
        return
    if not _decoder:
        log.warning("googlenewsdecoder no está instalado: se usarán links de Google")
    threading.Thread(target=escuchar_comandos, daemon=True).start()
    enviar(f"🤖 Bot de portales opositores iniciado. Monitoreo {len(PORTALES)} "
           f"fuentes cada {INTERVALO} s, sin filtro.\n/ayuda para ver comandos.")
    while True:
        inicio = time.time()
        log.info("---- Nueva vuelta ----")
        try:
            vuelta()
        except Exception as e:
            log.exception(f"Error en la vuelta: {e}")
        time.sleep(max(5, INTERVALO - (time.time() - inicio)))

if __name__ == "__main__":
    main()
