"""Sonda: anclar en CADA dia de septiembre y leer franjas reales."""
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

traer(BASE + "/?pais=cl")
IDOF = "24"  # Providencia
servs = json.loads(traer("%s/cita/comboServicios?idOficina=%s&codServicio=&idioma="
                         % (BASE, IDOF))).get("servicios") or []
emp = [(s["idServicio"], s["auxServicio"]) for s in servs
       if re.search("empalme", s.get("auxServicio", ""), re.I)]

d = date(2026, 9, 1)
fin = date(2026, 10, 10)
fechas = []
while d <= fin:
    if d.weekday() < 5:
        fechas.append(d)
    d += timedelta(days=1)

for idserv, et in emp:
    linea("\n===== %s (%s) =====" % (et, idserv))
    for f in fechas:
        try:
            r = json.loads(traer("%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                                 "&grupoMaestroRaiz=1&idOficina=%s"
                                 % (BASE, idserv, f.isoformat(), IDOF)))
        except Exception as e:
            linea("  %s error %s" % (f, e)); continue
        dias = (r.get("calendario") or {}).get("dias") or []
        mio = [x for x in dias if x.get("fecha") == f.isoformat()]
        if not dias:
            continue
        if not mio:
            linea("  ancla %s -> devuelve %s..%s (no incluye el dia)"
                  % (f, dias[0].get("fecha"), dias[-1].get("fecha")))
            continue
        x = mio[0]
        fr = x.get("franjas")
        libres = sorted({y["horaInicio"][:5] for y in (fr or [])
                         if (y.get("huecosLibres") or 0) > 0 and y.get("horaInicio")})
        marca = "  *** CUPO" if libres else "  "
        linea("%s ancla %s estado=%s franjas=%s -> %s"
              % (marca, f, x.get("estado"),
                 "null" if fr is None else len(fr), libres or "sin cupo"))
