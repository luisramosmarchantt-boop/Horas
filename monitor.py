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
      -> {"calendario": {"dias": [{"fecha": "2026-09-01", "franjas": [
             {"horaInicio": "09:40:00", "huecosLibres": 1, ...}]}]}}

Cada llamada devuelve la semana que contiene 'fecha', asi que se avanza de
siete en siete dias para cubrir las proximas semanas.
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
SEMANAS = int(os.environ.get("SEMANAS", "13"))
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


def semana(ab, idserv, idof, fecha):
    """Devuelve los dias de la semana que contiene 'fecha'."""
    crudo = traer(ab, "%s/cita/calendarioServicio?idServicio=%s&fecha=%s"
                      "&grupoMaestroRaiz=1&idOficina=%s" % (BASE, idserv, fecha, idof))
    try:
        datos = json.loads(crudo)
    except ValueError:
        raise SitioCambio("calendarioServicio ya no devuelve JSON")
    return (datos.get("calendario") or {}).get("dias") or []


def etiqueta_dia(iso):
    try:
        d = date.fromisoformat(iso)
        return "%s %02d-%02d" % (DIAS[d.weekday()], d.day, d.month)
    except Exception:
        return iso


def revisar():
    """Devuelve (hallazgos, semanas_con_datos). Lanza SitioCambio si algo no cuadra."""
    ab = abridor()
    idof, nombre = id_oficina(ab)
    print("oficina:", nombre, "(id %s)" % idof)

    tramites = servicios(ab, idof)
    print("tramites:", [t for _, t in tramites])

    hallazgos = []
    con_datos = 0
    hoy = datetime.now(CHILE).date()

    for idserv, etiqueta in tramites:
        libres_tramite = []
        for i in range(SEMANAS):
            dias = semana(ab, idserv, idof, (hoy + timedelta(days=7 * i)).isoformat())
            if dias:
                con_datos += 1
            for d in dias:
                # 'franjas' puede venir como null, no solo ausente
                horas = sorted({f["horaInicio"][:5] for f in (d.get("franjas") or [])
                                if (f.get("huecosLibres") or 0) > 0 and f.get("horaInicio")})
                if horas:
                    libres_tramite.append((d.get("fecha", "?"), horas))
        if libres_tramite:
            print("%s -> %d dias con horas" % (etiqueta, len(libres_tramite)))
            lineas = []
            for fecha, horas in sorted(libres_tramite):
                extra = " (+%d mas)" % (len(horas) - 4) if len(horas) > 4 else ""
                lineas.append("  %s: %s%s"
                              % (etiqueta_dia(fecha), ", ".join(horas[:4]), extra))
                print("    %s: %s" % (fecha, ", ".join(horas)))
            hallazgos.append(etiqueta + "\n" + "\n".join(lineas))
        else:
            print("%s -> sin horas" % etiqueta)

    # si ninguna semana trajo dias, el formato cambio: no podemos afirmar que no hay horas
    if con_datos == 0:
        raise SitioCambio("ninguna de las %d semanas consultadas trajo dias" % SEMANAS)

    return hallazgos, con_datos


def main():
    est = cargar_estado()
    t = time.time()

    try:
        hallazgos, con_datos = revisar()
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
        firma = "|".join(sorted(hallazgos))
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
        print("%s - sin horas (%d semanas revisadas)" % (ahora(), con_datos))
        est["firma"] = ""
        if t - est.get("ts_latido", 0) > LATIDO:
            avisar("Monitor de Enel funcionando (%s). Sigo revisando %s, por ahora sin horas."
                   % (ahora(), OFICINA))
            est["ts_latido"] = t

    guardar_estado(est)


if __name__ == "__main__":
    main()
