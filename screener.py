#!/usr/bin/env python3
"""
Bot de Telegram: "calidad en caída" (valores de España).

Cada ejecución manda UN mensaje con:
  ✅ CUMPLEN -> pasan todas las pautas
  🟡 LAS 5 MÁS CERCA -> no cumplen, pero son las que menos les falta

Uso:
  python screener.py                                  # normal: envía a Telegram
  python screener.py --dry-run                        # solo imprime, no envía ni guarda
  python screener.py --test                           # manda un mensaje de prueba
  python screener.py --tickers ITX.MC,IDR.MC --dry-run

Variables de entorno necesarias para enviar:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# ----------------------------------------------------------------------------
# PAUTAS (aquí afinas lo que quieres que te avise)
# ----------------------------------------------------------------------------
RULES = {
    "min_drawdown": 0.30,             # caída mínima desde el máximo de 52 semanas (30%)
    "min_net_margin": 0.05,           # margen neto mínimo: sigue ganando dinero
    "min_operating_margin": 0.10,     # margen operativo mínimo
    "min_revenue_growth": -0.05,      # los ingresos no se hunden (>= -5% interanual)
    "max_debt_to_equity": 150.0,      # deuda/capital máxima (en %, formato Yahoo)
    "max_forward_pe": 25.0,           # PER a futuro máximo
    "require_positive_fcf": True,     # exige flujo de caja libre positivo
    "min_rebound_from_20d_low": 0.0,  # 0 = desactivado. Ej: 0.03 exige estar un 3% sobre el mínimo de 20 días
}

# Bancos y aseguradoras (sector "Financial Services" en Yahoo): el margen operativo,
# el flujo de caja libre y la deuda no se miden igual, así que solo se evalúan
# beneficio neto positivo, ingresos y PER bajo.
RULES_FINANCIALS = {
    "min_drawdown": RULES["min_drawdown"],
    "min_net_margin": 0.03,
    "min_revenue_growth": -0.05,
    "max_forward_pe": 12.0,
    "min_rebound_from_20d_low": RULES["min_rebound_from_20d_low"],
}

TOP_NEAR = 5                # cuántas "que no cumplen pero están cerca" se muestran
REVIEW_MIN_DRAWDOWN = 0.10  # solo se revisan valores con al menos esta caída...
MAX_REVIEW = 80             # ...y como mucho los N que más han caído (al ser semanal, hay tiempo de sobra)
MAX_CUMPLE = 10             # máximo de empresas que cumplen listadas en el mensaje (el resto: "y N más")
SHOW_EXTRA = True           # en los que cumplen: recompras y activo circulante neto
REPEAT_ALERTS = True        # True = avisa cada ejecución aunque ya lo hubiera avisado antes
REALERT_AFTER_DAYS = 30     # (solo si REPEAT_ALERTS = False) repite pasados estos días...
REALERT_IF_WORSE_BY = 0.10  # ...o si ha caído 10 puntos más desde el último aviso

# Universo: valores españoles en Yahoo Finance (sufijo .MC = Bolsa de Madrid).
# IBEX 35 + una selección de medianas y pequeñas. La lista es de memoria: si algún
# valor ya no cotiza o cambió de ticker, el log lo marcará como "sin datos de precio".
SPAIN_TICKERS = [
    # IBEX 35
    "ACS.MC", "ACX.MC", "AENA.MC", "AMS.MC", "ANA.MC", "ANE.MC", "BBVA.MC",
    "BKT.MC", "CABK.MC", "CLNX.MC", "COL.MC", "ELE.MC", "ENG.MC", "FDR.MC",
    "FER.MC", "GRF.MC", "IAG.MC", "IBE.MC", "IDR.MC", "ITX.MC", "LOG.MC",
    "MAP.MC", "MEL.MC", "MRL.MC", "MTS.MC", "NTGY.MC", "PHM.MC", "PUIG.MC",
    "RED.MC", "REP.MC", "ROVI.MC", "SAB.MC", "SAN.MC", "SCYR.MC", "SLR.MC",
    "TEF.MC", "UNI.MC",
    # Medianas y pequeñas
    "A3M.MC", "ADX.MC", "ALB.MC", "ALM.MC", "CASH.MC", "CIE.MC", "DOM.MC",
    "EBRO.MC", "ECR.MC", "ENC.MC", "ENO.MC", "FAE.MC", "GCO.MC", "GEST.MC",
    "GRE.MC", "HOME.MC", "PSG.MC", "RJF.MC", "SOL.MC", "TLGO.MC", "TUB.MC",
    "VID.MC", "VIS.MC", "ZOT.MC",
    # Más del Mercado Continuo
    "ADZ.MC", "AMP.MC", "ARM.MC", "AZK.MC", "CAF.MC", "CBAV.MC", "CIRSA.MC",
    "DIA.MC", "EDR.MC", "ENER.MC", "GSJ.MC", "IBG.MC", "ISUR.MC", "LDA.MC",
    "LGT.MC", "MCM.MC", "MDF.MC", "MVC.MC", "NEA.MC", "NTH.MC", "OHLA.MC",
    "ORY.MC", "PRM.MC", "PRS.MC", "R4.MC", "REN.MC", "RLIA.MC", "TRE.MC",
    "VOC.MC",
]

# Valores extra que quieras añadir (mismo formato). Ejemplo: "CAF.MC"
EXTRA_TICKERS = []

# Nombres cortos para el mensaje (Yahoo los da en mayúsculas y larguísimos).
TICKER_NAMES = {
    "ACS.MC": "ACS", "ACX.MC": "Acerinox", "AENA.MC": "Aena", "AMS.MC": "Amadeus",
    "ANA.MC": "Acciona", "ANE.MC": "Acciona Energía", "BBVA.MC": "BBVA",
    "BKT.MC": "Bankinter", "CABK.MC": "CaixaBank", "CLNX.MC": "Cellnex",
    "COL.MC": "Colonial", "ELE.MC": "Endesa", "ENG.MC": "Enagás", "FDR.MC": "Fluidra",
    "FER.MC": "Ferrovial", "GRF.MC": "Grifols", "IAG.MC": "IAG", "IBE.MC": "Iberdrola",
    "IDR.MC": "Indra", "ITX.MC": "Inditex", "LOG.MC": "Logista", "MAP.MC": "Mapfre",
    "MEL.MC": "Meliá", "MRL.MC": "Merlin", "MTS.MC": "ArcelorMittal", "NTGY.MC": "Naturgy",
    "PHM.MC": "PharmaMar", "PUIG.MC": "Puig", "RED.MC": "Redeia", "REP.MC": "Repsol",
    "ROVI.MC": "Rovi", "SAB.MC": "Sabadell", "SAN.MC": "Santander", "SCYR.MC": "Sacyr",
    "SLR.MC": "Solaria", "TEF.MC": "Telefónica", "UNI.MC": "Unicaja",
    "A3M.MC": "Atresmedia", "ADX.MC": "Audax", "ALB.MC": "Alba", "ALM.MC": "Almirall",
    "CASH.MC": "Prosegur Cash", "CIE.MC": "CIE Automotive", "DOM.MC": "Dominion",
    "EBRO.MC": "Ebro Foods", "ECR.MC": "Ercros", "ENC.MC": "Ence", "ENO.MC": "Elecnor",
    "FAE.MC": "Faes Farma", "GCO.MC": "Catalana Occidente", "GEST.MC": "Gestamp",
    "GRE.MC": "Grenergy", "HOME.MC": "Neinor", "PSG.MC": "Prosegur", "RJF.MC": "Reig Jofre",
    "SOL.MC": "Soltec", "TLGO.MC": "Talgo", "TUB.MC": "Tubacex", "VID.MC": "Vidrala",
    "VIS.MC": "Viscofan", "ZOT.MC": "Zardoya Otis",
    "ADZ.MC": "Adolfo Domínguez", "AMP.MC": "Amper", "ARM.MC": "Árima", "AZK.MC": "Azkoyen",
    "CAF.MC": "CAF", "CBAV.MC": "Clínica Baviera", "CIRSA.MC": "Cirsa", "DIA.MC": "Dia",
    "EDR.MC": "eDreams", "ENER.MC": "Ecoener", "GSJ.MC": "Grupo San José", "IBG.MC": "Iberpapel",
    "ISUR.MC": "Inmobiliaria del Sur", "LDA.MC": "Línea Directa", "LGT.MC": "Lingotes Especiales",
    "MCM.MC": "Miquel y Costas", "MDF.MC": "Duro Felguera", "MVC.MC": "Metrovacesa",
    "NEA.MC": "Nicolás Correa", "NTH.MC": "Naturhouse", "OHLA.MC": "OHLA", "ORY.MC": "Oryzon",
    "PRM.MC": "Prim", "PRS.MC": "Prisa", "R4.MC": "Renta 4", "REN.MC": "Renta Corporación",
    "RLIA.MC": "Realia", "TRE.MC": "Técnicas Reunidas", "VOC.MC": "Vocento",
}

STATE_FILE = Path(__file__).with_name("state.json")

FUND_KEYS = [
    "marketCap", "profitMargins", "operatingMargins", "revenueGrowth",
    "freeCashflow", "debtToEquity", "forwardPE", "trailingPE",
    "returnOnEquity", "earningsGrowth", "totalCash", "totalDebt",
    "sector", "industry", "currency", "financialCurrency",
    "priceToBook", "bookValue", "sharesOutstanding",
]


# ----------------------------------------------------------------------------
# Datos
# ----------------------------------------------------------------------------
def get_universe():
    return sorted(set(SPAIN_TICKERS + EXTRA_TICKERS))


def price_stats(tickers):
    """Etapa 1 (barata): descarga precios de 1 año en lotes y calcula la caída."""
    stats = {}
    for i in range(0, len(tickers), 100):
        batch = tickers[i:i + 100]
        try:
            data = yf.download(
                batch, period="1y", interval="1d", auto_adjust=True,
                group_by="ticker", threads=True, progress=False,
            )
        except Exception as e:
            print(f"[aviso] fallo descargando lote {i}: {e}")
            continue
        for t in batch:
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    close = data[t]["Close"]
                else:
                    close = data["Close"]
                close = close.dropna()
            except KeyError:
                continue
            if len(close) < 120:
                continue
            last = float(close.iloc[-1])
            high = float(close.max())
            low20 = float(close.tail(20).min())
            if high <= 0 or low20 <= 0:
                continue
            stats[t] = {
                "last": last,
                "high": high,
                "drawdown": last / high - 1,
                "rebound20": last / low20 - 1,
            }
    return stats


def fundamentals(ticker):
    """Etapa 2 (más lenta): fundamentales. Reintenta si Yahoo responde vacío o con error."""
    info = {}
    for attempt in range(3):
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as e:
            print(f"[aviso] fundamentales de {ticker} (intento {attempt + 1}/3): {e}")
            info = {}
        if info.get("profitMargins") is not None or info.get("marketCap") is not None:
            break
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    out = {k: info.get(k) for k in FUND_KEYS}
    out["name"] = info.get("shortName") or info.get("longName") or ticker
    return out


def _num(f, key):
    v = f.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def fund_missing(f):
    """True si Yahoo no ha dado ningún dato fundamental de la empresa."""
    return all(_num(f, k) is None for k in ("profitMargins", "operatingMargins", "freeCashflow", "marketCap"))


def is_financial(f):
    return f.get("sector") == "Financial Services"


# ----------------------------------------------------------------------------
# Formato de números (coma decimal)
# ----------------------------------------------------------------------------
def _n(x, dec=1):
    return f"{x:.{dec}f}".replace(".", ",").replace("-", "−")


def _pc(x, dec=1, signed=False):
    s = f"{abs(x) * 100:.{dec}f}".replace(".", ",")
    sign = "−" if x < 0 else ("+" if signed else "")
    return f"{sign}{s}%"


def _sym(cur):
    return {"EUR": "€", "USD": "$", "GBP": "£"}.get(cur or "", cur or "")


def _m(v, cur=""):
    sym = _sym(cur)
    if abs(v) >= 1e9:
        return f"{v / 1e6:,.0f}".replace(",", ".") + f"M{sym}"
    return f"{v / 1e6:.0f}M{sym}"


def _name(t, f):
    if t in TICKER_NAMES:
        return TICKER_NAMES[t]
    raw = str(f.get("name") or t).strip()
    raw = re.sub(r"[,\s]+(S\.?A\.?U?\.?|S\.?L\.?|PLC|SE|N\.?V\.?)\.?$", "", raw, flags=re.I)
    raw = raw.title()
    return raw if len(raw) <= 22 else raw[:21].rstrip() + "…"


# ----------------------------------------------------------------------------
# Criterios
# ----------------------------------------------------------------------------
def checks(stat, f, rules=None):
    """Lista de (estado, texto, distancia). estado = ok | fail | nodata.
    La distancia mide cuánto le falta para cumplir (0 = cumple; más = más lejos)."""
    if rules is None:
        rules = RULES_FINANCIALS if is_financial(f) else RULES
    out = []

    def cap(x):
        return max(0.0, min(2.0, x))

    dd = stat["drawdown"]
    if dd <= -rules["min_drawdown"]:
        out.append(("ok", "", 0.0))
    else:
        gap = (rules["min_drawdown"] - abs(dd)) / rules["min_drawdown"]
        out.append(("fail", f"Caída {_pc(abs(dd), 0)} (pide {_pc(rules['min_drawdown'], 0)})", cap(gap)))

    net = _num(f, "profitMargins")
    lim = rules["min_net_margin"]
    if net is None:
        out.append(("nodata", "margen neto", 0.5))
    elif net < lim:
        out.append(("fail", f"Margen neto {_pc(net)} (pide {_pc(lim, 0)})", cap((lim - net) / lim)))
    else:
        out.append(("ok", "", 0.0))

    if "min_operating_margin" in rules:
        op = _num(f, "operatingMargins")
        lim = rules["min_operating_margin"]
        if op is None:
            out.append(("nodata", "margen operativo", 0.5))
        elif op < lim:
            out.append(("fail", f"Margen operativo {_pc(op)} (pide {_pc(lim, 0)})", cap((lim - op) / lim)))
        else:
            out.append(("ok", "", 0.0))

    if rules.get("require_positive_fcf"):
        fcf = _num(f, "freeCashflow")
        if fcf is None:
            out.append(("nodata", "flujo de caja libre", 0.5))
        elif fcf <= 0:
            out.append(("fail", "Caja libre negativa", 1.0))
        else:
            out.append(("ok", "", 0.0))

    rev = _num(f, "revenueGrowth")
    lim = rules["min_revenue_growth"]
    if rev is not None and rev < lim:
        out.append(("fail", f"Ingresos {_pc(rev, 1, True)} (pide {_pc(lim, 0)})", cap((lim - rev) / abs(lim))))

    if "max_debt_to_equity" in rules:
        de = _num(f, "debtToEquity")
        lim = rules["max_debt_to_equity"]
        if de is not None and de > lim:
            out.append(("fail", f"Deuda/capital {de:.0f}% (máx {lim:.0f}%)", cap((de - lim) / lim)))

    fpe = _num(f, "forwardPE")
    lim = rules["max_forward_pe"]
    if fpe is not None:
        if fpe <= 0:
            out.append(("fail", "PER a futuro negativo", 1.0))
        elif fpe > lim:
            out.append(("fail", f"PER {_n(fpe)} (máx {lim:.0f})", cap((fpe - lim) / lim)))

    if rules["min_rebound_from_20d_low"] > 0 and stat["rebound20"] < rules["min_rebound_from_20d_low"]:
        out.append(("fail", "Aún sin rebote desde mínimos", 0.5))

    return out


def classify(results):
    """Devuelve (tipo, fallos, sin_dato, puntuación). tipo = cumple | no.
    Puntuación: cuanto más baja, más cerca de cumplir."""
    fails = [t for s, t, g in results if s == "fail"]
    nodata = [t for s, t, g in results if s == "nodata"]
    score = sum(g for s, t, g in results if s != "ok") + 0.05 * len(fails)
    return ("cumple" if not fails and not nodata else "no"), fails, nodata, score


def evaluate(stat, f, rules=None):
    """Motivos de fallo en texto (para el log). Lista vacía = cumple."""
    res = checks(stat, f, rules)
    return [t for s, t, g in res if s == "fail"] + [f"sin dato de {t}" for s, t, g in res if s == "nodata"]


# ----------------------------------------------------------------------------
# Extras para los que cumplen: recompras y activo circulante neto por acción
# ----------------------------------------------------------------------------
def _series(df, *names):
    """Fila de un estado financiero de yfinance, de más reciente a más antigua."""
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            s = df.loc[n].dropna()
            if len(s):
                return s.sort_index(ascending=False)
    return None


def _latest(df, *names):
    s = _series(df, *names)
    return float(s.iloc[0]) if s is not None else None


def _fetch_statements(ticker):
    tk = yf.Ticker(ticker)
    out = {}
    for key, attr in (("bs_y", "balance_sheet"), ("bs_q", "quarterly_balance_sheet"), ("cf_y", "cashflow")):
        try:
            out[key] = getattr(tk, attr)
        except Exception:
            out[key] = None
    return out


def extras(t, stat, f):
    """Lista de líneas cortas (recompras y activo circulante neto). [] si no hay nada."""
    try:
        st = _fetch_statements(t)
    except Exception:
        return []
    cur = f.get("currency") or ""
    fcur = f.get("financialCurrency") or cur
    same_cur = (fcur == cur) or not cur
    lines = []

    rep = _series(st["cf_y"], "Repurchase Of Capital Stock", "Common Stock Payments")
    if rep is not None:
        amount = abs(float(rep.iloc[0]))
        if amount > 0:
            txt = f"Recompras: {_m(amount, fcur)}"
            shares = _series(st["bs_y"], "Ordinary Shares Number", "Share Issued")
            if shares is not None and len(shares) >= 2 and float(shares.iloc[1]) > 0:
                chg = float(shares.iloc[0]) / float(shares.iloc[1]) - 1
                txt += " (acciones sin cambio)" if abs(chg) < 0.005 else f" (acciones {_pc(chg, 1, True)})"
            lines.append(txt)

    if not is_financial(f) and same_cur:
        has_q = _series(st["bs_q"], "Current Assets", "Stockholders Equity", "Common Stock Equity") is not None
        bs = st["bs_q"] if has_q else st["bs_y"]
        ca = _latest(bs, "Current Assets", "Total Current Assets")
        tl = _latest(bs, "Total Liabilities Net Minority Interest", "Total Liabilities")
        sh = _latest(bs, "Ordinary Shares Number", "Share Issued") or _num(f, "sharesOutstanding")
        if ca is not None and tl is not None and sh:
            ncav = (ca - tl) / sh
            if ncav > 0:
                lines.append(f"Activo circulante neto: {_n(ncav, 2)}{_sym(cur)} por acción")
    return lines


# ----------------------------------------------------------------------------
# Mensaje
# ----------------------------------------------------------------------------
def _head(r):
    t, s, f = r["t"], r["stat"], r["f"]
    short = html.escape(t.replace(".MC", ""))
    yahoo = f'<a href="https://finance.yahoo.com/quote/{html.escape(t)}">Yahoo</a>'
    name = _name(t, f)
    label = f"<b>{html.escape(name)}</b>" + ("" if name.upper() == t.replace(".MC", "") else f" ({short})")
    return f"• {label} · cae {abs(s['drawdown']) * 100:.0f}% · {yahoo}"


def _cumple_lines(r):
    f = r["f"]
    L = []
    per = _num(f, "trailingPE")
    if per is None or per <= 0:
        per = _num(f, "forwardPE")
    bits = []
    if per and per > 0:
        bits.append(f"PER {_n(per)} (rentabilidad {100 / per:.0f}%)")
    pb = _num(f, "priceToBook")
    if pb is not None:
        bits.append(f"valor contable {_n(pb, 2)}x")
    if bits:
        L.append("↳ " + " · ".join(bits))
    for e in r.get("extra", []):
        L.append("↳ " + e)
    return L


def _near_lines(r):
    motivos = [f"❌ {t}" for t in r["fails"]] + [f"❔ Sin dato de {x}" for x in r["nodata"]]
    if len(motivos) > 3:
        motivos = motivos[:3] + [f"❌ y {len(motivos) - 3} más"]
    return [html.escape(m) for m in motivos]


def _fit(lines, limit=3900):
    """Une las líneas sin pasar del límite de Telegram, cortando siempre entre líneas."""
    out, size = [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            out.append("…")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out)


def build_report(cumple, near, n_review, no_data=0, n_total=0, cumple_more=0):
    cover = f" · {n_total} empresas revisadas" if n_total else ""
    L = [f"📉 <b>Calidad en caída · {date.today():%d/%m}</b>",
         f"<i>Caída desde el máximo de 12 meses{cover}</i>"]
    L += ["", f"✅ <b>CUMPLEN LAS PAUTAS</b>"]
    if cumple:
        for r in cumple:
            L.append(_head(r))
            L += [html.escape(x) for x in _cumple_lines(r)]
        if cumple_more:
            L.append(f"… y {cumple_more} más que cumplen")
    else:
        L.append("Ninguna hoy")
    if near:
        L += ["", f"🟡 <b>NO CUMPLEN, PERO ESTÁN CERCA</b>"]
        for r in near:
            L.append(_head(r))
            L += _near_lines(r)
    if no_data:
        L += ["", f"⚠️ Yahoo no dio datos de {no_data} de {n_review} valores; pueden faltar candidatas."]
    return _fit(L)


def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"[error] Telegram respondió {r.status_code}: {r.text}")
        r.raise_for_status()


# ----------------------------------------------------------------------------
# Historial (solo se usa si REPEAT_ALERTS = False)
# ----------------------------------------------------------------------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def should_alert(ticker, drawdown, state):
    prev = state.get(ticker)
    if not prev:
        return True
    days = (date.today() - date.fromisoformat(prev["date"])).days
    return days >= REALERT_AFTER_DAYS or drawdown <= prev["drawdown"] - REALERT_IF_WORSE_BY


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Calidad en caída -> Telegram")
    ap.add_argument("--dry-run", action="store_true", help="solo imprime, no envía ni guarda")
    ap.add_argument("--test", action="store_true", help="envía un mensaje de prueba")
    ap.add_argument("--tickers", default="", help="lista separada por comas (por defecto: valores de España)")
    args = ap.parse_args()

    if args.test:
        send("✅ Bot conectado. Aquí recibirás los avisos de calidad en caída.")
        print("Mensaje de prueba enviado.")
        return

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or get_universe()
    print(f"Analizando {len(tickers)} valores...")

    stats = price_stats(tickers)
    missing = [t for t in tickers if t not in stats]
    if missing:
        print(f"[aviso] sin datos de precio para {len(missing)}: {', '.join(missing)}")

    fallen = sorted(
        ((t, s) for t, s in stats.items() if s["drawdown"] <= -REVIEW_MIN_DRAWDOWN),
        key=lambda ts: ts[1]["drawdown"],
    )
    review = fallen[:MAX_REVIEW]
    print(f"{len(fallen)} con caída >= {REVIEW_MIN_DRAWDOWN:.0%}; reviso las {len(review)} que más han caído")

    rows, no_data = [], 0
    for t, s in review:
        f = fundamentals(t)
        missing_f = fund_missing(f)
        if missing_f:
            no_data += 1
        kind, fails, nodata, score = classify(checks(s, f))
        detail = "; ".join(fails + [f"sin dato de {x}" for x in nodata])
        mark = "✓" if kind == "cumple" else "✗"
        print(f"  {mark} {t} ({score:.2f}): {detail or 'pasa todas las pautas'}")
        rows.append({"kind": kind, "t": t, "stat": s, "f": f, "fails": fails, "nodata": nodata,
                     "score": score, "missing": missing_f, "extra": []})
        if no_data >= 8 and no_data == len(rows):
            print("[aviso] Yahoo no da datos de las 8 primeras: paro la revisión para no perder tiempo")
            break
        time.sleep(0.4)

    state = load_state()
    cumple = [r for r in rows if r["kind"] == "cumple"]
    if not REPEAT_ALERTS and not args.dry_run:
        cumple = [r for r in cumple if should_alert(r["t"], r["stat"]["drawdown"], state)]
    cumple.sort(key=lambda r: r["stat"]["drawdown"])
    cumple_more = max(0, len(cumple) - MAX_CUMPLE)
    cumple = cumple[:MAX_CUMPLE]
    near = sorted((r for r in rows if r["kind"] == "no" and not r["missing"]), key=lambda r: r["score"])[:TOP_NEAR]

    if SHOW_EXTRA:
        for r in cumple:
            r["extra"] = extras(r["t"], r["stat"], r["f"])

    warn = no_data if (rows and no_data * 2 >= len(rows)) else 0
    text = build_report(cumple, near, len(rows), warn, len(stats), cumple_more)

    if args.dry_run:
        print("\n" + text)
        return

    send(text)
    for r in cumple:
        state[r["t"]] = {"date": date.today().isoformat(), "drawdown": r["stat"]["drawdown"]}
    save_state(state)


if __name__ == "__main__":
    main()
