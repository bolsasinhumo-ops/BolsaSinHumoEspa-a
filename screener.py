#!/usr/bin/env python3
"""
Bot de Telegram: "calidad en caída".

Busca empresas que han caído mucho desde su máximo de 52 semanas pero siguen
siendo rentables y con balance sano (el patrón tipo Accenture) y te las manda
por Telegram.

Uso:
  python screener.py                              # ejecución normal (envía a Telegram)
  python screener.py --dry-run                    # no envía ni guarda nada, solo imprime
  python screener.py --test                       # manda un mensaje de prueba
  python screener.py --tickers ITX.MC,IDR.MC --dry-run  # evalúa solo esos valores

Variables de entorno necesarias para enviar:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import html
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# ----------------------------------------------------------------------------
# PAUTAS (aquí es donde afinas lo que quieres que te avise)
# ----------------------------------------------------------------------------
RULES = {
    "min_drawdown": 0.30,             # caída mínima desde el máximo de 52 semanas (30%)
    "min_net_margin": 0.05,           # margen neto mínimo: sigue ganando dinero
    "min_operating_margin": 0.10,     # margen operativo mínimo
    "min_revenue_growth": -0.05,      # los ingresos no se hunden (>= -5% interanual)
    "max_debt_to_equity": 150.0,      # deuda/capital máxima (en %, formato Yahoo)
    "max_forward_pe": 25.0,           # PER a futuro máximo: no está cara aunque haya caído
    "require_positive_fcf": True,     # exige flujo de caja libre positivo
    "min_rebound_from_20d_low": 0.0,  # 0 = desactivado. Ej: 0.03 exige estar un 3% por encima del mínimo de 20 días
}

# Bancos y aseguradoras (sector "Financial Services" en Yahoo): el margen operativo,
# el flujo de caja libre y la deuda no se miden igual en estas empresas, así que se
# evalúan solo con beneficio neto positivo y PER bajo.
RULES_FINANCIALS = {
    "min_drawdown": RULES["min_drawdown"],
    "min_net_margin": 0.03,           # sigue ganando dinero
    "min_revenue_growth": -0.05,
    "max_forward_pe": 12.0,           # PER a futuro bajo (lo normal en banca/seguros es 6-12)
    "min_rebound_from_20d_low": RULES["min_rebound_from_20d_low"],
}

MAX_ALERTS_PER_RUN = 8       # máximo de avisos por ejecución
REALERT_AFTER_DAYS = 0      # vuelve a avisar de la misma empresa pasados estos días...
REALERT_IF_WORSE_BY = 0.10   # ...o si ha caído 10 puntos más desde el último aviso

# Universo: valores españoles en Yahoo Finance (sufijo .MC = Bolsa de Madrid).
# IBEX 35 + una selección de medianas y pequeñas. La lista es de memoria: si algún
# valor ya no cotiza o cambió de ticker, el log lo marcará como "sin datos".
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
]

# Valores extra que quieras añadir (mismo formato). Ejemplo: "CAF.MC"
EXTRA_TICKERS = []

STATE_FILE = Path(__file__).with_name("state.json")


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
    """Etapa 2 (más lenta): fundamentales, solo para las que ya han caído mucho."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        print(f"[aviso] sin fundamentales de {ticker}: {e}")
        return {}
    keys = [
        "marketCap", "profitMargins", "operatingMargins", "revenueGrowth",
        "freeCashflow", "debtToEquity", "forwardPE", "trailingPE",
        "returnOnEquity", "earningsGrowth", "totalCash", "totalDebt",
        "sector", "industry", "currency", "financialCurrency",
        "priceToBook", "bookValue", "sharesOutstanding",
    ]
    out = {k: info.get(k) for k in keys}
    out["name"] = info.get("shortName") or info.get("longName") or ticker
    return out


# ----------------------------------------------------------------------------
# Criterios
# ----------------------------------------------------------------------------
def _num(f, key):
    v = f.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def is_financial(f):
    return f.get("sector") == "Financial Services"


def evaluate(stat, f, rules=None):
    """Devuelve la lista de motivos por los que NO pasa. Lista vacía = pasa."""
    if rules is None:
        rules = RULES_FINANCIALS if is_financial(f) else RULES
    fails = []

    if stat["drawdown"] > -rules["min_drawdown"]:
        fails.append(f"caída {stat['drawdown']:.0%} insuficiente")

    net = _num(f, "profitMargins")
    if net is None:
        fails.append("sin dato de margen neto")
    elif net < rules["min_net_margin"]:
        fails.append(f"margen neto {net:.1%} bajo")

    if "min_operating_margin" in rules:
        op = _num(f, "operatingMargins")
        if op is None:
            fails.append("sin dato de margen operativo")
        elif op < rules["min_operating_margin"]:
            fails.append(f"margen operativo {op:.1%} bajo")

    if rules.get("require_positive_fcf"):
        fcf = _num(f, "freeCashflow")
        if fcf is None:
            fails.append("sin dato de flujo de caja libre")
        elif fcf <= 0:
            fails.append("flujo de caja libre negativo")

    rev = _num(f, "revenueGrowth")
    if rev is not None and rev < rules["min_revenue_growth"]:
        fails.append(f"ingresos cayendo {rev:.1%}")

    if "max_debt_to_equity" in rules:
        de = _num(f, "debtToEquity")
        if de is not None and de > rules["max_debt_to_equity"]:
            fails.append(f"deuda/capital {de:.0f} alta")

    fpe = _num(f, "forwardPE")
    if fpe is not None and (fpe <= 0 or fpe > rules["max_forward_pe"]):
        fails.append(f"PER a futuro {fpe:.1f} fuera de rango")

    if rules["min_rebound_from_20d_low"] > 0 and stat["rebound20"] < rules["min_rebound_from_20d_low"]:
        fails.append("aún sin rebote desde mínimos")

    return fails


# ----------------------------------------------------------------------------
# Mensajes
# ----------------------------------------------------------------------------
def pct(x, signed=True):
    if x is None:
        return "n/d"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def money(v, cur=""):
    if v is None:
        return "n/d"
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B {cur}".strip()
    return f"{v / 1e6:.0f}M {cur}".strip()


def build_message(t, stat, f):
    cur = f.get("currency") or ""
    name = html.escape(str(f.get("name") or t))
    sector = html.escape(str(f.get("sector") or "n/d"))
    lines = [
        f"📉 <b>{html.escape(t)}</b> · {name}",
        f"Cae <b>{stat['drawdown'] * 100:.1f}%</b> desde su máximo de 52 semanas "
        f"(precio {stat['last']:.2f} {cur})",
        f"Sector: {sector}" + (f" · Capitalización: {money(_num(f, 'marketCap'), cur)}" if _num(f, "marketCap") else ""),
        "",
    ]
    fin = is_financial(f)
    if fin:
        lines.append(f"✅ Margen neto {pct(_num(f, 'profitMargins'), False)} (banca/seguros: criterios adaptados)")
    else:
        lines.append(f"✅ Margen neto {pct(_num(f, 'profitMargins'), False)} · operativo {pct(_num(f, 'operatingMargins'), False)}")
        lines.append(f"✅ Flujo de caja libre: {money(_num(f, 'freeCashflow'), cur)}")
        de = _num(f, "debtToEquity")
        if de is not None:
            lines.append(f"✅ Deuda/capital: {de:.0f}%")
    fpe, tpe = _num(f, "forwardPE"), _num(f, "trailingPE")
    if fpe is not None:
        extra = f" (actual {tpe:.1f})" if tpe else ""
        lines.append(f"✅ PER a futuro: {fpe:.1f}{extra}")
    rev = _num(f, "revenueGrowth")
    if rev is not None:
        lines.append(f"📈 Ingresos interanual: {pct(rev)}")

    bonus = []
    roe = _num(f, "returnOnEquity")
    if roe is not None and roe >= 0.15:
        bonus.append(f"ROE {roe:.0%}")
    eg = _num(f, "earningsGrowth")
    if eg is not None and eg > 0:
        bonus.append(f"beneficios creciendo {eg:.0%}")
    if fpe is not None and tpe is not None and fpe < tpe:
        bonus.append("beneficios esperados al alza")
    cash, debt = _num(f, "totalCash"), _num(f, "totalDebt")
    if not fin and cash is not None and debt is not None and cash > debt:
        bonus.append("caja neta")
    if bonus:
        lines.append("⭐ " + " · ".join(bonus))

    lines.append(f"Rebote desde mínimo de 20 días: {pct(stat['rebound20'])}")
    lines.append(f'<a href="https://finance.yahoo.com/quote/{html.escape(t)}">Ver en Yahoo Finance</a>')
    return "\n".join(lines)


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
    r.raise_for_status()


# ----------------------------------------------------------------------------
# Análisis de la acción (los 5 puntos), se añade debajo de cada aviso
# ----------------------------------------------------------------------------
CNMV_DIRECTIVOS_URL = "https://www.cnmv.es/Portal/Consultas/Directivos-Consulta.aspx"


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
    """Balance (anual y trimestral) y flujos de caja. Cada uno puede venir vacío."""
    tk = yf.Ticker(ticker)
    out = {}
    for key, attr in (("bs_y", "balance_sheet"), ("bs_q", "quarterly_balance_sheet"), ("cf_y", "cashflow")):
        try:
            out[key] = getattr(tk, attr)
        except Exception:
            out[key] = None
    return out


def per_threshold(f):
    """Umbral de PER según el sector (pautas del usuario)."""
    sector = f.get("sector") or ""
    industry = (f.get("industry") or "").lower()
    if sector == "Energy" or any(k in industry for k in ("auto", "airline", "oil & gas")):
        return 5, "cíclicas (autos, petróleo, aerolíneas)"
    if sector == "Technology" or any(k in industry for k in ("software", "information technology", "consulting")):
        return 20, "consultoría, software y tecnología"
    return 10, "resto de sectores"


def analysis_block(t, stat, f):
    cur = f.get("currency") or ""
    fcur = f.get("financialCurrency") or cur
    same_cur = (fcur == cur) or not cur
    fin = is_financial(f)
    try:
        st = _fetch_statements(t)
    except Exception:
        st = {"bs_y": None, "bs_q": None, "cf_y": None}
    bs = st["bs_q"] if _series(st["bs_q"], "Current Assets", "Stockholders Equity", "Common Stock Equity") is not None else st["bs_y"]
    L = ["🔬 <b>Análisis</b>"]

    # 1) PER y contexto sectorial
    tpe, fpe = _num(f, "trailingPE"), _num(f, "forwardPE")
    thr, thr_name = per_threshold(f)
    if tpe is None or tpe <= 0:
        per_txt = "sin PER válido (beneficio nulo o negativo)"
    else:
        rel = "por debajo" if tpe < thr else "por encima"
        per_txt = f"{tpe:.1f}" + (f" (a futuro {fpe:.1f})" if fpe else "") + f" · {rel} del umbral {thr} para {thr_name}"
    L.append(f"1) PER: {per_txt}")

    # 2) Rentabilidad implícita = 1/PER
    if tpe is not None and tpe > 0:
        imp = f"{100 / tpe:.1f}% anual"
        if fpe is not None and fpe > 0:
            imp += f" (con PER a futuro: {100 / fpe:.1f}%)"
        L.append(f"2) Rentabilidad implícita (1/PER): {imp}")
    else:
        L.append("2) Rentabilidad implícita: n/d")

    # 3) Valor contable frente a capitalización
    pb = _num(f, "priceToBook")
    equity = _latest(bs, "Stockholders Equity", "Common Stock Equity")
    mcap = _num(f, "marketCap")
    if pb is None and equity and mcap and equity > 0 and same_cur:
        pb = mcap / equity
    if pb is None:
        L.append("3) Valor contable: n/d")
    else:
        rel = "por debajo del patrimonio neto" if pb < 1 else "por encima del patrimonio neto"
        eq_txt = f" · patrimonio neto {money(equity, fcur)}" if equity and equity > 0 else ""
        L.append(f"3) Valor contable: P/B {pb:.2f} → capitalización {rel}{eq_txt}")

    # 4) Recompras y directivos
    rep = _series(st["cf_y"], "Repurchase Of Capital Stock", "Common Stock Payments")
    bits = []
    if rep is not None:
        amount = abs(float(rep.iloc[0]))
        year = getattr(rep.index[0], "year", "")
        bits.append(f"recompras ej. {year}: {money(amount, fcur)}" if amount > 0 else f"sin recompras en el ej. {year}")
    shares = _series(st["bs_y"], "Ordinary Shares Number", "Share Issued")
    if shares is not None and len(shares) >= 2 and float(shares.iloc[1]) > 0:
        chg = float(shares.iloc[0]) / float(shares.iloc[1]) - 1
        bits.append(f"acciones en circulación {pct(chg)} vs año anterior")
    txt4 = " · ".join(bits) if bits else "recompras: n/d"
    L.append("4) " + txt4[0].upper() + txt4[1:])
    if t.endswith(".MC"):
        name = html.escape(str(f.get("name") or t))
        L.append(
            f'    Directivos: <a href="{CNMV_DIRECTIVOS_URL}">consulta en la CNMV</a> '
            f"(busca «{name}»); no hay descarga automática"
        )

    # 5) Activo circulante neto por acción (NCAV, estilo Graham)
    if fin:
        L.append("5) Activo circulante neto/acción: no aplica a banca/seguros")
    elif not same_cur:
        L.append(f"5) Activo circulante neto/acción: no comparable (cuentas en {fcur}, cotiza en {cur})")
    else:
        ca = _latest(bs, "Current Assets", "Total Current Assets")
        tl = _latest(bs, "Total Liabilities Net Minority Interest", "Total Liabilities")
        sh = _latest(bs, "Ordinary Shares Number", "Share Issued") or _num(f, "sharesOutstanding")
        if ca is None or tl is None or not sh:
            L.append("5) Activo circulante neto/acción: n/d")
        else:
            ncav = (ca - tl) / sh
            if ncav <= 0:
                L.append(f"5) Activo circulante neto/acción: {ncav:.2f} {cur} (negativo: los pasivos superan al activo circulante)")
            else:
                L.append(
                    f"5) Activo circulante neto/acción: {ncav:.2f} {cur} · precio {stat['last']:.2f} {cur} "
                    f"({stat['last'] / ncav:.2f}x)"
                )
    return "\n".join(L)


def full_message(t, stat, f):
    try:
        extra = analysis_block(t, stat, f)
    except Exception as e:
        print(f"[aviso] análisis de {t} falló: {e}")
        extra = ""
    text = build_message(t, stat, f) + ("\n\n" + extra if extra else "")
    return text[:4000]


# ----------------------------------------------------------------------------
# Historial (para no repetir avisos)
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
    candidates = {t: s for t, s in stats.items() if s["drawdown"] <= -RULES["min_drawdown"]}
    print(f"{len(candidates)} con caída >= {RULES['min_drawdown']:.0%}")

    hits = []
    for t, s in sorted(candidates.items(), key=lambda kv: kv[1]["drawdown"]):
        f = fundamentals(t)
        fails = evaluate(s, f)
        if fails:
            print(f"  ✗ {t}: " + "; ".join(fails))
        else:
            print(f"  ✓ {t}: pasa todas las pautas")
            hits.append((t, s, f))
        time.sleep(0.4)

    state = load_state()
    to_send = hits if args.dry_run else [h for h in hits if should_alert(h[0], h[1]["drawdown"], state)]
    to_send = to_send[:MAX_ALERTS_PER_RUN]

    if to_send:
        header = f"🔎 {len(to_send)} candidata(s) de calidad en caída"
        if args.dry_run:
            print("\n" + header)
            for t, s, f in to_send:
                print("\n" + full_message(t, s, f))
        else:
            send(header)
            for t, s, f in to_send:
                send(full_message(t, s, f))
                state[t] = {"date": date.today().isoformat(), "drawdown": s["drawdown"]}
                time.sleep(0.5)
    else:
        print("Sin novedades hoy.")

    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
