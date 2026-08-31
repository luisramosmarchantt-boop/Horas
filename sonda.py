"""Sonda acotada: una sola ancla por servicio, con salida inmediata."""
import http.cookiejar, json, re, sys, urllib.request
from datetime import date

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

html = traer(BASE + "/?pais=cl")
ofs = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)</option>', html)
linea("oficinas:", len(ofs))

# el endpoint ignora la fecha y devuelve la ventana publicada: basta una ancla
ancla = date.today().isoformat()
total = 0
for idof, nombre in ofs:
    nombre = nombre.strip()
    try:
        servs = json.loads(traer("%s/cita/comboServicios?idOficina=%s&codServicio=&idioma="
                                 % (BASE, idof))).get("servicios") or []
    except Exception as e:
        linea("## %-28s fallo: %s" % (nombre, e)); continue
    linea("\n## %s (id %s) - %d servicios" % (nombre, idof, len(servs)))
    for s in servs:
        idserv = s.get("idServicio")
        et = (s.get("auxServicio") or "").strip()
        try:
            d = json.loads(traer("%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                                 "&grupoMaestroRaiz=1&idOficina=%s"
                                 % (BASE, idserv, ancla, idof)))
        except Exception as e:
            linea("   ? %-46s error: %s" % (et[:46], e)); continue
        dias = (d.get("calendario") or {}).get("dias") or []
        if not dias:
            linea("   - %-46s sin calendario" % et[:46]); continue
        libres = [x for x in dias if x.get("estado") == 0]
        if libres:
            total += len(libres)
            for x in libres:
                hs = sorted({y["horaInicio"][:5] for y in (x.get("franjas") or [])
                             if (y.get("huecosLibres") or 0) > 0 and y.get("horaInicio")})
                linea("   *** %-42s %s -> %s" % (et[:42], x.get("fecha"), hs or "(sin franjas)"))
        else:
            linea("   - %-46s %s..%s todos llenos"
                  % (et[:46], dias[0].get("fecha"), dias[-1].get("fecha")))

linea("\n=== DIAS CON CUPO EN TODAS LAS OFICINAS: %d ===" % total)
