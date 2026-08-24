"""Monitor de horas disponibles para citas de Enel.

Consulta la API interna del agendamiento online de Enel y avisa por Telegram
cuando aparecen horas libres en una oficina para los tramites que interesan.

La version anterior de este monitor manejaba un navegador y trataba de adivinar
los dias disponibles comparando el color de fondo de las celdas del calendario.
Eso nunca funciono: el color vive en el <span> interior, no en el <td>, asi que
el detector daba siempre cero. Ahora se usan los mismos endpoints que usa la
pagina, que devuelven JSON con los huecos libres de cada franja horaria:

  GET /citaprevia/cita/comboServicios?idOficina=24
      -> {"servicios": [{"idServicio": 11441, "auxServicio": "Empalmes ..."}]}

  GET /citaprevia/cita/calendarioServicio?idServicio=11441&fecha=2026-09-02
                                         &grupoMaestroRaiz=1&idOficina=24
      -> {"calendario": {"dias": [{"fecha": "2026-09-03", "estado": 0, "franjas": [
             {"horaInicio": "13:40:00", "huecosLibres": 2, ...}]}]}}

Sobre ese endpoint hay dos cosas que conviene tener claras:

  - 'dias' trae los dias con horario publicado del mes al que apunta 'fecha',
    y 'estado' vale 0 cuando el dia tiene cupos y 1 cuando esta lleno. Es el
    mismo criterio con que la pagina pinta el dia como seleccionable.

  - 'franjas' solo viene rellena para la semana que contiene 'fecha'; para el
    resto llega vacia o nula. Por eso hay que anclar en el dia que interesa
    para poder leer sus horas, en vez de recorrer el calendario a saltos.
"""

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = os.environ.get("BASE", "https://servicequendalat.enel.com/citaprevia")
PORTADA = BASE + "/?pais=cl"
OFICINA = os.environ.get("OFICINA", r"PROVIDENCIA")
PATRON = os.environ.get("PATRON", r"empalme")
MESES = int(os.environ.get("MESES", "4"))
ESTADO = os.environ.get("ESTADO", "estado.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# cada cuanto repetir un aviso si la disponibilidad no cambio
REPETIR_AVISO = 2 * 3600
# cada cuanto avisar de una falla del monitor
REPETIR_ERROR = 6 * 3600
# cada cuanto mandar el "sigo vivo"
LATIDO = 24 * 3600

CHILE = timezone(timedelta(hours=-4))
DIAS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]

NAVEGADOR = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class SitioCambio(Exception):
    """El sitio no respondio como esperabamos."""


def ahora():
    return datetime.now(CHILE).strftime("%d-%m-%Y %H:%M")


def avisar(texto):
    print("[aviso]", texto.replace("\n", " | "))
    if not (TG_TOKEN and TG_CHAT_ID):
        print("!! faltan TG_TOKEN / TG_CHAT_ID, no se envio nada", file=sys.stderr)
        return False
    datos = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": texto}).encode()
    for intento in range(3):
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=datos
                ),
                timeout=20,
            ).read()
            return True
        except Exception as e:
            print("fallo telegram (intento %d): %s" % (intento + 1, e), file=sys.stderr)
            time.sleep(3)
    return False


def cargar_estado():
    try:
        with open(ESTADO) as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_estado(est):
    try:
        with open(ESTADO, "w") as f:
            json.dump(est, f)
    except Exception as e:
        print("no pude guardar el estado:", e, file=sys.stderr)


def abridor():
    """Un opener con galletas: la API necesita la sesion que entrega la portada."""
    tarro = http.cookiejar.CookieJar()
    ab = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(tarro))
    ab.addheaders = [("User-Agent", NAVEGADOR), ("Accept-Language", "es-CL,es;q=0.9")]
    return ab


def traer(ab, url, reintentos=3):
    ultimo = None
    for intento in range(reintentos):
        try:
            with ab.open(url, timeout=30) as r:
                if r.status != 200:
                    raise SitioCambio("%s respondio %s" % (url, r.status))
                return r.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            ultimo = e
            time.sleep(2 * (intento + 1))
    raise SitioCambio("no pude leer %s: %s" % (url, ultimo))


def id_oficina(ab):
    """Saca el id de la oficina del <select> de la portada."""
    html = traer(ab, PORTADA)
    opciones = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)</option>', html)
    if not opciones:
        raise SitioCambio("la portada no trae el listado de oficinas")
    for idof, nombre in opciones:
        if re.search(OFICINA, nombre, re.I):
            return idof, nombre.strip()
    raise SitioCambio("no encontre la oficina %r; hay %d opciones"
                      % (OFICINA, len(opciones)))


def servicios(ab, idof):
    """Tramites de la oficina que calzan con el patron."""
    crudo = traer(ab, "%s/cita/comboServicios?idOficina=%s&codServicio=&idioma=" % (BASE, idof))
    try:
        datos = json.loads(crudo)
    except ValueError:
        raise SitioCambio("comboServicios ya no devuelve JSON")
    todos = datos.get("servicios")
    if not todos:
        raise SitioCambio("comboServicios no trae servicios para la oficina %s" % idof)
    elegidos = [(s["idServicio"], s["auxServicio"]) for s in todos
                if re.search(PATRON, s.get("auxServicio", ""), re.I)]
    if not elegidos:
        raise SitioCambio("ningun tramite calza con %r (hay %d en la oficina)"
                          % (PATRON, len(todos)))
    return elegidos


def calendario(ab, idserv, idof, fecha):
    """Dias publicados alrededor de 'fecha' (con franjas solo para esa semana)."""
    crudo = traer(ab, "%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                      "&grupoMaestroRaiz=1&idOficina=%s" % (BASE, idserv, fecha, idof))
    try:
        datos = json.loads(crudo)
    except ValueError:
        raise SitioCambio("calendarioServicio ya no devuelve JSON")
    return (datos.get("calendario") or {}).get("dias") or []


def horas_libres(dia):
    """Horas con cupo de un dia, si es que trae las franjas rellenas."""
    return sorted({f["horaInicio"][:5] for f in (dia.get("franjas") or [])
                   if (f.get("huecosLibres") or 0) > 0 and f.get("horaInicio")})


def tiene_cupo(dia):
    """El sitio marca el dia con estado 0; las franjas confirman cuando vienen."""
    return dia.get("estado") == 0 or bool(horas_libres(dia))


def anclas(hoy, meses):
    """Hoy, y despues el primer dia de cada mes siguiente."""
    fechas = [hoy]
    a, m = hoy.year, hoy.month
    for _ in range(meses - 1):
        m += 1
        if m > 12:
            a, m = a + 1, 1
        fechas.append(date(a, m, 1))
    return fechas


def etiqueta_dia(iso):
    try:
        d = date.fromisoformat(iso)
        return "%s %02d-%02d" % (DIAS[d.weekday()], d.day, d.month)
    except Exception:
        return iso


def revisar():
    """Devuelve (hallazgos, firma, dias_vistos). Lanza SitioCambio si algo no cuadra."""
    ab = abridor()
    idof, nombre = id_oficina(ab)
    print("oficina:", nombre, "(id %s)" % idof)

    tramites = servicios(ab, idof)
    print("tramites:", [t for _, t in tramites])

    hallazgos = []
    marcas = []
    vistos = 0
    fechas = anclas(datetime.now(CHILE).date(), MESES)

    for idserv, etiqueta in tramites:
        libres = {}
        for ancla in fechas:
            for dia in calendario(ab, idserv, idof, ancla.isoformat()):
                vistos += 1
                if not tiene_cupo(dia):
                    continue
                fecha = dia.get("fecha")
                if not fecha or fecha in libres:
                    continue
                # las franjas solo vienen para la semana apuntada: pedimos ese dia
                horas = horas_libres(dia)
                if not horas:
                    for d in calendario(ab, idserv, idof, fecha):
                        if d.get("fecha") == fecha:
                            horas = horas_libres(d)
                            break
                libres[fecha] = horas

        if libres:
            print("%s -> %d dias con cupo" % (etiqueta, len(libres)))
            lineas = []
            for fecha, horas in sorted(libres.items()):
                if horas:
                    extra = " (+%d mas)" % (len(horas) - 4) if len(horas) > 4 else ""
                    lineas.append("  %s: %s%s"
                                  % (etiqueta_dia(fecha), ", ".join(horas[:4]), extra))
                else:
                    lineas.append("  %s: dia habilitado" % etiqueta_dia(fecha))
                print("    %s: %s" % (fecha, ", ".join(horas) or "sin detalle de horas"))
            hallazgos.append(etiqueta + "\n" + "\n".join(lineas))
            # la firma va por dia, no por hora: si alguien toma una hora suelta
            # del mismo dia no tiene sentido volver a avisar de ese dia
            marcas.extend("%s::%s" % (etiqueta, f) for f in sorted(libres))
        else:
            print("%s -> sin horas" % etiqueta)

    # si no vimos ni un dia, el formato cambio: no podemos afirmar que no hay horas
    if vistos == 0:
        raise SitioCambio("el calendario no devolvio ningun dia en %d meses" % MESES)

    return hallazgos, "|".join(sorted(marcas)), vistos


def main():
    est = cargar_estado()
    t = time.time()

    try:
        hallazgos, firma, con_datos = revisar()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        if t - est.get("ts_error", 0) > REPETIR_ERROR:
            avisar("PROBLEMA CON EL MONITOR DE ENEL\n(%s)\n\n%s\n\n"
                   "Mientras no se arregle, no puede avisarte de horas nuevas."
                   % (ahora(), e))
            est["ts_error"] = t
        guardar_estado(est)
        raise

    est["ts_error"] = 0

    if hallazgos:
        nuevo = firma != est.get("firma")
        vencido = t - est.get("ts_aviso", 0) > REPETIR_AVISO
        if nuevo or vencido:
            avisar("HAY HORAS - ENEL %s\n(%s)\n\n%s\n\n%s"
                   % (OFICINA, ahora(), "\n\n".join(hallazgos), PORTADA))
            est["firma"] = firma
            est["ts_aviso"] = t
            est["ts_latido"] = t
        else:
            print("las mismas horas ya avisadas, no repito")
    else:
        print("%s - sin horas (%d dias revisados)" % (ahora(), con_datos))
        est["firma"] = ""
        if t - est.get("ts_latido", 0) > LATIDO:
            avisar("Monitor de Enel funcionando (%s). Sigo revisando %s, por ahora sin horas."
                   % (ahora(), OFICINA))
            est["ts_latido"] = t

    guardar_estado(est)


if __name__ == "__main__":
    main()
