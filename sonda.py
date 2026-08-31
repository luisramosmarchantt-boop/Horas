"""Sonda temporal: mira que devuelve de verdad calendarioServicio."""
import http.cookiejar, json, re, sys, time, urllib.request
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
print("=== OFICINAS (%d) ===" % len(ofs))
for i, n in ofs:
    print("   ", i, n.strip())

idof = next(i for i, n in ofs if re.search("PROVIDENCIA", n, re.I))
print("\n=== usando oficina", idof)

servs = json.loads(traer("%s/cita/comboServicios?idOficina=%s&codServicio=&idioma=" % (BASE, idof)))
todos = servs.get("servicios") or []
print("=== SERVICIOS DE LA OFICINA (%d) ===" % len(todos))
for s in todos:
    print("   ", s.get("idServicio"), "|", s.get("auxServicio"))

emp = [(s["idServicio"], s["auxServicio"]) for s in todos
       if re.search("empalme", s.get("auxServicio", ""), re.I)]

# 1) forma cruda de una respuesta
idserv, et = emp[0]
for f in ["2026-08-31", "2026-09-01", "2026-09-15", "2026-10-01"]:
    crudo = traer("%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                  "&grupoMaestroRaiz=1&idOficina=%s" % (BASE, idserv, f, idof))
    d = json.loads(crudo)
    dias = (d.get("calendario") or {}).get("dias") or []
    print("\n--- ancla %s | %s | %d dias" % (f, et[:40], len(dias)))
    print("    claves calendario:", list((d.get("calendario") or {}).keys()))
    if dias:
        print("    rango:", dias[0].get("fecha"), "->", dias[-1].get("fecha"))
        for x in dias:
            fr = x.get("franjas")
            libres = sum((y.get("huecosLibres") or 0) for y in (fr or []))
            print("      %s estado=%s franjas=%s huecosLibres=%s"
                  % (x.get("fecha"), x.get("estado"),
                     "null" if fr is None else len(fr), libres))
    if f == "2026-08-31":
        print("    CRUDO (1200):", crudo[:1200])

# 2) barrido semanal de TODOS los servicios de la oficina, sept y oct
print("\n=== BARRIDO SEMANAL (todos los servicios) ===")
hoy = date(2026, 8, 31)
anclas = [hoy + timedelta(days=7 * k) for k in range(10)]
for s in todos:
    idserv, et = s["idServicio"], s.get("auxServicio", "")
    encontrados = []
    for a in anclas:
        try:
            d = json.loads(traer("%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                                 "&grupoMaestroRaiz=1&idOficina=%s"
                                 % (BASE, idserv, a.isoformat(), idof)))
        except Exception as e:
            print("   !! %s %s: %s" % (et[:30], a, e)); continue
        for x in ((d.get("calendario") or {}).get("dias") or []):
            if x.get("estado") == 0:
                fr = x.get("franjas") or []
                horas = sorted({y["horaInicio"][:5] for y in fr
                                if (y.get("huecosLibres") or 0) > 0 and y.get("horaInicio")})
                encontrados.append((x.get("fecha"), horas))
        time.sleep(0.15)
    if encontrados:
        vistos = sorted(set(f for f, _ in encontrados))
        print("  *** %s (%s): %s" % (et, idserv, vistos[:15]))
        for f, h in encontrados[:6]:
            if h: print("        %s -> %s" % (f, h))
