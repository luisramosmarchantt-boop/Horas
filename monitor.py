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

Sobre ese endpoint hay tres cosas que conviene tener claras:

  - Solo responde cuando la fecha pedida cae dentro de una ventana con
    horario publicado. Medido el 31-08-2026 en Providencia: fecha=2026-08-31
    devuelve {}, fecha=2026-09-01 devuelve del 07 al 11 de septiembre, y
    fecha=2026-10-01 devuelve {} de nuevo. Por eso no sirve anclar en hoy ni
    en el dia 1 de cada mes: hay que ir caminando las ventanas.

  - 'franjas' solo viene rellena cuando la fecha pedida es la del propio dia;
    desde otra ancla llega nula. Asi que para saber si un dia tiene cupo hay
    que preguntar por ese dia.

  - 'estado' no alcanza para decidir: los dias del 07 al 11 de septiembre
    llegaban con estado=1 y franjas=null desde el ancla del mes, y al anclar
    en cada dia aparecian sus 10 franjas. La verdad esta en huecosLibres.
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
# los empalmes solo existen en Providencia y La Florida; se puede ampliar
OFICINAS = os.environ.get("OFICINAS", os.environ.get("OFICINA", r"PROVIDENCIA|LA FLORIDA"))
PATRON = os.environ.get("PATRON", r"empalme")
MESES = int(os.environ.get("MESES", "4"))
ESTADO = os.environ.get("ESTADO", "estado.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# cuanto rato se queda sondeando cada corrida, en minutos
SONDEO_UTIL = int(os.environ.get("SONDEO_UTIL", "15"))
SONDEO_FUERA = int(os.environ.get("SONDEO_FUERA", "0"))
# pausa entre pasadas dentro de una misma corrida
PAUSA = int(os.environ.get("PAUSA", "90"))
# franja horaria chilena en que Enel carga y libera cupos
HORA_DESDE = int(os.environ.get("HORA_DESDE", "8"))
HORA_HASTA = int(os.environ.get("HORA_HASTA", "19"))

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


def oficinas(ab):
    """Todas las oficinas del <select> de la portada que calzan con el patron."""
    html = traer(ab, PORTADA)
    opciones = re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)</option>', html)
    if not opciones:
        raise SitioCambio("la portada no trae el listado de oficinas")
    elegidas = [(i, n.strip()) for i, n in opciones if re.search(OFICINAS, n, re.I)]
    if not elegidas:
        raise SitioCambio("ninguna oficina calza con %r; hay %d en la portada"
                          % (OFICINAS, len(opciones)))
    return elegidas


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


def semillas(hoy, meses):
    """Fechas desde donde tantear: el endpoint solo responde dentro de una
    ventana publicada, asi que se prueban varios puntos del calendario."""
    fechas = [hoy, hoy + timedelta(days=1)]
    a, m = hoy.year, hoy.month
    for _ in range(meses):
        fechas.append(date(a, m, 1))
        fechas.append(date(a, m, 15))
        m += 1
        if m > 12:
            a, m = a + 1, 1
    return [f.isoformat() for f in fechas]


def dias_publicados(ab, idserv, idof, hoy, tope_consultas=30):
    """Camina las ventanas publicadas y devuelve {fecha: dia}.

    Parte de las semillas y, cada vez que una responde, se asoma mas alla del
    ultimo dia devuelto, porque las ventanas siguientes solo aparecen si se
    pregunta por una fecha que caiga dentro de ellas.
    """
    vistos = {}
    pendientes = semillas(hoy, MESES)
    intentadas = set()
    while pendientes and len(intentadas) < tope_consultas:
        f = pendientes.pop(0)
        if f in intentadas:
            continue
        intentadas.add(f)
        dias = calendario(ab, idserv, idof, f)
        if not dias:
            continue
        fechas = [d["fecha"] for d in dias if d.get("fecha")]
        for d in dias:
            if d.get("fecha"):
                vistos.setdefault(d["fecha"], d)
        if fechas:
            sig = (date.fromisoformat(max(fechas)) + timedelta(days=3)).isoformat()
            if sig not in intentadas:
                pendientes.append(sig)
    return vistos


def horas_del_dia(ab, idserv, idof, fecha):
    """Horas con cupo de un dia. Hay que anclar en el dia: desde otra fecha
    las franjas llegan nulas y el dia parece lleno aunque no lo este."""
    for d in calendario(ab, idserv, idof, fecha):
        if d.get("fecha") == fecha:
            return horas_libres(d), d.get("estado")
    return [], None


def etiqueta_dia(iso):
    try:
        d = date.fromisoformat(iso)
        return "%s %02d-%02d" % (DIAS[d.weekday()], d.day, d.month)
    except Exception:
        return iso


def revisar():
    """Una pasada completa.

    Devuelve (hallazgos, firma, publicados, dias_vistos), donde 'publicados' son
    todos los dias que Enel tiene con horario cargado, tengan cupo o no: que
    aparezca una semana nueva es la senal mas temprana que existe, porque los
    cupos se toman a las pocas horas de publicarse.

    Lanza SitioCambio si nada responde, para no confundir una caida con un
    "no hay horas".
    """
    ab = abridor()
    hoy = datetime.now(CHILE).date()
    hallazgos = []
    marcas = []
    publicados = {}
    vistos = 0

    for idof, nombre in oficinas(ab):
        print("oficina:", nombre, "(id %s)" % idof)
        for idserv, etiqueta in servicios(ab, idof):
            dias = dias_publicados(ab, idserv, idof, hoy)
            vistos += len(dias)
            if not dias:
                print("  %s -> sin ventana publicada" % etiqueta)
                continue

            for fecha in dias:
                publicados["%s::%s::%s" % (idof, idserv, fecha)] = (nombre, etiqueta, fecha)

            libres = {}
            for fecha in sorted(dias):
                horas, estado = horas_del_dia(ab, idserv, idof, fecha)
                if horas:
                    libres[fecha] = horas
                elif estado == 0:
                    # el sitio lo pinta seleccionable aunque no leamos franjas
                    libres[fecha] = []

            print("  %s -> %s..%s (%d dias), %d con cupo"
                  % (etiqueta, min(dias), max(dias), len(dias), len(libres)))
            if not libres:
                continue

            lineas = []
            for fecha, horas in sorted(libres.items()):
                if horas:
                    extra = " (+%d mas)" % (len(horas) - 4) if len(horas) > 4 else ""
                    lineas.append("  %s: %s%s"
                                  % (etiqueta_dia(fecha), ", ".join(horas[:4]), extra))
                else:
                    lineas.append("  %s: dia habilitado" % etiqueta_dia(fecha))
                print("    %s: %s" % (fecha, ", ".join(horas) or "sin detalle de horas"))
            hallazgos.append("%s - %s\n%s" % (nombre, etiqueta, "\n".join(lineas)))
            # la firma va por dia, no por hora: si alguien toma una hora suelta
            # del mismo dia no tiene sentido volver a avisar de ese dia
            marcas.extend("%s::%s::%s" % (idof, etiqueta, f) for f in sorted(libres))

    if vistos == 0:
        raise SitioCambio("ninguna oficina devolvio dias publicados; "
                          "el sitio o la API cambiaron")

    return hallazgos, "|".join(sorted(marcas)), publicados, vistos


def avisar_publicados(est, publicados, t):
    """Avisa cuando Enel carga dias que no habiamos visto nunca."""
    conocidos = est.get("publicados")
    if conocidos is None:
        # primera vez: tomar nota sin avisar, si no avisaria de todo el calendario
        est["publicados"] = sorted(publicados)
        print("primer registro: %d dias publicados anotados sin avisar" % len(publicados))
        return False

    conocidos = set(conocidos)
    nuevos = [publicados[k] for k in publicados if k not in conocidos]
    # se acumula y se podan los dias ya pasados: si una pasada falla a medias,
    # los dias que no vinieron esta vez no deben reaparecer como "nuevos"
    hoy = datetime.now(CHILE).date().isoformat()
    est["publicados"] = sorted(k for k in conocidos | set(publicados)
                               if k.rsplit("::", 1)[-1] >= hoy)
    if not nuevos:
        return False

    porgrupo = {}
    for nombre, etiqueta, fecha in nuevos:
        porgrupo.setdefault("%s - %s" % (nombre, etiqueta), []).append(fecha)
    detalle = "\n".join("%s\n  %s" % (g, ", ".join(etiqueta_dia(f) for f in sorted(fs)))
                        for g, fs in sorted(porgrupo.items()))
    avisar("ENEL PUBLICO DIAS NUEVOS\n(%s)\n\n%s\n\nLos cupos se toman rapido, "
           "conviene entrar ahora.\n\n%s" % (ahora(), detalle, PORTADA))
    est["ts_latido"] = t
    return True


def una_pasada(est):
    """Revisa y avisa lo que corresponda. Devuelve True si mando algun aviso."""
    t = time.time()
    try:
        hallazgos, firma, publicados, con_datos = revisar()
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
    aviso = avisar_publicados(est, publicados, t)

    if hallazgos:
        nuevo = firma != est.get("firma")
        vencido = t - est.get("ts_aviso", 0) > REPETIR_AVISO
        if nuevo or vencido:
            avisar("HAY HORAS - ENEL\n(%s)\n\n%s\n\n%s"
                   % (ahora(), "\n\n".join(hallazgos), PORTADA))
            est["firma"] = firma
            est["ts_aviso"] = t
            est["ts_latido"] = t
            aviso = True
        else:
            print("las mismas horas ya avisadas, no repito")
    else:
        print("%s - sin horas (%d dias revisados)" % (ahora(), con_datos))
        est["firma"] = ""
        if t - est.get("ts_latido", 0) > LATIDO:
            avisar("Monitor de Enel funcionando (%s). Sigo revisando %s, por ahora sin horas."
                   % (ahora(), OFICINAS))
            est["ts_latido"] = t
            aviso = True

    guardar_estado(est)
    return aviso


def minutos_de_sondeo(momento):
    """Cuanto rato sondear en esta corrida.

    GitHub estrangula los schedules de los repos privados: pedimos una revision
    cada 15 minutos y en la practica corre cada ~3,4 horas. Como no controlamos
    cuando arranca la corrida, cada una se queda un rato sondeando en vez de
    mirar una sola vez. El presupuesto de Actions (~2000 min al mes) se gasta
    donde sirve: en el horario en que Enel carga y libera cupos.
    """
    if momento.weekday() >= 5:
        return SONDEO_FUERA
    return SONDEO_UTIL if HORA_DESDE <= momento.hour < HORA_HASTA else SONDEO_FUERA


def main():
    est = cargar_estado()
    inicio = datetime.now(CHILE)
    tope = minutos_de_sondeo(inicio) * 60
    fin = time.time() + tope
    print("%s - sondeando hasta %d min, cada %d s" % (ahora(), tope // 60, PAUSA))

    pasada = 0
    fallas = 0
    ultimo = None
    while True:
        pasada += 1
        print("--- pasada %d ---" % pasada)
        try:
            una_pasada(est)
        except Exception as e:
            # una caida puntual no puede comerse el resto de la ventana: se
            # anota y se sigue sondeando, y recien al final se decide
            fallas += 1
            ultimo = e
        if time.time() + PAUSA >= fin:
            break
        time.sleep(PAUSA)

    print("%s - fin de la corrida (%d pasadas, %d fallidas)" % (ahora(), pasada, fallas))
    if fallas == pasada:
        # ninguna pasada llego a leer el sitio: la corrida tiene que salir roja
        raise ultimo


if __name__ == "__main__":
    main()
