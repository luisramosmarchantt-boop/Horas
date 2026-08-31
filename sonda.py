"""Barrido de todas las oficinas con VARIAS anclas (una sola ancla no sirve:
el endpoint devuelve vacio si la fecha cae fuera de la ventana publicada)."""
import http.cookiejar, json, re, sys, urllib.request
from datetime import date, timedelta

BASE = "https://servicequendalat.enel.com/citaprevia"
NAV = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
tarro = http.cookiejar.CookieJar()
ab = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(tarro))
ab.addheaders = [("User-Agent", NAV), ("Accept-Language", "es-CL,es;q=0.9")]

def traer(u, t=12):
    with ab.open(u, timeout=t) as r:
        return r.read().decode("utf-8", "replace")

def linea(*a):
    print(*a); sys.stdout.flush()

def cal(idserv, idof, f):
    try:
        r = json.loads(traer("%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                             "&grupoMaestroRaiz=1&idOficina=%s" % (BASE, idserv, f, idof)))
    except Exception:
        return []
    return (r.get("calendario") or {}).get("dias") or []

html = traer(BASE + "/?pais=cl")
ofs = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)</option>', html)

hoy = date.today()
semillas = [hoy.isoformat(), "2026-09-01", "2026-09-15",
            "2026-10-01", "2026-10-15", "2026-11-01"]

total = 0
for idof, nombre in ofs:
    nombre = nombre.strip()
    try:
        servs = json.loads(traer("%s/cita/comboServicios?idOficina=%s&codServicio=&idioma="
                                 % (BASE, idof))).get("servicios") or []
    except Exception as e:
        linea("## %s: fallo %s" % (nombre, e)); continue
    if not servs:
        linea("\n## %s (id %s): sin servicios" % (nombre, idof)); continue
    linea("\n## %s (id %s)" % (nombre, idof))
    for s in servs:
        idserv = s.get("idServicio"); et = (s.get("auxServicio") or "").strip()
        dias = {}
        for f in semillas:
            for x in cal(idserv, idof, f):
                if x.get("fecha"):
                    dias[x["fecha"]] = x
        if not dias:
            linea("   -   %-46s sin ventana publicada" % et[:46]); continue
        # leer franjas reales anclando en cada dia
        concupo = []
        for f in sorted(dias):
            for x in cal(idserv, idof, f):
                if x.get("fecha") == f:
                    hs = sorted({y["horaInicio"][:5] for y in (x.get("franjas") or [])
                                 if (y.get("huecosLibres") or 0) > 0 and y.get("horaInicio")})
                    if hs:
                        concupo.append((f, hs))
                    break
        if concupo:
            total += len(concupo)
            for f, hs in concupo:
                linea("   *** %-46s %s -> %s" % (et[:46], f, hs))
        else:
            linea("   -   %-46s %s..%s sin cupo"
                  % (et[:46], min(dias), max(dias)))

linea("\n=== DIAS CON CUPO REAL EN TODO EL PAIS: %d ===" % total)
