"""Sonda: busca dias con cupo en TODAS las oficinas y trámites."""
import http.cookiejar, json, re, time, urllib.request
from datetime import date, timedelta

BASE = "https://servicequendalat.enel.com/citaprevia"
PORTADA = BASE + "/?pais=cl"
NAV = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

tarro = http.cookiejar.CookieJar()
ab = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(tarro))
ab.addheaders = [("User-Agent", NAV), ("Accept-Language", "es-CL,es;q=0.9")]

def traer(u):
    with ab.open(u, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

html = traer(PORTADA)
ofs = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)</option>', html)

hoy = date.today()
anclas = [hoy, hoy + timedelta(days=7), hoy + timedelta(days=21),
          hoy + timedelta(days=45), hoy + timedelta(days=75)]

print("=== BARRIDO COMPLETO: %d oficinas ===" % len(ofs))
total_libres = 0
for idof, nombre in ofs:
    try:
        servs = json.loads(traer("%s/cita/comboServicios?idOficina=%s&codServicio=&idioma="
                                 % (BASE, idof))).get("servicios") or []
    except Exception as e:
        print("\n## %s (%s): fallo comboServicios: %s" % (nombre.strip(), idof, e))
        continue
    print("\n## %s (id %s) - %d servicios" % (nombre.strip(), idof, len(servs)))
    for s in servs:
        idserv, et = s.get("idServicio"), (s.get("auxServicio") or "").strip()
        dias = {}
        for a in anclas:
            try:
                d = json.loads(traer("%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                                     "&grupoMaestroRaiz=1&idOficina=%s"
                                     % (BASE, idserv, a.isoformat(), idof)))
            except Exception:
                continue
            for x in ((d.get("calendario") or {}).get("dias") or []):
                f = x.get("fecha")
                if f and f not in dias:
                    dias[f] = x
            time.sleep(0.1)
        if not dias:
            print("   - %-52s sin calendario publicado" % et[:52])
            continue
        libres = {f: x for f, x in dias.items() if x.get("estado") == 0}
        rango = "%s..%s" % (min(dias), max(dias))
        if libres:
            total_libres += len(libres)
            print("   *** %-48s %s | CON CUPO: %s" % (et[:48], rango, sorted(libres)))
            for f in sorted(libres):
                d2 = json.loads(traer("%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                                      "&grupoMaestroRaiz=1&idOficina=%s"
                                      % (BASE, idserv, f, idof)))
                for x in ((d2.get("calendario") or {}).get("dias") or []):
                    if x.get("fecha") == f:
                        hs = sorted({y["horaInicio"][:5] for y in (x.get("franjas") or [])
                                     if (y.get("huecosLibres") or 0) > 0 and y.get("horaInicio")})
                        print("           %s -> %s" % (f, hs or "estado 0 pero sin franjas"))
        else:
            print("   - %-52s %s | %d dias, todos llenos" % (et[:52], rango, len(dias)))

print("\n=== TOTAL DIAS CON CUPO EN TODO EL PAIS: %d ===" % total_libres)
