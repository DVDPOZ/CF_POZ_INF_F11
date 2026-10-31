#!/usr/bin/env python3
"""
extractor.py - Descarga el calendario y la clasificación de una competición de la RFFM
y mantiene al día datos_liga.json y datos_liga.js (los archivos que lee index.html).

USO
    pip install -r requirements.txt
    python extractor.py                 # una sola comprobación y salir
    python extractor.py --vigilar       # comprobar en bucle, sin parar
    python extractor.py --diagnostico   # guarda las páginas y el esquema de datos en ./diagnostico
    python extractor.py "<otra URL>"    # otra competición / grupo

VIGILANCIA (--vigilar)
    Cada comprobación descarga la RFFM, la compara con el datos_liga.json guardado y SOLO
    reescribe los archivos si cambia algo (resultados, horarios, campos, actas o clasificación).
    Intervalo por defecto: cada 5 min en fin de semana (sáb-dom) y cada 60 min entre semana.
        --intervalo-finde MIN        --intervalo-semana MIN
    Si la RFFM falla se conservan los últimos datos y se reintenta con espera creciente.
    Para ver la web en directo usa servidor.py: arranca este vigilante y avisa a la web.

CÓMO EXTRAE LOS DATOS (de más a menos fiable)
  1. La web de la RFFM es una app Next.js que incrusta todos los datos en un bloque JSON
     (<script id="__NEXT_DATA__">). Se lee ese JSON directamente, sin ejecutar JavaScript.
  2. Si ese bloque no contiene partidos, se lee el texto visible de la página.
  3. Si tampoco hay datos, se reintenta con un navegador real (si Playwright está instalado).

ESCUDOS: las URLs de los escudos de cada equipo se leen de la misma página (campos del JSON o
imágenes «Escudo equipo local/visitante»), se descargan una sola vez a la carpeta ./escudos y se
listan en datos_liga.json ("escudos": {equipo: "escudos/archivo.png"}). Si un escudo falla, la web
muestra un escudo genérico con las iniciales del equipo.

El archivo datos_liga.json anterior NUNCA se sobrescribe con datos vacíos o claramente
incompletos: si algo falla, se conserva el último bueno.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup, NavigableString
except ImportError:  # pragma: no cover
    sys.exit("Faltan dependencias. Ejecuta:  pip install -r requirements.txt")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

URL_POR_DEFECTO = (
    "https://www.rffm.es/competicion/calendario?temporada=22&tipojuego=1"
    "&competicion=26748876&grupo=27505221"
)
INTERVALO_FIN_SEMANA_MIN = 5.0    # sábado y domingo
INTERVALO_LABORABLE_MIN = 60.0    # lunes a viernes
REINTENTOS_DESCARGA = 3

CARPETA = Path(__file__).resolve().parent
SALIDA_POR_DEFECTO = CARPETA / "datos_liga.json"
CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-ES,es;q=0.9",
}


def log(*partes):
    print(datetime.now().strftime("[%H:%M:%S]"), *partes, flush=True)


def ahora_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ErrorExtraccion(Exception):
    pass


# --------------------------------------------------------------------------
# Descarga
# --------------------------------------------------------------------------
def construir_urls(url):
    u = urlparse(url)
    q = parse_qs(u.query)
    base = {k: q[k][0] for k in ("temporada", "tipojuego", "competicion", "grupo") if k in q}
    faltan = [k for k in ("temporada", "competicion", "grupo") if k not in base]
    if faltan:
        raise ErrorExtraccion(f"La URL no contiene los parámetros: {', '.join(faltan)}")
    origen = f"{u.scheme}://{u.netloc}"
    # El calendario devuelve todas las jornadas con cualquier valor de "jornada".
    calendario = f"{origen}/competicion/calendario?" + urlencode({**base, "jornada": 1})
    clasificacion = f"{origen}/competicion/clasificaciones?" + urlencode(base)
    return calendario, clasificacion


def descargar(url, reintentos=None):
    reintentos = reintentos or REINTENTOS_DESCARGA
    ultimo = None
    for i in range(reintentos):
        try:
            r = requests.get(url, headers=CABECERAS, timeout=30)
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            ultimo = e
            time.sleep(2 * (i + 1))
    if isinstance(ultimo, requests.HTTPError) and ultimo.response is not None:
        motivo = f"HTTP {ultimo.response.status_code}"
    else:
        motivo = type(ultimo).__name__
    raise ErrorExtraccion(f"La RFFM no responde ({motivo}) al pedir {urlparse(url).path}")


def descargar_con_navegador(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ErrorExtraccion(
            "Para usar el navegador instala Playwright:\n"
            "    pip install playwright && playwright install chromium"
        )
    with sync_playwright() as p:
        nav = p.chromium.launch()
        try:
            pag = nav.new_page(user_agent=CABECERAS["User-Agent"])
            pag.goto(url, wait_until="networkidle", timeout=60000)
            return pag.content()
        finally:
            nav.close()


def extraer_next_data(html):
    tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if tag:
        try:
            return json.loads(tag.get_text())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Utilidades de limpieza
# --------------------------------------------------------------------------
def _nk(clave):
    s = unicodedata.normalize("NFKD", str(clave)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _get(d, candidatos):
    """Devuelve el valor de la primera clave de `candidatos` presente en d (ignora mayúsculas, _ y acentos)."""
    claves = {_nk(k): k for k in d}
    for c in candidatos:
        if c in claves:
            v = d[claves[c]]
            if v is not None and v != "":
                return v
    return None


def _nombre(v):
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        n = _get(v, ("nombre", "nombreequipo", "name", "descripcion", "equipo"))
        return _nombre(n) if n is not None else None
    return None


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        return int(m.group()) if m else None
    return None


def _goles(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
        return int(v)
    return None


def _fecha_hora(v):
    """Devuelve (AAAA-MM-DD, HH:MM) a partir de formatos ISO o DD-MM-AAAA. Hora 00:00 = sin hora."""
    if not isinstance(v, str):
        return None, None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[T\s]+(\d{1,2}):(\d{2}))?", v)
    if m:
        f, hh, mm = f"{m[1]}-{m[2]}-{m[3]}", m[4], m[5]
    else:
        m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:[T\s]+(\d{1,2}):(\d{2}))?", v)
        if not m:
            return None, None
        f, hh, mm = f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}", m[4], m[5]
    hora = f"{int(hh):02d}:{mm}" if hh else None
    return f, (None if hora == "00:00" else hora)


def _hora(v):
    if isinstance(v, str):
        m = re.search(r"(\d{1,2}):(\d{2})", v)
        if m:
            h = f"{int(m[1]):02d}:{m[2]}"
            return None if h == "00:00" else h
    return None


def normaliza(nombre, quitar_siglas=False):
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().upper()
    if quitar_siglas:
        s = re.sub(r"\b(CD|CF|AD|CDE|CDB|CDA|SAD|UD|CLUB|DE|DEL|DEPORTIVO|FUTBOL)\b", " ", re.sub(r"[.]", "", s))
    return re.sub(r"[^A-Z0-9]", "", s)


# --------------------------------------------------------------------------
# Calendario desde el JSON __NEXT_DATA__ (búsqueda tolerante a nombres de campo)
# --------------------------------------------------------------------------
LOCAL = ("equipolocal", "nombreequipolocal", "nombrelocal", "local", "equipocasa", "hometeam", "home")
VISIT = ("equipovisitante", "nombreequipovisitante", "nombrevisitante", "visitante", "equipofuera", "awayteam", "away")
GOL_L = ("golesequipolocal", "goleslocal", "golescasa", "golocal", "resultadolocal", "golesl", "homescore")
GOL_V = ("golesequipovisitante", "golesvisitante", "golesfuera", "golvisitante", "resultadovisitante", "golesv", "awayscore")
FECHA = ("fechapartido", "fecha", "fechajornada", "date")
HORA = ("horapartido", "hora", "time")
CAMPO = ("nombrecampo", "campojuego", "campo", "terrenojuego", "lugar", "estadio")
ACTA = ("urlacta", "enlaceacta", "linkacta", "actapartido", "acta")
COD_ACTA = ("codacta", "codigoacta", "idacta", "actaid", "codactapartido")
JORNADA = ("numerojornada", "numjornada", "njornada", "jornada", "nombrejornada")


CREST_PALABRAS = ("escudo", "logo", "shield", "crest", "imagen", "foto")
LADO_TOKENS = {"local": ("local", "home", "casa"), "visitante": ("visitante", "away", "fuera")}


def _url_img(v):
    """URL (absoluta o relativa) de una imagen, o None. Las imágenes incrustadas (data:) no valen."""
    if isinstance(v, dict):
        v = _get(v, ("url", "src", "href", "path", "ruta", "enlace", "link"))
    if isinstance(v, str):
        v = v.strip()
        if v and not v.startswith("data:") and (v.startswith(("http", "/")) or re.search(r"\.(png|jpe?g|gif|webp|svg)(\?|$)", v, re.I)):
            return v
    return None


def _escudo_json(nodo, lado):
    """Busca la URL del escudo de un equipo dentro de un partido (campo del partido o del propio equipo)."""
    cand = []
    eq = _get(nodo, LOCAL if lado == "local" else VISIT)
    if isinstance(eq, dict):
        cand += list(eq.items())
    cand += [(k, v) for k, v in nodo.items() if any(t in _nk(k) for t in LADO_TOKENS[lado])]
    for k, v in cand:
        if any(w in _nk(k) for w in CREST_PALABRAS):
            u = _url_img(v)
            if u:
                return u
    return None


# Claves adicionales aceptadas solo cuando el diccionario contiene directamente la lista de partidos.
JORNADA_AMPLIA = ("numero", "num", "orden", "nombre", "descripcion", "titulo")


def _jornada_de(d, amplio=False):
    v = _get(d, JORNADA + (JORNADA_AMPLIA if amplio else ()))
    if isinstance(v, dict):
        v = _get(v, ("numero", "jornada", "num", "nombre"))
    return _int(v)


def _url_acta(v):
    """Solo se acepta una URL (absoluta o relativa); un simple identificador no sirve para enlazar."""
    if isinstance(v, dict):
        v = _get(v, ("url", "href", "enlace", "link"))
    if isinstance(v, str) and (v.startswith("http") or v.startswith("/")):
        return v
    return None


def _cod_acta(d):
    """Código numérico del acta de un partido (la web monta con él la dirección del acta)."""
    v = _get(d, COD_ACTA)
    if isinstance(v, (int, str)) and re.fullmatch(r"\d{3,}", str(v).strip()):
        return str(v).strip()
    return None


def _equipos(d):
    loc, vis = _nombre(_get(d, LOCAL)), _nombre(_get(d, VISIT))
    if loc and vis and loc.lower() != vis.lower():
        return loc, vis
    return None


def _directos(v):
    """True si la lista v contiene partidos directamente."""
    return isinstance(v, list) and any(isinstance(x, dict) and _equipos(x) for x in v)


def buscar_partidos(nodo, jornada=None, fecha=None, out=None):
    out = [] if out is None else out
    if isinstance(nodo, list):
        # Lista de jornadas representadas como listas de partidos: la posición es el nº de jornada.
        por_posicion = jornada is None and len(nodo) > 1 and all(_directos(x) for x in nodo)
        for i, x in enumerate(nodo, 1):
            buscar_partidos(x, i if por_posicion else jornada, fecha, out)
    elif isinstance(nodo, dict):
        eq = _equipos(nodo)
        if eq:
            f, h = _fecha_hora(_get(nodo, FECHA))
            h = _hora(_get(nodo, HORA)) or h
            gl, gv = _goles(_get(nodo, GOL_L)), _goles(_get(nodo, GOL_V))
            if gl is None or gv is None:
                gl = gv = None
            out.append({
                "jornada": _jornada_de(nodo) or jornada,
                "fecha": f or fecha,
                "hora": h,
                "equipo_local": eq[0],
                "equipo_visitante": eq[1],
                "resultado_local": gl,
                "resultado_visitante": gv,
                "campo": _nombre(_get(nodo, CAMPO)),
                "acta": _url_acta(_get(nodo, ACTA)),
                "_codacta": _cod_acta(nodo),
                "escudo_local": _escudo_json(nodo, "local"),
                "escudo_visitante": _escudo_json(nodo, "visitante"),
            })
        else:
            j2 = _jornada_de(nodo, amplio=any(_directos(v) for v in nodo.values())) or jornada
            f2 = _fecha_hora(_get(nodo, FECHA) or "")[0] or fecha
            for v in nodo.values():
                if isinstance(v, (list, dict)):
                    buscar_partidos(v, j2, f2, out)
    return out


def limpiar_partidos(partidos):
    vistos, res = set(), []
    for p in partidos:
        if p["jornada"] is None:
            continue
        p.setdefault("acta", None)
        if "descansa" in p["equipo_local"].lower() or "descansa" in p["equipo_visitante"].lower():
            continue
        clave = (p["jornada"], p["equipo_local"], p["equipo_visitante"])
        if clave in vistos:
            continue
        vistos.add(clave)
        res.append(p)
    res.sort(key=lambda p: (p["jornada"], p["fecha"] or "", p["equipo_local"]))
    return res


# --------------------------------------------------------------------------
# Calendario desde el texto visible (formato conocido de la web)
#   JORNADA 4 4 (31-10-2026) / equipo local / marcador / VER ACTA / equipo visitante
# --------------------------------------------------------------------------
RE_JORNADA = re.compile(r"^JORNADA\s+(\d+)(?:\s+\d+)?\s*(?:\((\d{2})[-/](\d{2})[-/](\d{4})\))?", re.I)
RE_MARC = re.compile(r"\d+|[-–]|\d+\s*[-–]\s*\d+")


def parsear_texto_calendario(html, origen=""):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    # Cada enlace "VER ACTA" deja tras de sí una línea marcadora con su URL.
    for a in soup.find_all("a"):
        if a.get_text(strip=True).upper() == "VER ACTA" and a.get("href"):
            a.insert_after(NavigableString("\n@@ACTA@@" + urljoin(origen, a["href"]) + "\n"))
    lineas, actas = [], {}
    for l in (x.strip() for x in soup.get_text("\n").splitlines()):
        if not l or l in ("Escudo equipo local", "Escudo equipo visitante"):
            continue
        if l.startswith("@@ACTA@@"):
            actas[len(lineas) - 1] = l[len("@@ACTA@@"):]
            continue
        lineas.append(l)

    partidos, jornada, fecha_j = [], None, None
    pend_fecha = pend_hora = None
    for k, l in enumerate(lineas):
        m = RE_JORNADA.match(l)
        if m:
            jornada = int(m[1])
            fecha_j = f"{m[4]}-{m[3]}-{m[2]}" if m[2] else None
            pend_fecha = pend_hora = None
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}", l):
            pend_hora = _hora(l)
            continue
        if re.fullmatch(r"\d{1,2}[-/]\d{1,2}[-/]\d{4}", l):
            pend_fecha = _fecha_hora(l)[0]
            continue
        if l.upper() != "VER ACTA" or jornada is None:
            continue
        j, toks = k - 1, []
        while j >= 0 and len(toks) < 3 and RE_MARC.fullmatch(lineas[j]):
            toks.insert(0, lineas[j])
            j -= 1
        if j < 0 or k + 1 >= len(lineas):
            continue
        local, visitante = lineas[j], lineas[k + 1]
        mm = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", " ".join(toks))
        partidos.append({
            "jornada": jornada,
            "fecha": pend_fecha or fecha_j,
            "hora": pend_hora,
            "equipo_local": local,
            "equipo_visitante": visitante,
            "resultado_local": int(mm[1]) if mm else None,
            "resultado_visitante": int(mm[2]) if mm else None,
            "campo": None,
            "acta": actas.get(k),
        })
        pend_fecha = pend_hora = None
    return partidos


# --------------------------------------------------------------------------
# Clasificación
# --------------------------------------------------------------------------
CLAS = {
    "equipo": ("nombre", "nombreequipo", "equipo", "nombreclub", "club"),
    "puntos": ("puntos", "pts", "puntuacion", "puntosequipo"),
    "posicion": ("posicion", "pos", "puesto", "orden"),
    "pj": ("jugados", "partidosjugados", "pj", "pjugados"),
    "pg": ("ganados", "partidosganados", "pg"),
    "pe": ("empatados", "partidosempatados", "pe"),
    "pp": ("perdidos", "partidosperdidos", "pp"),
    "gf": ("golesafavor", "golesfavor", "gf", "golafavor", "golesmarcados"),
    "gc": ("golesencontra", "golescontra", "gc", "golenconcra", "golesrecibidos"),
}


def _fila_clasificacion(d):
    equipo, puntos = _nombre(_get(d, CLAS["equipo"])), _int(_get(d, CLAS["puntos"]))
    if not equipo or puntos is None:
        return None
    fila = {"equipo": equipo, "puntos": puntos}
    for k in ("posicion", "pj", "pg", "pe", "pp", "gf", "gc"):
        fila[k] = _int(_get(d, CLAS[k]))
    return fila


def buscar_clasificacion(nodo):
    candidatas = []

    def rec(n):
        if isinstance(n, list):
            dicts = [x for x in n if isinstance(x, dict)]
            filas = [f for f in (_fila_clasificacion(x) for x in dicts) if f]
            if len(filas) >= 4 and len(filas) == len(dicts):
                candidatas.append(filas)
            for x in n:
                rec(x)
        elif isinstance(n, dict):
            for x in n.values():
                rec(x)

    rec(nodo)
    return max(candidatas, key=len) if candidatas else None


def calcular_clasificacion(partidos):
    equipos = {e for p in partidos for e in (p["equipo_local"], p["equipo_visitante"])}
    t = {e: dict(equipo=e, puntos=0, pj=0, pg=0, pe=0, pp=0, gf=0, gc=0) for e in equipos}
    for p in partidos:
        if p["resultado_local"] is None:
            continue
        gl, gv = p["resultado_local"], p["resultado_visitante"]
        for e, a, b in ((p["equipo_local"], gl, gv), (p["equipo_visitante"], gv, gl)):
            f = t[e]
            f["pj"] += 1
            f["gf"] += a
            f["gc"] += b
            if a > b:
                f["pg"] += 1
                f["puntos"] += 3
            elif a == b:
                f["pe"] += 1
                f["puntos"] += 1
            else:
                f["pp"] += 1
    filas = sorted(t.values(), key=lambda f: (-f["puntos"], -(f["gf"] - f["gc"]), -f["gf"], f["equipo"]))
    for i, f in enumerate(filas, 1):
        f["posicion"] = i
    return filas


def reconciliar_clasificacion(oficial, partidos):
    """Hace coincidir los nombres de la clasificación con los del calendario. None si no es posible."""
    nombres = sorted({e for p in partidos for e in (p["equipo_local"], p["equipo_visitante"])})
    exacto = {normaliza(n): n for n in nombres}
    laxo = {normaliza(n, True): n for n in nombres}
    res = []
    for f in oficial:
        n = exacto.get(normaliza(f["equipo"])) or laxo.get(normaliza(f["equipo"], True))
        if not n:
            return None
        res.append({**f, "equipo": n})
    if len({f["equipo"] for f in res}) != len(nombres) or sum((f["pj"] or 0) for f in res) == 0:
        return None
    for i, f in enumerate(res, 1):
        f["posicion"] = f["posicion"] or i
        for k in ("pj", "pg", "pe", "pp", "gf", "gc"):
            f[k] = f[k] or 0
    res.sort(key=lambda f: f["posicion"])
    return res


# --------------------------------------------------------------------------
# Metadatos y orquestación
# --------------------------------------------------------------------------
def buscar_texto(nodo, claves, prof=0):
    if prof > 8:
        return None
    if isinstance(nodo, dict):
        for k, v in nodo.items():
            if _nk(k) in claves and isinstance(v, str) and v.strip():
                return v.strip()
        for v in nodo.values():
            r = buscar_texto(v, claves, prof + 1)
            if r:
                return r
    elif isinstance(nodo, list):
        for v in nodo[:50]:
            r = buscar_texto(v, claves, prof + 1)
            if r:
                return r
    return None


def parsear_calendario(html, origen=""):
    """Devuelve (partidos, next_data, vía). Las actas relativas se convierten en URLs absolutas."""
    nd = extraer_next_data(html)
    via, partidos = "next_data", []
    if nd is not None:
        partidos = limpiar_partidos(buscar_partidos(nd))
    if not partidos:
        via, partidos = "texto", limpiar_partidos(parsear_texto_calendario(html, origen))
    for p in partidos:
        if p.get("acta") and p["acta"].startswith("/"):
            p["acta"] = origen + p["acta"]
    return partidos, nd, via


def esquema(nodo, prof=0, maxprof=7):
    """Resumen del árbol JSON (claves y tamaños de listas) para diagnosticar cambios en la web."""
    pad = "  " * prof
    if prof > maxprof:
        return pad + "...\n"
    if isinstance(nodo, dict):
        s = ""
        for k, v in nodo.items():
            if isinstance(v, (dict, list)):
                tam = f" [{len(v)}]" if isinstance(v, list) else ""
                s += f"{pad}{k}{tam}:\n" + esquema(v, prof + 1, maxprof)
            else:
                ej = repr(v)[:40]
                s += f"{pad}{k}: {type(v).__name__} = {ej}\n"
        return s
    if isinstance(nodo, list) and nodo:
        return f"{pad}(ejemplo del primer elemento)\n" + esquema(nodo[0], prof, maxprof)
    return ""


# --------------------------------------------------------------------------
# Escudos de los equipos
# --------------------------------------------------------------------------
RE_ALT_ESCUDO = re.compile(r"escudo|logo|shield|crest", re.I)
MAX_BYTES_ESCUDO = 8 * 1024 * 1024


def _src_imagen(tag, origen):
    """URL real de un <img>: src, data-src o srcset; desenvuelve /_next/image?url=... de Next.js."""
    cand = [tag.get("src"), tag.get("data-src"), tag.get("data-lazy-src")]
    for attr in ("srcset", "data-srcset"):
        if tag.get(attr):
            cand.append(tag[attr].split(",")[0].strip().split(" ")[0])
    for c in cand:
        if not c or c.startswith("data:"):
            continue
        u = urlparse(c)
        if u.path.startswith("/_next/image") and parse_qs(u.query).get("url"):
            c = parse_qs(u.query)["url"][0]
        return urljoin(origen + "/", c)
    return None


def _pistas_escudo(el):
    """True si el <img> parece un escudo por su alt, title, aria-label, clase o nombre de archivo."""
    texto = " ".join(str(el.get(a) or "") for a in ("alt", "title", "aria-label", "class", "src", "data-src"))
    return bool(RE_ALT_ESCUDO.search(texto))


def _urls_fondo(el, origen):
    estilo = el.get("style") or ""
    return [urljoin(origen + "/", m) for m in re.findall(r"url\(['\"]?([^'\")]+)['\"]?\)", estilo) if not m.startswith("data:")]


def _imagenes_de(nodo, origen):
    """URLs de todas las imágenes (<img> y fondos CSS) dentro de un nodo, en orden de aparición."""
    res = []
    for el in nodo.descendants:
        if getattr(el, "name", None) == "img":
            u = _src_imagen(el, origen)
            if u:
                res.append(u)
        elif getattr(el, "name", None) and el.get("style") and "url(" in el.get("style"):
            res += _urls_fondo(el, origen)
    return res


def _escudos_por_alt(soup, origen, nombres):
    fichas = []   # ("t", texto) | ("i", url, alt)
    for el in soup.descendants:
        if isinstance(el, NavigableString):
            if el.parent is not None and el.parent.name in ("script", "style", "noscript"):
                continue
            t = str(el).strip()
            if t:
                fichas.append(("t", t))
        elif getattr(el, "name", None) == "img" and _pistas_escudo(el):
            u = _src_imagen(el, origen)
            if u:
                fichas.append(("i", u, (el.get("alt") or el.get("title") or "").strip()))
    res = {}
    for i, f in enumerate(fichas):
        if f[0] != "i":
            continue
        directo = re.sub(RE_ALT_ESCUDO, "", f[2], count=1).strip(" :-–")   # alt «Escudo <equipo>»
        if directo in nombres:
            res.setdefault(directo, f[1])
            continue
        mejor = None
        for d in range(1, 5):
            for j in (i - d, i + d):
                if 0 <= j < len(fichas) and fichas[j][0] == "t" and fichas[j][1] in nombres:
                    mejor = fichas[j][1]
                    break
            if mejor:
                break
        if mejor:
            res.setdefault(mejor, f[1])
    return res


def _escudos_por_filas(soup, origen, nombres):
    """Respaldo sin pistas en el HTML: en cada fila de partido (la que contiene un único «VER ACTA»)
    la primera imagen es el escudo del local y la última el del visitante. Las imágenes que se repiten
    en casi todas las filas (iconos) se descartan."""
    es_acta = lambda a: a.get_text(strip=True).upper() == "VER ACTA"
    filas = []
    for a in soup.find_all("a"):
        if not es_acta(a):
            continue
        nodo = a
        while nodo.parent is not None and nodo.parent.name not in ("body", "html", "[document]"):
            if sum(1 for x in nodo.parent.find_all("a") if es_acta(x)) != 1:
                break
            nodo = nodo.parent
        textos = [str(t).strip() for t in nodo.find_all(string=True) if t.parent.name not in ("script", "style")]
        eq = [t for t in textos if t in nombres]
        if len(eq) >= 2 and eq[0] != eq[-1]:
            filas.append((eq[0], eq[-1], _imagenes_de(nodo, origen)))
    if not filas:
        return {}
    frecuencia = {}
    for _, _, imgs in filas:
        for u in set(imgs):
            frecuencia[u] = frecuencia.get(u, 0) + 1
    icono = {u for u, n in frecuencia.items() if len(filas) >= 6 and n > 0.4 * len(filas)}
    res = {}
    for loc, vis, imgs in filas:
        imgs = [u for u in imgs if u not in icono]
        if len(imgs) >= 2:
            res.setdefault(loc, imgs[0])
            res.setdefault(vis, imgs[-1])
    return res


def escudos_desde_html(html, origen, nombres):
    """{equipo: url}. Primero las imágenes con pinta de escudo (alt, title, clase…) y, para los equipos
    que falten, la posición de las imágenes dentro de cada fila de partido."""
    soup = BeautifulSoup(html, "html.parser")
    res = _escudos_por_alt(soup, origen, nombres)
    if any(n not in res for n in nombres):
        for n, u in _escudos_por_filas(soup, origen, nombres).items():
            res.setdefault(n, u)
    return res


def informe_escudos(html, origen, nombres=(), nd=None):
    """Texto breve (sin datos personales) para ver cómo publica la RFFM los escudos."""
    soup = BeautifulSoup(html, "html.parser")
    corto = lambda x, n=260: re.sub(r"data:[^\"')\s]{20,}", "data:…", str(x)).replace("\n", " ")[:n]
    imgs = soup.find_all("img")
    L = [f"Informe de escudos · {ahora_iso()}", f"Página: {len(html)} caracteres · __NEXT_DATA__: {'sí' if nd else 'no'}",
         f"Etiquetas <img>: {len(imgs)} · con pinta de escudo: {sum(_pistas_escudo(i) for i in imgs)} · "
         f"enlaces «VER ACTA»: {sum(a.get_text(strip=True).upper() == 'VER ACTA' for a in soup.find_all('a'))}",
         f"Equipos conocidos: {len(nombres)}", "", "--- primeras 15 imágenes ---"]
    L += [corto(i) for i in imgs[:15]]
    fondos = [corto(e) for e in soup.find_all(style=re.compile(r"url\("))][:5]
    L += ["", f"--- elementos con imagen de fondo: {len(fondos)} ---"] + fondos
    textos = [t for t in soup.find_all(string=re.compile(r"escudo", re.I)) if t.parent.name not in ("script", "style")][:8]
    L += ["", "--- textos que contienen «escudo» ---"] + [f"<{t.parent.name} class={t.parent.get('class')}> {corto(t.strip(), 80)}" for t in textos]
    claves = []

    def rec(n, ruta=""):
        if len(claves) >= 15:
            return
        if isinstance(n, dict):
            for k, v in n.items():
                if re.search(r"escudo|logo|imagen|shield|crest|foto|img", str(k), re.I) and isinstance(v, (str, dict, list)):
                    claves.append(f"{ruta}/{k} = {corto(json.dumps(v, ensure_ascii=False), 120)}")
                rec(v, f"{ruta}/{k}")
        elif isinstance(n, list):
            for x in n[:30]:
                rec(x, ruta + "[]")
    if nd:
        rec(nd)
    L += ["", "--- claves del JSON relacionadas con imágenes ---"] + (claves or ["(ninguna)"])
    return "\n".join(L) + "\n"


def guardar_informe_escudos(html, origen, nombres, nd=None):
    carpeta = CARPETA / "diagnostico"
    carpeta.mkdir(exist_ok=True)
    ruta = carpeta / "escudos.txt"
    texto = informe_escudos(html, origen, nombres, nd)
    ruta.write_text(texto, encoding="utf-8")
    return ruta, texto


def recopilar_escudos(partidos, html, origen):
    """{equipo: url_remota}. Quita de los partidos los campos temporales escudo_local/escudo_visitante."""
    urls = {}
    for p in partidos:
        for lado in ("local", "visitante"):
            u = p.pop("escudo_" + lado, None)
            if u:
                urls.setdefault(p["equipo_" + lado], urljoin(origen + "/", u))
    nombres = {p[k] for p in partidos for k in ("equipo_local", "equipo_visitante")}
    if any(n not in urls for n in nombres):
        try:
            for n, u in escudos_desde_html(html, origen, nombres).items():
                urls.setdefault(n, u)
        except Exception as e:  # los escudos nunca deben impedir actualizar los resultados
            log(f"Aviso: no se pudieron leer los escudos de la página ({type(e).__name__})")
    return urls


def _tipo_imagen(b):
    if b.startswith(b"\x89PNG"):
        return "png"
    if b.startswith(b"\xff\xd8"):
        return "jpg"
    if b[:4] == b"GIF8":
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    t = b[:2000].lstrip().lower()
    if t.startswith(b"<svg") or (t.startswith(b"<?xml") and b"<svg" in t):
        return "svg"
    return None


CABECERAS_IMG = {
    "User-Agent": CABECERAS["User-Agent"],
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Site": "same-origin",
}


def _a_imagen(b):
    """Devuelve los bytes de la imagen o None. Acepta también una imagen en base64 / data: URI / JSON con base64."""
    if _tipo_imagen(b):
        return b
    t = b.strip()
    if t.startswith(b"{"):
        try:
            for v in json.loads(t).values():
                if isinstance(v, str) and len(v) > 100:
                    t = v.encode()
                    break
        except ValueError:
            return None
    if t.startswith(b"data:image"):
        t = t.split(b",", 1)[-1]
    if len(t) > 100 and re.fullmatch(rb"[A-Za-z0-9+/=\s]+", t[:4000]):
        try:
            raw = base64.b64decode(t)
            return raw if _tipo_imagen(raw) else None
        except (ValueError, TypeError):
            return None
    return None


def _sesion_rffm(referer):
    """Sesión que primero visita la página del calendario (cookies) y pide las imágenes como lo haría el navegador."""
    ses = requests.Session()
    ses.headers.update(CABECERAS_IMG)
    ses.headers["Referer"] = referer
    try:
        ses.get(referer, headers=CABECERAS, timeout=30)
    except requests.RequestException:
        pass
    return ses


# Los escudos viven en otro servidor de la RFFM y la web los pide a través del optimizador de imágenes de
# Next.js:  <origen>/_next/image?url=<BASE_IMAGENES + ruta>&w=256&q=75   (la ruta empieza por «/», de ahí la doble barra).
BASE_IMAGENES = "https://appweb.rffm.es/"
ACCEPT_NEXT = "image/webp,image/png,image/jpeg,image/*;q=0.8"   # sin AVIF: así llega webp/png/jpeg


def _url_next_image(origen, destino, w=256, q=75):
    return f"{origen}/_next/image?url={quote(destino, safe='')}&w={w}&q={q}"


IGNORAR_HOSTS = ("w3.org", "google", "gstatic", "facebook", "twitter", "github", "reactjs", "nextjs", "schema.org", "youtube",
                 "instagram", "apple.com", "mozilla", "vercel", "cloudflare", "jsdelivr", "unpkg", "fontawesome", "wikipedia",
                 "linkedin", "whatsapp", "tiktok", "sentry", "doubleclick", "cookiebot", "onetrust", "fb.com", "paypal")
RE_URL_LIT = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9._~%/-]*)?")


def _ruta_de(url):
    u = urlparse(url)
    return u.path + (("?" + u.query) if u.query else "")


def _textos_js(html, origen, ses, max_archivos=25):
    """Texto de los JavaScript propios de la página (ahí está el código que construye las URLs de los escudos)."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(origen).netloc
    out = []
    for t in soup.find_all("script", src=True)[:max_archivos]:
        u = urljoin(origen + "/", t["src"])
        if urlparse(u).netloc != host:
            continue
        try:
            r = ses.get(u, timeout=30)
        except requests.RequestException:
            continue
        if r.ok and len(r.content) < 4_000_000:
            out.append((u, r.text))
    return out


def candidatos_base_imagen(html, origen, textos_js):
    """Bases (https://host[/prefijo]) que la página menciona y podrían alojar los escudos, las más probables primero."""
    textos = [html] + [t for _, t in textos_js]
    host_origen = urlparse(origen).netloc
    puntos = {}
    for t in textos:
        for m in RE_URL_LIT.findall(t):
            lit = m.rstrip("/.,")
            host = urlparse(lit).netloc.lower()
            if not host or any(x in host for x in IGNORAR_HOSTS) or re.search(r"\.(js|css|png|jpe?g|svg|ico|woff2?)$", lit, re.I):
                continue
            pts = (3 if re.search(r"pimg|clubes|escudo|imagen|pnfg", lit, re.I) else 0) + (2 if "rffm" in host else 0) + (1 if host != host_origen else 0)
            puntos[lit] = max(puntos.get(lit, 0), pts)
    orden = sorted(puntos, key=lambda x: -puntos[x])[:20]
    bases = []
    for lit in orden:
        u = urlparse(lit)
        for b in (lit, f"{u.scheme}://{u.netloc}"):
            if b not in bases:
                bases.append(b)
    for extra in (origen.replace("://www.", "://"), origen.replace("https://", "http://")):   # variantes sin www / sin https
        if extra != origen and extra not in bases:
            bases.append(extra)
    return bases


def contexto_js(textos_js, patron=r"escudo", ancho=110, maximo=8):
    """Fragmentos del código JavaScript de la página donde se usa `patron` (para ver cómo arma las URLs)."""
    res = []
    for nombre, t in textos_js:
        for m in re.finditer(patron, t, re.I):
            res.append(f"[{nombre.rsplit('/', 1)[-1]}] …{t[max(0, m.start() - ancho): m.end() + ancho]!r}…")
            if len(res) >= maximo:
                return res
    return res


class Bajador:
    """Descarga imágenes de los escudos con sesión de navegador y, si la dirección de la página no devuelve
    una imagen, busca la base correcta (otro host o prefijo) en el HTML y los JavaScript de la propia página."""

    def __init__(self, origen, referer=None, html=None, base_previa=None, verboso=False):
        self.origen, self.referer, self.html = origen, referer or origen + "/", html
        self.base, self.verboso = base_previa, verboso
        self.ses, self.descubierto, self.textos_js = None, False, []

    def sesion(self):
        if self.ses is None:
            self.ses = _sesion_rffm(self.referer)
        return self.ses

    def _pedir(self, url, detalles, cabeceras=None):
        try:
            r = self.sesion().get(url, timeout=20, headers=cabeceras)
        except requests.RequestException as e:
            detalles.append(f"{type(e).__name__}")
            return None
        cuerpo = r.content or b""
        if r.ok and len(cuerpo) <= MAX_BYTES_ESCUDO:
            datos = _a_imagen(cuerpo)
            if datos:
                return datos
        detalles.append(f"HTTP {r.status_code} {r.headers.get('Content-Type', '?').split(';')[0]} {len(cuerpo)} B"
                        + f" «{cuerpo[:50]!r}»")
        return None

    def bajar(self, url):
        """Devuelve los bytes de la imagen o lanza ValueError con el detalle de lo que respondió el servidor."""
        detalles = []
        path = _ruta_de(url) if url.startswith(self.origen) else None
        intentos = []   # (dirección, cabeceras extra)
        if path:
            for base in dict.fromkeys(b for b in (self.base, BASE_IMAGENES) if b):
                destino = base + path
                intentos.append((_url_next_image(self.origen, destino), {"Accept": ACCEPT_NEXT}))   # como la web
                intentos.append((destino, None))                                                    # directo al servidor
        intentos.append((url, None))
        for c, cab in intentos:
            datos = self._pedir(c, detalles, cab)
            if datos:
                return datos
        if path and self.html and not self.descubierto:
            self.descubierto = True
            self.textos_js = _textos_js(self.html, self.origen, self.sesion())
            bases = candidatos_base_imagen(self.html, self.origen, self.textos_js)
            if self.verboso:
                print(f"Buscando dónde están los escudos: {len(bases)} direcciones candidatas en la página")
            for b in bases:
                datos = self._pedir(b.rstrip("/") + path, [])
                if self.verboso:
                    print(f"   {'✔' if datos else '✘'} {b}{path[:50]}")
                if datos:
                    self.base = b
                    log(f"Los escudos están en {b}")
                    return datos
            detalles.append(f"probadas {len(bases)} direcciones alternativas de la página sin éxito")
        raise ValueError("no es una imagen · " + " | ".join(detalles))


def _guardar_escudo(datos, url, nombre, carpeta):
    ext = _tipo_imagen(datos)
    base = (normaliza(nombre).lower() or "equipo")[:60]
    destino = Path(carpeta) / f"{base}.{ext}"
    for viejo in Path(carpeta).glob(base + ".*"):   # un solo archivo por equipo
        if viejo != destino:
            viejo.unlink(missing_ok=True)
    fd, tmp = tempfile.mkstemp(dir=carpeta, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(datos)
    os.replace(tmp, destino)
    return f"escudos/{destino.name}?v={hashlib.md5(url.encode()).hexdigest()[:8]}"


def descargar_escudos(urls, carpeta, previo, origen="https://www.rffm.es", referer=None, html=None):
    """Descarga los escudos que faltan o han cambiado. Devuelve ({equipo: ruta}, {equipo: url_origen}).
    Conserva lo anterior si algo falla o si esta vez no se encontraron escudos."""
    carpeta = Path(carpeta)
    escudos = dict(previo.get("escudos") or {})
    fuentes = dict(previo.get("escudos_fuente") or {})
    if not urls:
        return escudos, fuentes
    carpeta.mkdir(parents=True, exist_ok=True)
    nuevos = fallos = 0
    por_url = {}
    baj = Bajador(origen, referer, html, fuentes.get("_base"))
    for nombre, url in sorted(urls.items()):
        ruta = escudos.get(nombre)
        if ruta and fuentes.get(nombre) == url and (carpeta.parent / ruta.split("?")[0]).exists():
            continue
        if url in por_url:
            escudos[nombre], fuentes[nombre] = por_url[url], url
            continue
        try:
            escudos[nombre] = por_url[url] = _guardar_escudo(baj.bajar(url), url, nombre, carpeta)
            fuentes[nombre] = url
            nuevos += 1
        except (requests.RequestException, ValueError, OSError) as e:
            fallos += 1
            log(f"  Escudo de {nombre}: no se pudo descargar ({type(e).__name__}: {e})"[:600])
    if baj.base:
        fuentes["_base"] = baj.base
    if nuevos or fallos:
        log(f"Escudos: {nuevos} descargados" + (f", {fallos} con error" if fallos else ""))
    return escudos, fuentes


def diagnostico(url, navegador):
    carpeta = CARPETA / "diagnostico"
    carpeta.mkdir(exist_ok=True)
    for nombre, u in zip(("calendario", "clasificacion"), construir_urls(url)):
        html = descargar_con_navegador(u) if navegador else descargar(u)
        (carpeta / f"{nombre}.html").write_text(html, encoding="utf-8")
        nd = extraer_next_data(html)
        if nd is None:
            print(f"[{nombre}] No hay bloque __NEXT_DATA__ (HTML de {len(html)} caracteres).")
            continue
        (carpeta / f"{nombre}_next_data.json").write_text(json.dumps(nd, ensure_ascii=False, indent=1), encoding="utf-8")
        (carpeta / f"{nombre}_esquema.txt").write_text(esquema(nd), encoding="utf-8")
        print(f"[{nombre}] guardado en {carpeta}")
    print("Si la extracción falla, el contenido de la carpeta 'diagnostico' permite ajustar el script.")


def extraer(url, navegador=False):
    url_cal, url_cla = construir_urls(url)
    u = urlparse(url)
    origen = f"{u.scheme}://{u.netloc}"
    via_nav = False

    html_cal = descargar_con_navegador(url_cal) if navegador else descargar(url_cal)
    partidos, nd, via = parsear_calendario(html_cal, origen)
    if not partidos and not navegador:
        try:
            log("Sin datos en el HTML descargado; reintentando con navegador...")
            html_cal = descargar_con_navegador(url_cal)
            partidos, nd, via = parsear_calendario(html_cal, origen)
            via_nav = True
        except ErrorExtraccion as e:
            log(f"  (navegador no disponible: {str(e).splitlines()[0]})")
    if not partidos:
        raise ErrorExtraccion(
            "No se encontraron partidos. La web puede haber cambiado su estructura.\n"
            "Ejecuta:  python extractor.py --diagnostico   y revisa la carpeta 'diagnostico'."
        )

    asignar_actas(partidos, url, origen)
    escudos_url = recopilar_escudos(partidos, html_cal, origen)
    if not escudos_url:
        nombres = {p[k] for p in partidos for k in ("equipo_local", "equipo_visitante")}
        try:
            ruta, _ = guardar_informe_escudos(html_cal, origen, nombres, nd)
            log(f"Aviso: no se encontraron escudos en la página (se mostrarán escudos genéricos). Informe en {ruta}")
        except OSError:
            log("Aviso: no se encontraron escudos en la página (se mostrarán escudos genéricos).")

    # Clasificación oficial (opcional): si falla, se calcula con los resultados.
    oficial, origen = None, "calculada"
    try:
        html_cla = descargar_con_navegador(url_cla) if (navegador or via_nav) else descargar(url_cla)
        nd_cla = extraer_next_data(html_cla)
        filas = buscar_clasificacion(nd_cla) if nd_cla is not None else None
        oficial = reconciliar_clasificacion(filas, partidos) if filas else None
        if oficial:
            origen = "oficial"
        else:
            log("Aviso: no se pudo leer la clasificación oficial; se calcula con los resultados.")
    except ErrorExtraccion as e:
        log(f"Aviso: clasificación oficial no disponible ({str(e).splitlines()[0]}); se calcula.")

    return {
        "fuente": url_cal,
        "origen_datos": ("navegador+" if (navegador or via_nav) else "") + via,
        "clasificacion_origen": origen,
        "partidos": partidos,
        "clasificacion": oficial or calcular_clasificacion(partidos),
        "_escudos_urls": escudos_url,
        "_html_cal": html_cal,
        "_meta": {
            "temporada": buscar_texto(nd, {"nombretemporada", "temporada"}) if nd else None,
            "categoria": buscar_texto(nd, {"nombrecompeticion", "competicion"}) if nd else None,
        },
    }


# --------------------------------------------------------------------------
# Actas de los partidos: alineaciones (titulares / suplentes), goles y tarjetas
# --------------------------------------------------------------------------
# La web de la RFFM publica cada acta en  <origen>/acta-partido/<codacta>?temporada=&competicion=&grupo=
# y la incrusta (como el calendario) en el bloque __NEXT_DATA__ -> props.pageProps.game.
RE_COD_EN_URL = re.compile(r"/acta-partido/(\d+)")
CLAVES_NOMBRE = ("nombrejugador", "nombrejugadora", "nombrecompleto", "jugador", "jugadora", "nombre", "name")
CLAVES_APELLIDOS = ("apellidos", "apellido", "apellidosjugador")
CLAVES_DORSAL = ("dorsal", "numero", "num", "dorsaljugador", "numerodorsal", "number")
CLAVES_MINUTO = ("minuto", "min", "minutos", "minutogol", "minutotarjeta", "tiempo", "minute")
RE_PIDE_TARJETA = re.compile(r"tarjeta|amonest|expuls|cartul|sancion|amarill|roja|disciplin", re.I)
MAX_ACTAS_POR_CICLO = 40
PAUSA_ENTRE_ACTAS_S = 0.6
_REVISADAS = {}   # codacta -> instante de la última descarga (en memoria: sirve para releer las actas recientes)


def url_acta(origen, cod, url_base):
    q = parse_qs(urlparse(url_base).query)
    par = {k: q[k][0] for k in ("temporada", "competicion", "grupo") if k in q}
    return f"{origen}/acta-partido/{cod}" + ("?" + urlencode(par) if par else "")


def cod_de_url(u):
    m = RE_COD_EN_URL.search(u or "")
    return m.group(1) if m else None


def asignar_actas(partidos, url_base, origen):
    """Deja en cada partido su cod_acta y, si ya se ha jugado, la dirección de su acta."""
    for p in partidos:
        cod = p.pop("_codacta", None) or cod_de_url(p.get("acta"))
        if cod:
            p["cod_acta"] = cod
            if p.get("resultado_local") is not None and not p.get("acta"):
                p["acta"] = url_acta(origen, cod, url_base)
        elif p.get("resultado_local") is None:
            p["acta"] = None


# --------------------------------------------------------------------------
# Resultados provisionales sacados del acta (la RFFM suele publicar el acta antes que el resultado en el calendario)
# --------------------------------------------------------------------------
MAX_ACTAS_PENDIENTES_POR_CICLO = 16
DURACION_PARTIDO_MIN = 100          # no se mira el acta hasta que, por horario, el partido ya debería haber terminado
DIAS_MAX_PENDIENTE = 10             # pasado este tiempo sin resultado oficial se deja de insistir
RE_GOL_LOCAL = re.compile(r"(gol|result|marcador|anotad|score).*(local|casa|home)|(local|casa|home).*(gol|result|marcador|anotad|score)")
RE_GOL_VISIT = re.compile(r"(gol|result|marcador|anotad|score).*(visit|fuera|away)|(visit|fuera|away).*(gol|result|marcador|anotad|score)")


def _entero(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 <= v <= 60 else None
    if isinstance(v, str) and re.fullmatch(r"\s*\d{1,2}\s*", v):
        return int(v)
    return None


def _marcador_en_nodo(nodo, prof=0):
    """Busca en el JSON pares de campos tipo golesLocal / golesVisitante (o resultadoLocal / resultadoVisitante)."""
    if prof > 6:
        return None
    if isinstance(nodo, dict):
        gl = gv = None
        for k, v in nodo.items():
            nk = _nk(k)
            n = _entero(v)
            if n is None or "equipo" in nk and not re.search(r"gol|result|marcador", nk):
                continue
            if RE_GOL_LOCAL.search(nk) and not RE_GOL_VISIT.search(nk):
                gl = n if gl is None else gl
            elif RE_GOL_VISIT.search(nk):
                gv = n if gv is None else gv
        if gl is not None and gv is not None:
            return gl, gv
        for v in nodo.values():
            r = _marcador_en_nodo(v, prof + 1)
            if r:
                return r
    elif isinstance(nodo, list):
        for v in nodo[:60]:
            r = _marcador_en_nodo(v, prof + 1)
            if r:
                return r
    return None


def marcador_desde_nd(nd, partido, url=""):
    """(goles_local, goles_visitante) leídos del acta, o None si no se puede saber con seguridad.
    1) campos explícitos de resultado; 2) si no los hay, recuento de goles del acta (solo si hay al menos un gol:
    un 0-0 no se puede distinguir de un partido sin empezar)."""
    try:
        raiz = nd["props"]["pageProps"]
    except (KeyError, TypeError):
        return None
    game = raiz.get("game") if isinstance(raiz.get("game"), (dict, list)) else raiz
    m = _marcador_en_nodo(game)
    if m:
        return m
    acta = acta_desde_next_data(nd, partido, url)
    if acta and acta["goles"]:
        gl = gv = 0
        for g in acta["goles"]:
            lado = g.get("lado")
            if lado is None:
                return None                   # no sabemos de quién es un gol: mejor no inventar
            propia = g.get("tipo") == "propia puerta"
            a_local = (lado == "local") != propia
            gl += a_local
            gv += not a_local
        return gl, gv
    return None


def _inicio_partido(p):
    try:
        f = datetime.strptime(p["fecha"], "%Y-%m-%d")
    except (KeyError, TypeError, ValueError):
        return None
    h = p.get("hora")
    if h and re.fullmatch(r"\d{1,2}:\d{2}", h):
        f = f.replace(hour=int(h.split(":")[0]), minute=int(h.split(":")[1]))
        return f, True
    return f + timedelta(days=1), False         # sin hora: se espera a que acabe el día


def _ahora_madrid():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def completar_resultados_con_actas(partidos, url_base, origen, ahora=None):
    """Para partidos que ya deberían haber terminado y aún no tienen resultado en el calendario, mira su acta.
    Si el acta ya trae el marcador, se usa como resultado PROVISIONAL (resultado_fuente = "acta"); en cuanto el
    calendario oficial lo publique, el resultado oficial lo sustituye. Un fallo aquí nunca detiene la actualización."""
    ahora = ahora or _ahora_madrid()
    n = 0
    candidatos = []
    for p in partidos:
        if p.get("resultado_local") is not None or not p.get("cod_acta"):
            continue
        ini = _inicio_partido(p)
        if not ini:
            continue
        inicio, con_hora = ini
        fin = inicio + (timedelta(minutes=DURACION_PARTIDO_MIN) if con_hora else timedelta(0))
        if ahora < fin or (ahora - fin).days > DIAS_MAX_PENDIENTE:
            continue
        candidatos.append(p)
    for p in candidatos[:MAX_ACTAS_PENDIENTES_POR_CICLO]:
        u = url_acta(origen, p["cod_acta"], url_base)
        try:
            nd = extraer_next_data(descargar(u, reintentos=2))
            m = marcador_desde_nd(nd, p, u) if nd else None
        except ErrorExtraccion as e:
            log(f"  Resultado por acta J{p['jornada']} {p['equipo_local']} - {p['equipo_visitante']}: no se pudo leer ({str(e).splitlines()[0]})")
            continue
        except Exception as e:                     # nunca debe romper la actualización
            log(f"  Resultado por acta: error inesperado ({type(e).__name__}: {e})")
            continue
        if not m and nd:
            try:   # esquema SIN nombres de un acta sin marcador, para poder ajustar el lector con datos reales
                f = CARPETA / "diagnostico" / "acta_pendiente_esquema.txt"
                if not f.exists():
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(esquema_anonimo(nd["props"]["pageProps"]), encoding="utf-8")
            except Exception:
                pass
        if m:
            p["resultado_local"], p["resultado_visitante"] = m
            p["resultado_fuente"] = "acta"
            p["acta"] = u
            n += 1
            log(f"  Resultado provisional por acta J{p['jornada']} {p['equipo_local']} {m[0]}-{m[1]} {p['equipo_visitante']}")
        time.sleep(PAUSA_ENTRE_ACTAS_S)
    if n:
        log(f"Resultados provisionales sacados de actas: {n}")
    return n


def _persona(d):
    nom = None
    for k in CLAVES_NOMBRE:
        v = _get(d, (k,))
        if isinstance(v, dict):
            v = _nombre(v)
        if isinstance(v, str) and v.strip() and not v.strip().isdigit():
            nom = v.strip()
            break
    if not nom:
        return None
    ap = _get(d, CLAVES_APELLIDOS)
    if isinstance(ap, str) and ap.strip() and ap.strip().lower() not in nom.lower():
        nom = f"{ap.strip()}, {nom}"
    return nom


def _dorsal(d):
    v = _get(d, CLAVES_DORSAL)
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 99 else None


def _minuto(d):
    v = _get(d, CLAVES_MINUTO)
    m = re.match(r"\d{1,3}", str(v)) if v is not None else None
    return int(m.group()) if m else None


def _verdadero(v):
    return v is True or (isinstance(v, (int, float)) and v == 1 and not isinstance(v, bool)) or \
        (isinstance(v, str) and v.strip().upper() in ("S", "SI", "SÍ", "T", "TRUE", "1", "TITULAR", "X"))


def _listas_de_dicts(nodo, ruta=(), out=None):
    out = [] if out is None else out
    if isinstance(nodo, list):
        ds = [x for x in nodo if isinstance(x, dict)]
        if ds and len(ds) >= 0.8 * len(nodo):
            out.append((ruta, ds))
        for x in nodo:
            _listas_de_dicts(x, ruta + ("[]",), out)
    elif isinstance(nodo, dict):
        for k, v in nodo.items():
            _listas_de_dicts(v, ruta + (str(k),), out)
    return out


def _lado_por_texto(texto, partido):
    t = _nk(texto)
    if re.search(r"visitante|fuera|away", t):
        return "visitante"
    if re.search(r"local|casa|home", t):
        return "local"
    n = normaliza(texto) if isinstance(texto, str) else ""
    if n and n == normaliza(partido["equipo_local"]):
        return "local"
    if n and n == normaliza(partido["equipo_visitante"]):
        return "visitante"
    return None


def _lado_item(ruta, item, partido):
    lado = _lado_por_texto(" ".join(ruta), partido)
    if lado:
        return lado
    for k, v in item.items():
        if isinstance(v, str) and re.search(r"equipo|club", _nk(k)):
            lado = _lado_por_texto(v, partido)
            if lado:
                return lado
        if re.search(r"^(es)?local$|^eslocal$", _nk(k)) and isinstance(v, bool):
            return "local" if v else "visitante"
    return None


def _rol_titular(ruta, item):
    ptxt = _nk(" ".join(ruta))
    if re.search(r"suplente|banquillo|reserva", ptxt):
        return False
    if re.search(r"titular|once|inicial|alineacion", ptxt):
        return True
    for k, v in item.items():
        nk = _nk(k)
        if re.fullmatch(r"(es)?titular(idad)?|alineado|inicial|once", nk):
            return _verdadero(v)
        if re.fullmatch(r"tipo|condicion|posicion|estado|situacion|rol", nk) and isinstance(v, str):
            if re.search(r"suplente|banquillo|reserva", v, re.I):
                return False
            if re.search(r"titular|once|inicial", v, re.I):
                return True
    return None


def _tipo_tarjeta(item):
    t = " ".join(str(v) for k, v in item.items() if isinstance(v, (str, int)) and re.search(r"tipo|color|tarjeta|descripcion|motivo|sancion", _nk(k)))
    if re.search(r"doble|segunda", t, re.I):
        return "doble amarilla"
    if re.search(r"roja|expuls|directa|\bR\b", t, re.I):
        return "roja"
    if re.search(r"amarill|amonest|\bA\b", t, re.I):
        return "amarilla"
    return None


def _tipo_gol(item):
    t = " ".join(str(v) for k, v in item.items() if isinstance(v, str) and re.search(r"tipo|descripcion|clase|modalidad", _nk(k)))
    if re.search(r"propia|autogol|p\.?p\.?", t, re.I):
        return "propia puerta"
    if re.search(r"penalt|penalty", t, re.I):
        return "penalti"
    return "normal"


def acta_desde_next_data(nd, partido, url):
    """Normaliza el acta de un partido: {local/visitante: [jugadoras], goles, tarjetas}. None si no reconoce el formato."""
    try:
        pp = nd["props"]["pageProps"]
    except (KeyError, TypeError):
        return None
    raiz = pp.get("game") if isinstance(pp.get("game"), (dict, list)) else pp
    jugadoras = {"local": {}, "visitante": {}}
    sin_lado = []
    goles, tarjetas = [], []
    for ruta, items in _listas_de_dicts(raiz):
        ptxt = _nk(" ".join(ruta))
        con_minuto = sum(_minuto(i) is not None for i in items)
        con_dorsal = sum(1 for i in items if _dorsal(i) is not None and _persona(i))
        if con_dorsal >= 0.5 * len(items) and not (con_minuto >= 0.8 * len(items) and re.search(r"gol|tarjeta|amonest|expuls|incidenc|evento", ptxt)):
            lista = []
            for it in items:
                nom = _persona(it)
                if not nom:
                    continue
                lista.append({"dorsal": _dorsal(it), "nombre": nom, "titular": _rol_titular(ruta, it),
                              "_g": next((int(v) for k, v in it.items() if re.fullmatch(r"goles?|golesmarcados", _nk(k)) and str(v).isdigit()), 0),
                              "_a": next((int(v) for k, v in it.items() if re.fullmatch(r"(tarjetas?)?amarillas?|ta", _nk(k)) and str(v).isdigit()), 0),
                              "_r": next((int(v) for k, v in it.items() if re.fullmatch(r"(tarjetas?)?rojas?|tr", _nk(k)) and str(v).isdigit()), 0)})
            if not lista:
                continue
            lados = {_lado_item(ruta, it, partido) for it in items} - {None}
            if len(lados) == 1:
                lado = lados.pop()
                for j in lista:
                    jugadoras[lado].setdefault((j["dorsal"], normaliza(j["nombre"])), j)
            else:
                sin_lado.append(lista)
        elif con_minuto >= 0.6 * len(items) and re.search(r"gol", ptxt) and not RE_PIDE_TARJETA.search(ptxt):
            for it in items:
                goles.append({"min": _minuto(it), "lado": _lado_item(ruta, it, partido), "dorsal": _dorsal(it),
                              "jugadora": _persona(it) or next((_persona(v) for v in it.values() if isinstance(v, dict)), None), "tipo": _tipo_gol(it)})
        elif con_minuto >= 0.6 * len(items) and RE_PIDE_TARJETA.search(ptxt):
            for it in items:
                tarjetas.append({"min": _minuto(it), "lado": _lado_item(ruta, it, partido), "dorsal": _dorsal(it),
                                 "jugadora": _persona(it) or next((_persona(v) for v in it.values() if isinstance(v, dict)), None), "tipo": _tipo_tarjeta(it) or "amarilla"})
    # listas sin lado reconocible: se reparten por orden de aparición (primero el local)
    for lado, lista in zip(("local", "visitante"), [x for x in sin_lado if not jugadoras["local"] or not jugadoras["visitante"]][:2]):
        if not jugadoras[lado]:
            for j in lista:
                jugadoras[lado].setdefault((j["dorsal"], normaliza(j["nombre"])), j)
    if not jugadoras["local"] and not jugadoras["visitante"]:
        return None
    # contadores por jugadora (si el acta los da así, sin minutos) -> eventos sin minuto
    for lado in ("local", "visitante"):
        for j in jugadoras[lado].values():
            for _ in range(j.pop("_g", 0)) if not any(g for g in goles) else ():
                goles.append({"min": None, "lado": lado, "dorsal": j["dorsal"], "jugadora": j["nombre"], "tipo": "normal"})
            for _ in range(j.pop("_a", 0)) if not any(t for t in tarjetas) else ():
                tarjetas.append({"min": None, "lado": lado, "dorsal": j["dorsal"], "jugadora": j["nombre"], "tipo": "amarilla"})
            for _ in range(j.pop("_r", 0)) if not any(t for t in tarjetas) else ():
                tarjetas.append({"min": None, "lado": lado, "dorsal": j["dorsal"], "jugadora": j["nombre"], "tipo": "roja"})
            j.pop("_g", None); j.pop("_a", None); j.pop("_r", None)
    # evento sin lado -> el de la jugadora a la que pertenece (por dorsal o nombre)
    for ev in goles + tarjetas:
        if ev["lado"] is None:
            for lado in ("local", "visitante"):
                for j in jugadoras[lado].values():
                    if (ev["jugadora"] and normaliza(ev["jugadora"]) == normaliza(j["nombre"])) or (ev["jugadora"] is None and ev["dorsal"] is not None and ev["dorsal"] == j["dorsal"]):
                        ev["lado"] = lado
    orden = lambda e: (e["min"] if e["min"] is not None else 999)
    return {
        "cod": cod_de_url(url), "url": url, "jornada": partido["jornada"],
        "local": partido["equipo_local"], "visitante": partido["equipo_visitante"],
        "jugadoras_local": sorted(jugadoras["local"].values(), key=lambda j: (j["titular"] is not True, j["dorsal"] if j["dorsal"] is not None else 999)),
        "jugadoras_visitante": sorted(jugadoras["visitante"].values(), key=lambda j: (j["titular"] is not True, j["dorsal"] if j["dorsal"] is not None else 999)),
        "goles": sorted(goles, key=orden), "tarjetas": sorted(tarjetas, key=orden),
    }


def esquema_anonimo(nodo, prof=0, maxprof=9, _cuentas=None):
    """Esquema del JSON sin datos personales: números y valores repetidos (tipos, colores…) se muestran;
    los textos únicos (nombres…) se ocultan. Sirve para ajustar el lector de actas sin compartir nombres."""
    if _cuentas is None:
        _cuentas = {}

        def cuenta(n):
            if isinstance(n, dict):
                [cuenta(v) for v in n.values()]
            elif isinstance(n, list):
                [cuenta(v) for v in n]
            elif isinstance(n, str):
                _cuentas[n] = _cuentas.get(n, 0) + 1
        cuenta(nodo)
    pad = "  " * prof
    if prof > maxprof:
        return pad + "…\n"

    def val(v):
        if isinstance(v, str):
            ok = re.fullmatch(r"(?i)(si|no|s|n|t|f|true|false|titular(es)?|suplentes?|gol(es)?|tarjetas?|amarilla|roja|doble amarilla|local|visitante|normal|penalti|propia puerta|\\d{1,3})", v.strip())
            return repr(v) if ok else f"<texto de {len(v)} car.>"
        return repr(v)
    if isinstance(nodo, dict):
        s = ""
        for k, v in nodo.items():
            if isinstance(v, (dict, list)):
                s += f"{pad}{k}{f' [{len(v)}]' if isinstance(v, list) else ''}:\n" + esquema_anonimo(v, prof + 1, maxprof, _cuentas)
            else:
                s += f"{pad}{k}: {type(v).__name__} = {val(v)}\n"
        return s
    if isinstance(nodo, list) and nodo:
        if isinstance(nodo[0], dict):
            claves = []
            for x in nodo[:10]:
                claves += [k for k in x if k not in claves]
            ej = max(nodo[:10], key=len)
            return f"{pad}(elemento más completo de {len(nodo)}; claves vistas: {', '.join(claves)})\n" + esquema_anonimo(ej, prof, maxprof, _cuentas)
        return f"{pad}(valores: {', '.join(val(x) for x in nodo[:6])})\n"
    return ""


def _nd_de_acta(html):
    nd = extraer_next_data(html)
    return nd


def descargar_actas(partidos, previas, hoy=None, max_nuevas=MAX_ACTAS_POR_CICLO):
    """{codacta: acta}. Descarga las actas de los partidos jugados que aún no se tienen (y relee las de los
    últimos 3 días cada 6 h, por si se corrigen). Un fallo no detiene nada: se conserva lo anterior."""
    hoy = hoy or datetime.now().date()
    actas = dict(previas or {})
    nuevas = fallos = 0
    for p in partidos:
        cod, u = p.get("cod_acta"), p.get("acta")
        if not cod or not u or p.get("resultado_local") is None:
            continue
        reciente = False
        try:
            reciente = p.get("fecha") and (hoy - datetime.strptime(p["fecha"], "%Y-%m-%d").date()).days <= 3
        except ValueError:
            pass
        ya = cod in actas
        if ya and not (reciente and time.time() - _REVISADAS.get(cod, 0) > 6 * 3600):
            continue
        if not ya and nuevas >= max_nuevas:
            continue
        try:
            html = descargar(u, reintentos=2)
            _REVISADAS[cod] = time.time()
            nd = extraer_next_data(html)
            acta = acta_desde_next_data(nd, p, u) if nd else None
        except ErrorExtraccion as e:
            fallos += 1
            log(f"  Acta J{p['jornada']} {p['equipo_local']} - {p['equipo_visitante']}: no se pudo descargar ({str(e).splitlines()[0]})")
            continue
        if acta is None:
            fallos += 1
            log(f"  Acta J{p['jornada']} {p['equipo_local']} - {p['equipo_visitante']}: formato no reconocido")
            if nd and not (CARPETA / "diagnostico" / "acta_esquema.txt").exists():
                try:
                    (CARPETA / "diagnostico").mkdir(exist_ok=True)
                    pp = (nd.get("props") or {}).get("pageProps")
                    (CARPETA / "diagnostico" / "acta_esquema.txt").write_text(esquema_anonimo(pp), encoding="utf-8")
                    log("  (esquema sin nombres guardado en diagnostico/acta_esquema.txt)")
                except OSError:
                    pass
            continue
        if actas.get(cod) != acta:
            nuevas += 1
        actas[cod] = acta
        time.sleep(PAUSA_ENTRE_ACTAS_S)
    if nuevas or fallos:
        log(f"Actas: {nuevas} nuevas o actualizadas" + (f", {fallos} sin poder leer" if fallos else ""))
    return actas


# --------------------------------------------------------------------------
# Comparación y guardado
# --------------------------------------------------------------------------
CAMPOS_PARTIDO = ("fecha", "hora", "campo", "acta", "resultado_fuente")
CAMPOS_CLASIFICACION = ("equipo", "posicion", "puntos", "pj", "pg", "pe", "pp", "gf", "gc")


# --- Cifrado de las actas para publicarlas en internet (AES-256-GCM, clave derivada con PBKDF2) -------------
# Con la variable de entorno LIGA_CLAVE, datos_liga.json lleva "actas_cifradas" en lugar de "actas": quien no
# tenga el código solo ve texto ilegible. La web lo descifra en el propio iPhone con el código que se teclea una vez.
ITER_PBKDF2 = 200_000


def _clave_aes(clave, sal, iteraciones):
    return hashlib.pbkdf2_hmac("sha256", clave.encode("utf-8"), sal, iteraciones, dklen=32)


def cifrar_actas(actas, clave):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    sal, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_clave_aes(clave, sal, ITER_PBKDF2)).encrypt(nonce, json.dumps(actas, ensure_ascii=False).encode("utf-8"), None)
    b = lambda x: base64.b64encode(x).decode("ascii")
    return {"v": 1, "iter": ITER_PBKDF2, "sal": b(sal), "nonce": b(nonce), "datos": b(ct)}


def descifrar_actas(blob, clave):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        d = lambda k: base64.b64decode(blob[k])
        pt = AESGCM(_clave_aes(clave, d("sal"), int(blob["iter"]))).decrypt(d("nonce"), d("datos"), None)
        return json.loads(pt.decode("utf-8"))
    except Exception:
        return None


def cargar_previo(salida):
    try:
        datos = json.loads(Path(salida).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    clave = os.environ.get("LIGA_CLAVE")
    if clave and datos.get("actas_cifradas") and not datos.get("actas"):
        datos["actas"] = descifrar_actas(datos["actas_cifradas"], clave) or {}
    return datos


def _resultado(p):
    gl, gv = p.get("resultado_local"), p.get("resultado_visitante")
    return (gl, gv) if gl is not None and gv is not None else None


def _etiqueta(p):
    return f"J{p['jornada']}: {p['equipo_local']} - {p['equipo_visitante']}"


def _clave(p):
    return (p["jornada"], p["equipo_local"], p["equipo_visitante"])


def _filas(tabla):
    return sorted(json.dumps([f.get(c) for c in CAMPOS_CLASIFICACION]) for f in (tabla or []))


def mismos_resultados(a, b):
    ra = {_clave(p): _resultado(p) for p in a.get("partidos", [])}
    rb = {_clave(p): _resultado(p) for p in b.get("partidos", [])}
    return ra == rb


def comparar_datos(previo, nuevo):
    """Lista legible de cambios relevantes (resultados, horarios, campos, actas, clasificación).
    Devuelve [] si no hay ninguno. Ignora marcas de tiempo y metadatos."""
    if not previo.get("partidos"):
        return ["Primera carga de datos"]
    antes = {_clave(p): p for p in previo["partidos"]}
    ahora = {_clave(p): p for p in nuevo["partidos"]}
    cambios = []
    for k, n in ahora.items():
        o = antes.get(k)
        if o is None:
            cambios.append(f"{_etiqueta(n)}: partido nuevo")
            continue
        ro, rn = _resultado(o), _resultado(n)
        if ro != rn:
            if rn is None:
                cambios.append(f"{_etiqueta(n)}: resultado retirado ({ro[0]}-{ro[1]})")
            elif ro is None:
                cambios.append(f"{_etiqueta(n)}: resultado {rn[0]}-{rn[1]}")
            else:
                cambios.append(f"{_etiqueta(n)}: resultado corregido {ro[0]}-{ro[1]} → {rn[0]}-{rn[1]}")
        for campo in CAMPOS_PARTIDO:
            vo, vn = o.get(campo), n.get(campo)
            if vo == vn:
                continue
            if campo == "acta":
                cambios.append(f"{_etiqueta(n)}: acta {'disponible' if not vo else 'modificada'}")
            elif campo == "resultado_fuente":
                if vo == "acta" and not vn:
                    cambios.append(f"{_etiqueta(n)}: resultado confirmado en el calendario de la RFFM")
            else:
                cambios.append(f"{_etiqueta(n)}: {campo} {vo or '—'} → {vn or '—'}")
    for k, o in antes.items():
        if k not in ahora:
            cambios.append(f"{_etiqueta(o)}: partido eliminado")
    if _filas(previo.get("clasificacion")) != _filas(nuevo.get("clasificacion")):
        cambios.append("Clasificación actualizada")
    if (previo.get("escudos") or {}) != (nuevo.get("escudos") or {}):
        cambios.append("Escudos de los equipos actualizados")
    pa, na = previo.get("actas") or {}, nuevo.get("actas") or {}
    for cod, a in na.items():
        etq = f"J{a['jornada']}: {a['local']} - {a['visitante']}"
        if cod not in pa:
            cambios.append(f"{etq}: acta cargada")
        elif pa[cod] != a:
            cambios.append(f"{etq}: acta actualizada")
    return cambios


def construir_datos(resultado, previo, forzar=False, temporada="", categoria=""):
    """Convierte el resultado de extraer() en el documento a guardar (sin marca de tiempo)."""
    n_prev, n_new = len(previo.get("partidos", [])), len(resultado["partidos"])
    if n_prev and n_new < 0.8 * n_prev and not forzar:
        raise ErrorExtraccion(
            f"Se extrajeron {n_new} partidos y el archivo actual tiene {n_prev}. "
            "Parece una extracción incompleta; no se sobrescribe (usa --forzar para obligarlo)."
        )
    meta = resultado.get("_meta", {})
    datos = {
        "temporada": temporada or previo.get("temporada") or meta.get("temporada") or "",
        "categoria": categoria or previo.get("categoria") or meta.get("categoria") or "Liga",
        **{k: v for k, v in resultado.items() if not k.startswith("_")},
    }
    # Estabilidad: si esta vez no se pudo leer la clasificación oficial pero los resultados son los
    # mismos de antes, se conserva la oficial anterior (evita alternar entre oficial y calculada).
    if (datos["clasificacion_origen"] != "oficial" and previo.get("clasificacion_origen") == "oficial"
            and previo.get("clasificacion") and mismos_resultados(previo, datos)):
        datos["clasificacion"] = previo["clasificacion"]
        datos["clasificacion_origen"] = "oficial"
    return datos


def escribir_datos(datos, salida, cambios):
    """Escribe datos_liga.js y después datos_liga.json (de forma atómica). Devuelve el documento final."""
    salida = Path(salida)
    final = {
        "temporada": datos["temporada"],
        "categoria": datos["categoria"],
        "actualizado": ahora_iso(),
        "ultimos_cambios": cambios[:50],
        **{k: v for k, v in datos.items() if k not in ("temporada", "categoria")},
    }
    clave = os.environ.get("LIGA_CLAVE")
    if clave and not os.environ.get("LIGA_NO_ACTAS"):
        previo_raw = {}
        try:
            previo_raw = json.loads(salida.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        actas = final.pop("actas", None) or {}
        previas = descifrar_actas(previo_raw["actas_cifradas"], clave) if previo_raw.get("actas_cifradas") else None
        # Si las actas no han cambiado se conserva el cifrado anterior (así el archivo no cambia sin motivo).
        final["actas_cifradas"] = previo_raw["actas_cifradas"] if previas == actas and previas is not None else cifrar_actas(actas, clave)
    if salida.exists() and not os.environ.get("CI"):   # en GitHub Actions no se guarda copia (se versiona con git)
        (salida.parent / (salida.stem + ".anterior.json")).write_text(salida.read_text(encoding="utf-8"), encoding="utf-8")
    # El .js primero: cuando el .json cambia (lo que avisa a la web), el .js ya está al día.
    (salida.parent / (salida.stem + ".js")).write_text(
        "window.DATOS_LIGA = " + json.dumps(final, ensure_ascii=False) + ";\n", encoding="utf-8")
    fd, tmp = tempfile.mkstemp(dir=salida.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    os.replace(tmp, salida)
    return final


def comprobar_una_vez(url, salida=SALIDA_POR_DEFECTO, navegador=False, forzar=False, temporada="", categoria=""):
    """Descarga, compara con lo guardado y reescribe SOLO si hay cambios.
    Devuelve {"cambiado": bool, "cambios": [...], "datos": dict}. Lanza ErrorExtraccion si falla."""
    salida = Path(salida)
    previo = cargar_previo(salida)
    res = extraer(url, navegador)
    urls_escudos = res.pop("_escudos_urls", {})
    html_cal = res.pop("_html_cal", None)
    res["escudos"], res["escudos_fuente"] = descargar_escudos(
        urls_escudos, salida.parent / "escudos", previo, "{0.scheme}://{0.netloc}".format(urlparse(url)), res.get("fuente"), html_cal)
    try:
        completar_resultados_con_actas(res["partidos"], url, "{0.scheme}://{0.netloc}".format(urlparse(url)))
    except Exception as e:
        log(f"Aviso: no se pudieron comprobar los resultados por acta ({type(e).__name__}: {e})")
    try:
        if os.environ.get("LIGA_NO_ACTAS"):   # publicación en internet sin nombres de jugadoras
            res["actas"] = {}
        else:
            res["actas"] = descargar_actas(res["partidos"], previo.get("actas"))
    except Exception as e:  # las actas nunca deben impedir actualizar resultados
        log(f"Aviso: error con las actas ({type(e).__name__}: {e})")
        res["actas"] = previo.get("actas") or {}
    datos = construir_datos(res, previo, forzar, temporada, categoria)
    cambios = comparar_datos(previo, datos)
    if not cambios and (not salida.exists() or not (salida.parent / (salida.stem + ".js")).exists()):
        cambios = ["Archivos regenerados"]
    if cambios:
        return {"cambiado": True, "cambios": cambios, "datos": escribir_datos(datos, salida, cambios)}
    return {"cambiado": False, "cambios": [], "datos": previo}


def resumen_datos(datos):
    jug = sum(p["resultado_local"] is not None for p in datos["partidos"])
    jornadas = len({p["jornada"] for p in datos["partidos"]})
    return (f"{len(datos['partidos'])} partidos ({jug} jugados) en {jornadas} jornadas · "
            f"clasificación {datos['clasificacion_origen']} · vía {datos['origen_datos']}")


# --------------------------------------------------------------------------
# Vigilancia continua
# --------------------------------------------------------------------------
def es_fin_de_semana(ahora=None):
    return (ahora or datetime.now()).weekday() >= 5  # 5 = sábado, 6 = domingo


def intervalo_segundos(min_fin_semana, min_laborable, ahora=None):
    return max(1.0, (min_fin_semana if es_fin_de_semana(ahora) else min_laborable) * 60.0)


class Vigilante(threading.Thread):
    """Comprueba la RFFM en bucle. `al_estado(dict)` se llama tras cada comprobación."""

    def __init__(self, url, salida=SALIDA_POR_DEFECTO, navegador=False,
                 min_fin_semana=INTERVALO_FIN_SEMANA_MIN, min_laborable=INTERVALO_LABORABLE_MIN,
                 forzar=False, temporada="", categoria="", al_estado=None):
        super().__init__(daemon=True, name="vigilante-rffm")
        self.url, self.salida, self.navegador = url, Path(salida), navegador
        self.min_fin_semana, self.min_laborable = min_fin_semana, min_laborable
        self.forzar, self.temporada, self.categoria = forzar, temporada, categoria
        self.al_estado = al_estado
        self._parar, self._despertar = threading.Event(), threading.Event()
        self.estado = {
            "vigilante": True, "ok": None, "comprobaciones": 0, "fallos_seguidos": 0,
            "ultima_comprobacion": None, "ultima_comprobacion_ok": None,
            "ultimo_cambio": cargar_previo(self.salida).get("actualizado"),
            "proxima_comprobacion": None, "intervalo_s": None, "fin_de_semana": es_fin_de_semana(),
            "error": None,
        }

    def parar(self):
        self._parar.set()
        self._despertar.set()

    def comprobar_ahora(self):
        self._despertar.set()

    def _emitir(self):
        if self.al_estado:
            try:
                self.al_estado(dict(self.estado))
            except Exception as e:  # un fallo del receptor no debe parar la vigilancia
                log(f"Aviso: error notificando el estado ({e})")

    def _ciclo(self):
        st = self.estado
        st["comprobaciones"] += 1
        st["ultima_comprobacion"] = ahora_iso()
        try:
            r = comprobar_una_vez(self.url, self.salida, self.navegador, self.forzar, self.temporada, self.categoria)
        except ErrorExtraccion as e:
            self._fallo(str(e).splitlines()[0])
        except Exception as e:  # error inesperado (p. ej. un cambio en la web): se registra y se sigue
            log(f"Error inesperado: {type(e).__name__}: {e}")
            self._fallo(f"{type(e).__name__}: {e}")
        else:
            st.update(ok=True, error=None, fallos_seguidos=0, ultima_comprobacion_ok=st["ultima_comprobacion"])
            if r["cambiado"]:
                st["ultimo_cambio"] = r["datos"]["actualizado"]
                log(f"Datos actualizados ({len(r['cambios'])} cambios):")
                for c in r["cambios"][:12]:
                    log("   •", c)
                if len(r["cambios"]) > 12:
                    log(f"   … y {len(r['cambios']) - 12} más")
            else:
                log("Sin cambios.")

    def _fallo(self, mensaje):
        st = self.estado
        st.update(ok=False, error=mensaje, fallos_seguidos=st["fallos_seguidos"] + 1)
        log(f"No se pudo actualizar ({mensaje}). Se conservan los últimos datos.")

    def run(self):
        log(f"Vigilando {self.url}")
        while not self._parar.is_set():
            self._ciclo()
            st = self.estado
            intervalo = intervalo_segundos(self.min_fin_semana, self.min_laborable)
            # tras un fallo se reintenta antes (30 s, 60 s, 120 s…) sin superar el intervalo normal
            espera = min(intervalo, 30.0 * 2 ** (st["fallos_seguidos"] - 1)) if st["fallos_seguidos"] else intervalo
            st["intervalo_s"] = espera
            st["fin_de_semana"] = es_fin_de_semana()
            st["proxima_comprobacion"] = (datetime.now().astimezone() + timedelta(seconds=espera)).isoformat(timespec="seconds")
            self._emitir()
            log(f"Próxima comprobación en {espera / 60:.1f} min ({'fin de semana' if st['fin_de_semana'] else 'entre semana'}).")
            self._despertar.wait(espera)
            self._despertar.clear()


# --------------------------------------------------------------------------
# Diagnóstico y línea de comandos
# --------------------------------------------------------------------------
def argumentos_comunes(ap):
    """Opciones compartidas por extractor.py y servidor.py."""
    ap.add_argument("url", nargs="?", default=URL_POR_DEFECTO, help="URL de la competición en rffm.es")
    ap.add_argument("--salida", type=Path, default=SALIDA_POR_DEFECTO, help="ruta de datos_liga.json")
    ap.add_argument("--navegador", action="store_true", help="usar un navegador real (Playwright)")
    ap.add_argument("--intervalo-finde", type=float, default=INTERVALO_FIN_SEMANA_MIN, metavar="MIN",
                    help="minutos entre comprobaciones en sábado y domingo (por defecto %(default)s)")
    ap.add_argument("--intervalo-semana", type=float, default=INTERVALO_LABORABLE_MIN, metavar="MIN",
                    help="minutos entre comprobaciones de lunes a viernes (por defecto %(default)s)")
    ap.add_argument("--forzar", action="store_true", help="guardar aunque haya menos partidos que antes")
    ap.add_argument("--temporada", default="")
    ap.add_argument("--categoria", default="")


def informe_acta(url, navegador=False):
    """Descarga UN acta y guarda su estructura con los textos personales ocultados (para ajustar el lector)."""
    cal = construir_urls(url)[0]
    origen = "{0.scheme}://{0.netloc}".format(urlparse(url))
    html = descargar_con_navegador(cal) if navegador else descargar(cal)
    partidos, nd, via = parsear_calendario(html, origen)
    asignar_actas(partidos, url, origen)
    jugados = [p for p in partidos if p.get("acta") and p.get("resultado_local") is not None]
    print(f"Partidos leídos: {len(partidos)} · con acta: {len(jugados)}")
    if not jugados:
        print("Ningún partido jugado tiene código de acta (¿la jornada aún no se ha disputado?).")
        return 1
    p = jugados[-1]
    print(f"Acta de prueba: J{p['jornada']} {p['equipo_local']} - {p['equipo_visitante']}\n  {p['acta']}")
    h = descargar_con_navegador(p["acta"]) if navegador else descargar(p["acta"])
    nd_a = extraer_next_data(h)
    if not nd_a:
        print("La página del acta no contiene datos legibles (__NEXT_DATA__). Prueba con --navegador.")
        return 1
    pp = (nd_a.get("props") or {}).get("pageProps")
    texto = esquema_anonimo(pp)
    acta = acta_desde_next_data(nd_a, p, p["acta"])
    resumen = "Lector de actas: formato NO reconocido" if not acta else (
        f"Lector de actas: {len(acta['jugadoras_local'])} jugadoras locales, {len(acta['jugadoras_visitante'])} visitantes, "
        f"{len(acta['goles'])} goles, {len(acta['tarjetas'])} tarjetas "
        f"(titulares: {sum(j['titular'] is True for j in acta['jugadoras_local'] + acta['jugadoras_visitante'])})")
    CARPETA_D = CARPETA / "diagnostico"
    CARPETA_D.mkdir(exist_ok=True)
    (CARPETA_D / "acta_esquema.txt").write_text(resumen + "\n\n" + texto, encoding="utf-8")
    print("\n" + resumen + "\n\n--- estructura (sin nombres) ---\n" + texto)
    print(f"Guardado en: {CARPETA_D / 'acta_esquema.txt'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Extrae calendario y clasificación de la RFFM a datos_liga.json")
    argumentos_comunes(ap)
    ap.add_argument("--vigilar", action="store_true", help="comprobar en bucle (Ctrl+C para parar)")
    ap.add_argument("--diagnostico", action="store_true", help="guardar páginas y esquema JSON en ./diagnostico")
    ap.add_argument("--escudos", action="store_true", help="informe de cómo publica la RFFM los escudos (se guarda en diagnostico/escudos.txt)")
    ap.add_argument("--acta", action="store_true", help="informe SIN NOMBRES de cómo publica la RFFM las actas (diagnostico/acta_esquema.txt)")
    args = ap.parse_args()

    try:
        if args.acta:
            return informe_acta(args.url, args.navegador)
        if args.diagnostico:
            return diagnostico(args.url, args.navegador)
        if args.escudos:
            html = descargar_con_navegador(construir_urls(args.url)[0]) if args.navegador else descargar(construir_urls(args.url)[0])
            origen = "{0.scheme}://{0.netloc}".format(urlparse(args.url))
            partidos, nd, via = parsear_calendario(html, origen)
            urls = recopilar_escudos(partidos, html, origen)
            nombres = {p[k] for p in partidos for k in ("equipo_local", "equipo_visitante")}
            ruta, texto = guardar_informe_escudos(html, origen, nombres, nd)
            print(texto)
            print(f"Partidos leídos: {len(partidos)} (vía {via}) · escudos localizados: {len(urls)} de {len(nombres)} equipos")
            for n, u in sorted(urls.items())[:20]:
                print(f"   {n}: {u[:110]}")
            print(f"\nInforme guardado en: {ruta}")
            if urls:
                n0, u0 = sorted(urls.items())[0]
                print(f"\nProbando a descargar el escudo de {n0} …")
                baj = Bajador(origen, construir_urls(args.url)[0], html, None, verboso=True)
                try:
                    d = baj.bajar(u0)
                    print(f"✔ Descargado ({len(d)} bytes, {_tipo_imagen(d)}). Base: {baj.base or u0}")
                except ValueError as e:
                    print(f"✘ {e}")
                ctx = contexto_js(baj.textos_js, r"escudo|pimg|Clubes")
                print("\n--- código de la página que menciona escudos ---")
                print("\n".join(ctx) or "(nada)")
                with open(ruta, "a", encoding="utf-8") as f:
                    f.write("\n--- código de la página que menciona escudos ---\n" + "\n".join(ctx) + "\n")
            return 0
        if args.vigilar:
            v = Vigilante(args.url, args.salida, args.navegador, args.intervalo_finde, args.intervalo_semana,
                          args.forzar, args.temporada, args.categoria)
            v.start()
            try:
                while v.is_alive():
                    v.join(1)
            except KeyboardInterrupt:
                print("\nVigilancia detenida.")
                v.parar()
            return 0
        r = comprobar_una_vez(args.url, args.salida, args.navegador, args.forzar, args.temporada, args.categoria)
    except ErrorExtraccion as e:
        print(f"\nERROR: {e}\nEl archivo de datos anterior se ha conservado.", file=sys.stderr)
        return 2

    if r["cambiado"]:
        print("Datos actualizados:")
        for c in r["cambios"][:20]:
            print("  •", c)
    else:
        print("Sin cambios respecto a los datos guardados.")
    print(f"OK: {resumen_datos(r['datos'])}\nArchivo: {args.salida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
