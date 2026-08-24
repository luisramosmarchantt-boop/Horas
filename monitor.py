"""Monitor de horas disponibles para citas de Enel.

Revisa el agendamiento online de Enel para una oficina y un conjunto de
tramites, y avisa por Telegram cuando aparecen dias con horas.

Deteccion: el sitio usa un datepicker de jQuery UI. Los dias sin horas
quedan marcados como 'ui-datepicker-unselectable ui-state-disabled'
(con clases propias 'diaPasado' / 'diaOcupado') y se dibujan con <span>;
un dia CON horas queda seleccionable y se dibuja con <a>. Se confirma
haciendo click en el dia y leyendo los horarios que aparecen.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

URL = os.environ.get("URL", "https://servicequendalat.enel.com/citaprevia/?pais=cl")
OFICINA = os.environ.get("OFICINA", r"PROVIDENCIA")
PATRON = os.environ.get("PATRON", r"empalme")
MESES = int(os.environ.get("MESES", "3"))
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


JS_CELDAS = """() => {
  const out = [];
  document.querySelectorAll('.ui-datepicker-calendar td').forEach(td => {
    const t = (td.innerText || '').trim();
    if (!/^\\d{1,2}$/.test(t)) return;
    const cls = (td.className || '').trim();
    out.push({
      dia: t,
      cls: cls,
      libre: !/ui-datepicker-unselectable|ui-state-disabled/.test(cls),
      link: !!td.querySelector('a')
    });
  });
  return out;
}"""

JS_MES = """() => {
  const m = document.querySelector('.ui-datepicker-month');
  const y = document.querySelector('.ui-datepicker-year');
  if (m && y) return m.innerText.trim() + ' ' + y.innerText.trim();
  return 'mes desconocido';
}"""

JS_HORAS = """() => {
  const zona = document.querySelector('#columnaDerecha') || document.body;
  const txt = zona.innerText || '';
  if (/no hay disponibilidad/i.test(txt)) return [];
  return [...new Set(txt.match(/\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/g) || [])];
}"""


def elegir_texto(page, patron, espera=30):
    """Elige en cualquier <select> la primera opcion que calce con el patron."""
    fin = time.time() + espera
    while time.time() < fin:
        for s in page.query_selector_all("select"):
            for op in s.query_selector_all("option"):
                txt = (op.inner_text() or "").strip()
                val = op.get_attribute("value")
                if val and re.search(patron, txt, re.I):
                    s.select_option(val)
                    return val, txt
        page.wait_for_timeout(1000)
    return None


def elegir_valor(page, valor):
    for s in page.query_selector_all("select"):
        for op in s.query_selector_all("option"):
            if op.get_attribute("value") == valor:
                s.select_option(valor)
                return True
    return False


def continuar(page):
    for t in ["Continuar", "Siguiente", "Aceptar"]:
        b = page.query_selector("text=%s" % t)
        if b and b.is_visible():
            b.click()
            return True
    return False


def avanzar_mes(page):
    el = page.query_selector("a.ui-datepicker-next")
    if el and el.is_visible() and "ui-state-disabled" not in (el.get_attribute("class") or ""):
        el.click()
        page.wait_for_timeout(2500)
        return True
    return False


def listar_tramites(page, espera=20):
    fin = time.time() + espera
    while time.time() < fin:
        lista = []
        for s in page.query_selector_all("select"):
            for op in s.query_selector_all("option"):
                v = op.get_attribute("value")
                t = (op.inner_text() or "").strip()
                if v and re.search(PATRON, t, re.I):
                    lista.append((v, t))
        if lista:
            return lista
        page.wait_for_timeout(1000)
    return []


def horas_del_dia(page, dia):
    """Hace click en el dia y devuelve los horarios que muestre el sitio."""
    celda = page.query_selector(
        ".ui-datepicker-calendar td:not(.ui-state-disabled):not(.ui-datepicker-unselectable) a"
        ":text('%s')" % dia
    )
    if celda is None:
        for td in page.query_selector_all(
            ".ui-datepicker-calendar td:not(.ui-state-disabled):not(.ui-datepicker-unselectable)"
        ):
            if (td.inner_text() or "").strip() == dia:
                celda = td.query_selector("a") or td
                break
    if celda is None:
        return []
    celda.click()
    page.wait_for_timeout(4000)
    return page.evaluate(JS_HORAS)


def revisar(page):
    """Devuelve (hallazgos, celdas_vistas). Lanza excepcion si el sitio no responde."""
    hallazgos = []
    vistas = 0

    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(5000)
    if not elegir_texto(page, OFICINA):
        raise RuntimeError("no encontre la oficina %r en el sitio" % OFICINA)
    page.wait_for_timeout(4000)

    tramites = listar_tramites(page)
    if not tramites:
        raise RuntimeError("no encontre tramites que calcen con %r" % PATRON)
    print("tramites:", [t for _, t in tramites])

    for valor, etiqueta in tramites:
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(5000)
        elegir_texto(page, OFICINA)
        page.wait_for_timeout(4000)
        if not elegir_valor(page, valor):
            print("!! no pude seleccionar", etiqueta, file=sys.stderr)
            continue
        page.wait_for_timeout(2000)
        continuar(page)
        page.wait_for_timeout(5000)

        for _ in range(MESES):
            mes = page.evaluate(JS_MES)
            celdas = page.evaluate(JS_CELDAS)
            vistas += len(celdas)
            libres = [c for c in celdas if c["libre"] or c["link"]]
            print("%s | %s | %d celdas | %d seleccionables"
                  % (etiqueta, mes, len(celdas), len(libres)))
            for c in libres:
                horas = horas_del_dia(page, c["dia"])
                print("    dia %s (%s) -> %s" % (c["dia"], c["cls"], horas or "sin horarios"))
                if horas:
                    hallazgos.append("%s: %s de %s -> %s"
                                     % (etiqueta, c["dia"], mes, ", ".join(sorted(horas)[:8])))
                else:
                    # dia seleccionable pero sin horarios visibles: igual vale avisar
                    hallazgos.append("%s: %s de %s (dia habilitado)" % (etiqueta, c["dia"], mes))
                # volver al calendario del mes por si el click cambio la vista
                mes_actual = page.evaluate(JS_MES)
                if mes_actual != mes:
                    break
            if not avanzar_mes(page):
                break

    if vistas == 0:
        raise RuntimeError("no encontre ninguna celda de calendario; el sitio cambio")
    return hallazgos, vistas


def main():
    est = cargar_estado()
    t = time.time()
    try:
        with sync_playwright() as p:
            nav = p.chromium.launch()
            page = nav.new_page(viewport={"width": 1280, "height": 1400})
            try:
                hallazgos, vistas = revisar(page)
            finally:
                try:
                    page.screenshot(path="calendario.png", full_page=True)
                except Exception:
                    pass
                nav.close()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        if t - est.get("ts_error", 0) > REPETIR_ERROR:
            avisar("PROBLEMA CON EL MONITOR DE ENEL\n(%s)\n\n%s\n\nRevisa el workflow en GitHub."
                   % (ahora(), e))
            est["ts_error"] = t
            guardar_estado(est)
        raise

    est["ts_error"] = 0

    if hallazgos:
        firma = "|".join(sorted(hallazgos))
        nuevo = firma != est.get("firma")
        viejo = t - est.get("ts_aviso", 0) > REPETIR_AVISO
        if nuevo or viejo:
            avisar("HAY HORAS - ENEL %s\n(%s)\n\n%s\n\n%s"
                   % (OFICINA, ahora(), "\n".join(hallazgos[:30]), URL))
            est["firma"] = firma
            est["ts_aviso"] = t
            est["ts_latido"] = t
        else:
            print("mismos dias ya avisados, no repito")
    else:
        print(ahora(), "- sin horas (%d celdas revisadas)" % vistas)
        est["firma"] = ""
        if t - est.get("ts_latido", 0) > LATIDO:
            avisar("Monitor de Enel funcionando (%s). Sigo revisando %s, por ahora sin horas."
                   % (ahora(), OFICINA))
            est["ts_latido"] = t

    guardar_estado(est)


if __name__ == "__main__":
    main()
