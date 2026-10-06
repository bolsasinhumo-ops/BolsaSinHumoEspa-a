#!/usr/bin/env python3
"""
Bot de Telegram: "calidad en caída" (valores de España).

Cada ejecución manda UN mensaje con:
  ✅ CUMPLE -> pasan todas las pautas
  🟡 CASI   -> falla una sola pauta (o la caída está entre NEAR_DRAWDOWN y min_drawdown)

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

NEAR_DRAWDOWN = 0.25     # desde esta caída una empresa ya se revisa (puede salir en CASI)
MAX_FAILS_NEAR = 1       # CASI = falla como mucho este número de pautas (1 o 2)
MAX_ITEMS = 15           # máximo de empresas en el mensaje
SHOW_EXTRA = True        # en CUMPLE: línea extra con recompras y activo circulante neto
REPEAT_ALERTS = True     # True = avisa cada ejecución aunque ya la hubiera avisado antes
REALERT_AFTER_DAYS = 30  # (solo si REPEAT_ALERTS = False) repite el aviso pasados estos días...
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
]

# Valores extra que quieras añadir (mismo formato). Ejemplo: "CAF.MC"
EXTRA_TICKERS = []

CNMV_DIRECTIVOS_URL = "https://www.cnmv.es/Portal/Consultas/Directivos-Consulta.aspx"
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


def _short(name, n=26):
    name = str(name).strip()
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


# ----------------------------------------------------------------------------
# Criterios
# ----------------------------------------------------------------------------
def checks(stat, f, rules=None):
    """Lista de (estado, texto): estado = ok | fail | nodata. El texto explica el fallo."""
    if rules is None:
        rules = RULES_FINANCIALS if is_financial(f) else RULES
    out = []

    dd = stat["drawdown"]
    if dd <= -rules["min_drawdown"]:
        out.append(("ok", ""))
    else:
        out.append(("fail", f"caída {_pc(abs(dd), 0)} (mín {_pc(rules['min_drawdown'], 0)})"))

    net = _num(f, "profitMargins")
    if net is None:
        out.append(("nodata", "margen neto"))
    elif net < rules["min_net_margin"]:
        out.append(("fail", f"margen neto {_pc(net)} (mín {_pc(rules['min_net_margin'], 0)})"))
    else:
        out.append(("ok", ""))

    if "min_operating_margin" in rules:
        op = _num(f, "operatingMargins")
        if op is None:
            out.append(("nodata", "margen operativo"))
        elif op < rules["min_operating_margin"]:
            out.append(("fail", f"margen operativo {_pc(op)} (mín {_pc(rules['min_operating_margin'], 0)})"))
        else:
            out.append(("ok", ""))

    if rules.get("require_positive_fcf"):
        fcf = _num(f, "freeCashflow")
        if fcf is None:
            out.append(("nodata", "flujo de caja libre"))
        elif fcf <= 0:
            out.append(("fail", "flujo de caja libre negativo"))
        else:
            out.append(("ok", ""))

    rev = _num(f, "revenueGrowth")
    if rev is not None and rev < rules["min_revenue_growth"]:
        out.append(("fail", f"ingresos {_pc(rev, 1, True)} (mín {_pc(rules['min_revenue_growth'], 0)})"))

    if "max_debt_to_equity" in rules:
        de = _num(f, "debtToEquity")
        if de is not None and de > rules["max_debt_to_equity"]:
            out.append(("fail", f"deuda/capital {de:.0f}% (máx {rules['max_debt_to_equity']:.0f}%)"))

    fpe = _num(f, "forwardPE")
    if fpe is not None:
        if fpe <= 0:
            out.append(("fail", "PER a futuro negativo"))
        elif fpe > rules["max_forward_pe"]:
            out.append(("fail", f"PER {_n(fpe)} (máx {rules['max_forward_pe']:.0f})"))

    if rules["min_rebound_from_20d_low"] > 0 and stat["rebound20"] < rules["min_rebound_from_20d_low"]:
        out.append(("fail", "aún sin rebote desde mínimos"))

    return out


def classify(results):
    """Devuelve (tipo, fallos, sin_dato). tipo = cumple | casi | no."""
    fails = [t for s, t in results if s == "fail"]
    nodata = [t for s, t in results if s == "nodata"]
    if not fails and not nodata:
        return "cumple", fails, nodata
    if len(fails) + len(nodata) <= MAX_FAILS_NEAR:
        return "casi", fails, nodata
    return "no", fails, nodata


def evaluate(stat, f, rules=None):
    """Motivos de fallo en texto (para el log). Lista vacía = cumple."""
    res = checks(stat, f, rules)
    return [t for s, t in res if s == "fail"] + [f"sin dato de {t}" for s, t in res if s == "nodata"]


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
    """Texto corto con recompras y NCAV (activo circulante neto por acción). '' si no hay nada."""
    try:
        st = _fetch_statements(t)
    except Exception:
        return ""
    cur = f.get("currency") or ""
    fcur = f.get("financialCurrency") or cur
    same_cur = (fcur == cur) or not cur
    bits = []

    rep = _series(st["cf_y"], "Repurchase Of Capital Stock", "Common Stock Payments")
    if rep is not None:
        amount = abs(float(rep.iloc[0]))
        if amount > 0:
            txt = f"recompras {_m(amount, fcur)}"
            shares = _series(st["bs_y"], "Ordinary Shares Number", "Share Issued")
            if shares is not None and len(shares) >= 2 and float(shares.iloc[1]) > 0:
                chg = float(shares.iloc[0]) / float(shares.iloc[1]) - 1
                txt += f" (acciones {_pc(chg, 1, True)})"
            bits.append(txt)

    if not is_financial(f) and same_cur:
        has_q = _series(st["bs_q"], "Current Assets", "Stockholders Equity", "Common Stock Equity") is not None
        bs = st["bs_q"] if has_q else st["bs_y"]
        ca = _latest(bs, "Current Assets", "Total Current Assets")
        tl = _latest(bs, "Total Liabilities Net Minority Interest", "Total Liabilities")
        sh = _latest(bs, "Ordinary Shares Number", "Share Issued") or _num(f, "sharesOutstanding")
        if ca is not None and tl is not None and sh:
            ncav = (ca - tl) / sh
            if ncav > 0:
                bits.append(f"NCAV {_n(ncav, 2)}{_sym(cur)} (precio {_n(stat['last'] / ncav)}x)")
    return " · ".join(bits)


# ----------------------------------------------------------------------------
# Mensaje
# ----------------------------------------------------------------------------
def _line(r):
    t, s, f = r["t"], r["stat"], r["f"]
    name = html.escape(_short(f.get("name") or t))
    yahoo = f'<a href="https://finance.yahoo.com/quote/{html.escape(t)}">Yahoo</a>'
    head = f"• <b>{name}</b> ({html.escape(t)}) −{abs(s['drawdown']) * 100:.0f}%"
    if r["kind"] == "cumple":
        bits = []
        per = _num(f, "trailingPE")
        if per is None or per <= 0:
            per = _num(f, "forwardPE")
        if per and per > 0:
            bits += [f"PER {_n(per)}", f"rent. {100 / per:.0f}%"]
        pb = _num(f, "priceToBook")
        if pb is not None:
            bits.append(f"P/B {_n(pb, 2)}")
        tail = " · ".join(bits)
    else:
        motivos = r["fails"] + [f"sin dato de {x}" for x in r["nodata"]]
        tail = html.escape("falla: " + "; ".join(motivos))
    return f"{head} · {tail} · {yahoo}" if tail else f"{head} · {yahoo}"


def build_report(rows, n_cand, no_data=0):
    cumple = [r for r in rows if r["kind"] == "cumple"]
    casi = [r for r in rows if r["kind"] == "casi"]
    L = [f"📉 <b>{date.today():%d/%m} · Calidad en caída</b>"]
    if cumple:
        L += ["", "✅ <b>CUMPLE</b>"]
        for r in cumple:
            L.append(_line(r))
            if r.get("extra"):
                L.append("   ↳ " + html.escape(r["extra"]))
    if casi:
        L += ["", "🟡 <b>CASI</b>"]
        for r in casi:
            L.append(_line(r))
    if not rows:
        L += ["", f"Sin candidatas hoy ({n_cand} con caída ≥{NEAR_DRAWDOWN:.0%})"]
    if cumple:
        L += ["", f'<a href="{CNMV_DIRECTIVOS_URL}">Directivos en la CNMV</a>']
    if no_data:
        L += ["", f"⚠️ Yahoo no dio datos de {no_data} de {n_cand} valores; pueden faltar candidatas."]
    return "\n".join(L)[:4000]


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
    candidates = {t: s for t, s in stats.items() if s["drawdown"] <= -NEAR_DRAWDOWN}
    print(f"{len(candidates)} con caída >= {NEAR_DRAWDOWN:.0%}")

    icon = {"cumple": "✓", "casi": "~", "no": "✗"}
    rows, no_data = [], 0
    for t, s in sorted(candidates.items(), key=lambda kv: kv[1]["drawdown"]):
        f = fundamentals(t)
        if fund_missing(f):
            no_data += 1
        kind, fails, nodata = classify(checks(s, f))
        detail = "; ".join(fails + [f"sin dato de {x}" for x in nodata])
        print(f"  {icon[kind]} {t}: {detail or 'pasa todas las pautas'}")
        if kind != "no":
            rows.append({"kind": kind, "t": t, "stat": s, "f": f, "fails": fails, "nodata": nodata, "extra": ""})
        time.sleep(0.4)

    state = load_state()
    if not REPEAT_ALERTS and not args.dry_run:
        rows = [r for r in rows if should_alert(r["t"], r["stat"]["drawdown"], state)]
    rows.sort(key=lambda r: (r["kind"] != "cumple", len(r["fails"]) + len(r["nodata"]), r["stat"]["drawdown"]))
    rows = rows[:MAX_ITEMS]

    if SHOW_EXTRA:
        for r in rows:
            if r["kind"] == "cumple":
                r["extra"] = extras(r["t"], r["stat"], r["f"])

    warn = no_data if (candidates and no_data * 2 >= len(candidates)) else 0
    text = build_report(rows, len(candidates), warn)

    if args.dry_run:
        print("\n" + text)
        return

    send(text)
    for r in rows:
        state[r["t"]] = {"date": date.today().isoformat(), "drawdown": r["stat"]["drawdown"]}
    save_state(state)


if __name__ == "__main__":
    main()
