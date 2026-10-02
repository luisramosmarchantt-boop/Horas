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

import http.client
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
from zoneinfo import ZoneInfo

BASE = os.environ.get("BASE", "https://servicequendalat.enel.com/citaprevia")
PORTADA = BASE + "/?pais=cl"
# los empalmes solo existen en Providencia y La Florida; se puede ampliar
OFICINAS = os.environ.get("OFICINAS", os.environ.get("OFICINA", r"PROVIDENCIA|LA FLORIDA"))
PATRON = os.environ.get("PATRON", r"empalme")
MESES = int(os.environ.get("MESES", "4"))
ESTADO = os.environ.get("ESTADO", "estado.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# techo de sondeo por corrida en horario habil, en minutos (repo privado)
SONDEO_MAX = int(os.environ.get("SONDEO_MAX", "40"))
# minutos de Actions que nos permitimos gastar al mes en un repo privado; el
# plan gratis da 2000 y si se agotan GitHub deja de correr el monitor hasta
# fin de mes, asi que se deja holgura
LIMITE_MES = int(os.environ.get("LIMITE_MES", "1700"))
# corridas en horario habil que GitHub alcanza a arrancar por dia (medido:
# 2,7 entre el 12 y el 28 de septiembre de 2026)
CORRIDAS_HABILES = float(os.environ.get("CORRIDAS_HABILES", "3"))
# lo que cuesta una corrida fuera de horario (una pasada + arranque), en s
COSTO_FUERA = int(os.environ.get("COSTO_FUERA", "90"))
# en repo publico los minutos son gratis: se vigila de corrido, con el tope
# de 6 horas que GitHub pone a un job
SONDEO_PUBLICO_MAX = int(os.environ.get("SONDEO_PUBLICO_MAX", "345"))
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "")
# pausa entre pasadas, de lunes a viernes en horario habil: los cupos se
# liberan tambien por anulaciones, en la manana o en la tarde
PAUSA = int(os.environ.get("PAUSA", "45"))
# fines de semana de dia
PAUSA_FINDE = int(os.environ.get("PAUSA_FINDE", "90"))
# Enel publica la semana subsiguiente los lunes en la manana: en el ultimo mes
# la semana nueva aparecio siempre entre las 09:34 y las 10:43. En esa franja
# cada pasada recorre el calendario entero para verla aparecer al tiro.
PUBLICACION_DESDE = int(os.environ.get("PUBLICACION_DESDE", "8"))
PUBLICACION_HASTA = int(os.environ.get("PUBLICACION_HASTA", "12"))
# fuera de esa franja, el calendario entero se recorre cada tantos segundos;
# entremedio solo se vuelven a mirar los dias ya publicados, que es donde
# aparecen las anulaciones. Una pasada completa son ~110 consultas a Enel y
# una rapida ~20: repetir las completas cada 45 s todo el dia arriesga que
# Enel bloquee el acceso y el monitor quede ciego.
REDESCUBRIR = int(os.environ.get("REDESCUBRIR", "300"))
# de noche no se publica ni se agenda: se espacia para no cargar el sitio
PAUSA_NOCHE = int(os.environ.get("PAUSA_NOCHE", "300"))
# franja horaria chilena en que Enel carga y libera cupos
HORA_DESDE = int(os.environ.get("HORA_DESDE", "8"))
HORA_HASTA = int(os.environ.get("HORA_HASTA", "19"))

# cada cuanto repetir un aviso si la disponibilidad no cambio
REPETIR_AVISO = 2 * 3600
# cada cuanto avisar de una falla del monitor
REPETIR_ERROR = 6 * 3600
# cuanto tiempo seguido tiene que fallar antes de avisar: Enel corta alguna
# conexion suelta de vez en cuando (1 de 123 pasadas el 02-10) y eso no
# merece un aviso si la pasada siguiente sale bien
AVISO_FALLA = int(os.environ.get("AVISO_FALLA", str(15 * 60)))
# cada cuanto mandar el "sigo vivo"
LATIDO = 24 * 3600

# hora oficial de Chile continental: cambia sola entre invierno (UTC-4) y
# verano (UTC-3). Con un desfase fijo los avisos marcaban una hora menos
# desde el cambio de horario del 6 de septiembre de 2026.
CHILE = ZoneInfo("America/Santiago")
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
        except (OSError, http.client.HTTPException) as e:
            # URLError, timeouts y conexiones que Enel corta sin responder
            # (RemoteDisconnected) son pasajeros: se reintenta
            ultimo = e
            time.sleep(3 * (intento + 1))
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


def revisar(mapa=None):
    """Una pasada completa.

    Devuelve (hallazgos, firma, publicados, dias_vistos, mapa), donde 'publicados' son
    todos los dias que Enel tiene con horario cargado, tengan cupo o no: que
    aparezca una semana nueva es la senal mas temprana que existe, porque los
    cupos se toman a las pocas horas de publicarse.

    Con 'mapa' (lo que devolvio una pasada completa) hace una pasada rapida:
    vuelve a mirar solo los dias ya publicados.

    Lanza SitioCambio si nada responde, para no confundir una caida con un
    "no hay horas".
    """
    ab = abridor()
    hoy = datetime.now(CHILE).date()
    hallazgos = []
    marcas = []
    publicados = {}
    vistos = 0

    if mapa is None:
        # pasada completa: descubrir oficinas, tramites y ventanas publicadas
        objetivos = []
        for idof, nombre in oficinas(ab):
            for idserv, etiqueta in servicios(ab, idof):
                dias = dias_publicados(ab, idserv, idof, hoy)
                objetivos.append((idof, nombre, idserv, etiqueta, sorted(dias)))
        mapa = objetivos
    else:
        # pasada rapida: solo los dias ya conocidos; la portada entrega la
        # sesion que la API necesita
        traer(ab, PORTADA)
        objetivos = [(o, n, s_, e, [f for f in fs if f >= hoy.isoformat()])
                     for o, n, s_, e, fs in mapa]

    actual = None
    for idof, nombre, idserv, etiqueta, dias in objetivos:
        if idof != actual:
            print("oficina:", nombre, "(id %s)" % idof)
            actual = idof
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

    return hallazgos, "|".join(sorted(marcas)), publicados, vistos, mapa


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


def una_pasada(est, cache=None):
    """Revisa y avisa lo que corresponda. Devuelve True si mando algun aviso.

    'cache' guarda entre pasadas de una misma corrida el mapa de dias
    publicados, para alternar pasadas completas y rapidas."""
    t = time.time()
    if cache is None:
        cache = {}
    completa = (not cache.get("mapa")
                or t - cache.get("ts", 0) >= REDESCUBRIR
                or en_publicacion(datetime.now(CHILE))
                or not any(fs for *_, fs in cache["mapa"]))
    try:
        hallazgos, firma, publicados, con_datos, mapa = revisar(
            None if completa else cache["mapa"])
        if completa:
            cache["mapa"], cache["ts"] = mapa, t
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        desde = est.setdefault("falla_desde", t)
        if t - desde >= AVISO_FALLA and t - est.get("ts_error", 0) > REPETIR_ERROR:
            avisar("PROBLEMA CON EL MONITOR DE ENEL\n(%s)\n\nFalla desde hace %d min: %s\n\n"
                   "Mientras no se arregle, no puede avisarte de horas nuevas."
                   % (ahora(), (t - desde) // 60, e))
            est["ts_error"] = t
        guardar_estado(est)
        raise

    if est.get("ts_error"):
        avisar("Monitor de Enel recuperado (%s). Vuelvo a revisar normalmente."
               % ahora())
    est["ts_error"] = 0
    est.pop("falla_desde", None)
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


def repo_publico():
    """Pregunta a GitHub si el repo es publico. Ante la duda, privado: asi
    nunca se gasta de mas."""
    if not (GH_TOKEN and GH_REPO):
        return False
    try:
        pedido = urllib.request.Request(
            "https://api.github.com/repos/%s" % GH_REPO,
            headers={"Authorization": "Bearer %s" % GH_TOKEN,
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(pedido, timeout=20) as r:
            return json.loads(r.read().decode()).get("private") is False
    except Exception as e:
        print("no pude leer la visibilidad del repo (%s); asumo privado" % e)
        return False


def pausa_para(momento):
    if not HORA_DESDE <= momento.hour < HORA_HASTA:
        return PAUSA_NOCHE
    if momento.weekday() >= 5:
        return PAUSA_FINDE
    return PAUSA


def en_publicacion(momento):
    return momento.weekday() == 0 and PUBLICACION_DESDE <= momento.hour < PUBLICACION_HASTA


def en_horario(momento):
    return momento.weekday() < 5 and HORA_DESDE <= momento.hour < HORA_HASTA


def habiles_restantes(momento):
    """Dias habiles que quedan en el mes, contando hoy."""
    d = momento.date()
    n = 0
    while d.month == momento.month:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return max(1, n)


def dias_restantes(momento):
    d = momento.date()
    n = 0
    while d.month == momento.month:
        n += 1
        d += timedelta(days=1)
    return n


def segundos_de_sondeo(momento, est, publico):
    """Cuanto rato sondear en esta corrida.

    GitHub estrangula los schedules: se pide cada 15 minutos y en la practica
    arranca cada ~3,5 horas. No controlamos cuando arranca la corrida, pero
    si cuanto dura.

    - Repo publico: minutos gratis, asi que cada corrida vigila casi 6 horas
      a cualquier hora. Como cada corrida dura mas que el hueco entre
      arranques, se encadenan y la vigilancia queda continua.
    - Repo privado: se reparte lo que queda del presupuesto del mes entre las
      corridas habiles que faltan. Si el mes viene holgado las ventanas se
      alargan solas, y si viene justo se achican, sin pasarse nunca.
    """
    if publico:
        # gratis: cada corrida vigila todo lo que GitHub deja, a cualquier
        # hora. Como los arranques no llegan a tiempo fijo, una corrida que
        # parte de madrugada es la que termina cubriendo el lunes a las 10.
        return SONDEO_PUBLICO_MAX * 60
    if not en_horario(momento):
        return 0

    usados = consumo_del_mes(est, momento)
    reserva = COSTO_FUERA * 4 * dias_restantes(momento)  # corridas nocturnas y de fin de semana
    disponible = LIMITE_MES * 60 - usados - reserva
    por_corrida = disponible / (habiles_restantes(momento) * CORRIDAS_HABILES)
    return int(max(0, min(por_corrida, SONDEO_MAX * 60)))


def consumo_supuesto(momento):
    """Sin registro del mes (primera corrida, o cache perdido a mitad de mes)
    no sabemos cuanto se gasto: se supone lo proporcional a los dias ya
    pasados, que es conservador."""
    total = (dias_restantes(momento) + momento.day - 1)
    return int(LIMITE_MES * 60 * (momento.day - 1) / total)


def consumo_del_mes(est, momento):
    mes = momento.strftime("%Y-%m")
    c = est.get("consumo") or {}
    if c.get("mes") == mes:
        return c.get("segundos", 0)
    return 0 if momento.day == 1 else consumo_supuesto(momento)


def anotar_consumo(est, momento, segundos):
    mes = momento.strftime("%Y-%m")
    c = est.get("consumo") or {}
    if c.get("mes") != mes:
        c = {"mes": mes, "segundos": consumo_del_mes({}, momento)}
    # GitHub cobra por minuto entero y el arranque del job tambien cuenta
    c["segundos"] = c.get("segundos", 0) + (int(segundos) // 60 + 1) * 60 + 30
    est["consumo"] = c


def main():
    est = cargar_estado()
    inicio = datetime.now(CHILE)
    t0 = time.time()
    publico = repo_publico()
    tope = segundos_de_sondeo(inicio, est, publico)
    fin = t0 + tope
    print("%s - repo %s, sondeando %d min, cada %d s (consumo del mes: %d min)"
          % (ahora(), "publico" if publico else "privado", tope // 60, PAUSA,
             consumo_del_mes(est, inicio) // 60))

    pasada = 0
    fallas = 0
    ultimo = None
    cache = {}
    try:
        while True:
            pasada += 1
            print("--- pasada %d ---" % pasada)
            try:
                una_pasada(est, cache)
            except Exception as e:
                # una caida puntual no puede comerse el resto de la ventana: se
                # anota y se sigue sondeando, y recien al final se decide
                fallas += 1
                ultimo = e
            pausa = pausa_para(datetime.now(CHILE))
            if time.time() + pausa >= fin:
                break
            time.sleep(pausa)
    finally:
        anotar_consumo(est, inicio, time.time() - t0)
        guardar_estado(est)

    print("%s - fin de la corrida (%d pasadas, %d fallidas)" % (ahora(), pasada, fallas))
    if fallas == pasada:
        # ninguna pasada llego a leer el sitio: la corrida tiene que salir roja
        raise ultimo


if __name__ == "__main__":
    main()
