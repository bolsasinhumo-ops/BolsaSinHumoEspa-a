#!/usr/bin/env python3
"""
Bot de Telegram: "calidad en caída" (valores de España).

Cada ejecución manda UN mensaje con:
  ✅ CUMPLEN -> pasan todas las pautas
  🟡 LAS 5 MÁS CERCA -> no cumplen, pero son las que menos les falta

Uso:
  python screener.py                                  # normal: mercado según el día (lun España, mar Europa, mié EE. UU.)
  python screener.py --market us                      # fuerza un mercado: es, eu o us
  python screener.py --dry-run                        # solo imprime, no envía ni guarda
  python screener.py --test                           # manda un mensaje de prueba
  python screener.py --tickers ITX.MC,IDR.MC --dry-run

Variables de entorno necesarias para enviar:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import html
import io
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
# Ajustes por mercado. En cada uno solo se revisan los fundamentales de las empresas que han
# caído al menos "review_min_drawdown", y como mucho las "max_review" que más han caído.
MARKETS = {
    "es": {"name": "España", "review_min_drawdown": 0.10, "max_review": 80},
    "eu": {"name": "Europa", "review_min_drawdown": 0.20, "max_review": 100},
    "us": {"name": "EE. UU.", "review_min_drawdown": 0.25, "max_review": 150},
}
# Qué mercado se ejecuta cada día (0 = lunes ... 6 = domingo; hora UTC). Máximo el miércoles.
AUTO_SCHEDULE = {0: "es", 1: "eu", 2: "us"}
MAX_CUMPLE = 10             # máximo de empresas que cumplen en el mensaje. Si hay 10 o más que cumplen,
                            # se muestran solo 10 y NO se listan las que no cumplen ni se dice cuántas hay más
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

# Valores extra que quieras añadir a cada mercado (mismo formato de Yahoo). Ejemplo: "CAF.MC"
EXTRA_TICKERS = []      # España
EXTRA_TICKERS_EU = []   # Europa
EXTRA_TICKERS_US = []   # EE. UU. (además del S&P 500, que se baja de Wikipedia al ejecutarse)

# Plan B de EE. UU.: si no se puede bajar el S&P 500 de Wikipedia, se revisa esta lista corta.
US_FALLBACK = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL", "CRM", "ADBE", "ACN", "IBM",
    "INTC", "AMD", "QCOM", "TXN", "CSCO", "NOW", "INTU", "AMAT", "MU", "PYPL", "EBAY", "NFLX", "DIS",
    "CMCSA", "T", "VZ", "TMUS", "CHTR", "WBD", "PARA", "NKE", "SBUX", "MCD", "HD", "LOW", "TGT", "WMT",
    "COST", "DG", "DLTR", "KR", "ROST", "TJX", "LULU", "EL", "PG", "KO", "PEP", "PM", "MO", "CL", "KMB",
    "GIS", "KHC", "MDLZ", "HSY", "CPB", "SJM", "JNJ", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD",
    "BIIB", "REGN", "VRTX", "MRNA", "UNH", "CVS", "CI", "HUM", "ELV", "CNC", "MDT", "ABT", "TMO", "DHR",
    "ISRG", "SYK", "BSX", "EW", "ZBH", "BAX", "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "AXP",
    "COF", "USB", "PNC", "TFC", "MET", "PRU", "AIG", "ALL", "TRV", "CB", "V", "MA", "XOM", "CVX", "COP",
    "OXY", "SLB", "EOG", "MPC", "VLO", "PSX", "KMI", "WMB", "CAT", "DE", "BA", "LMT", "RTX", "GE", "HON",
    "MMM", "UPS", "FDX", "UNP", "CSX", "NSC", "LUV", "DAL", "UAL", "F", "GM", "DOW", "DD", "LYB", "NEM",
    "FCX", "NUE", "NEE", "DUK", "SO", "D", "AEP", "EXC", "PLD", "AMT", "CCI", "SPG", "O", "PSA", "WELL",
    "EQIX", "KVUE", "ZTS", "TEAM", "WDAY", "ADSK", "FTNT", "PANW", "CRWD", "SNPS", "CDNS", "KLAC", "LRCX",
    "ADI", "NXPI", "ON", "MCHP", "SWKS", "QRVO", "EPAM", "CTSH", "GPN", "FIS", "FI", "ADP", "PAYX",
]

# Europa (sin España): principales valores de Francia, Alemania, Países Bajos, Italia, Bélgica,
# Finlandia, Suecia, Dinamarca, Noruega, Suiza, Reino Unido, Irlanda, Portugal y Austria.
# Lista de memoria: lo que Yahoo no reconozca saldrá en el log como "sin datos de precio".
EUROPE_TICKERS = [
    # Francia
    "AIR.PA", "MC.PA", "OR.PA", "RMS.PA", "KER.PA", "TTE.PA", "SAN.PA", "AI.PA", "BNP.PA", "GLE.PA",
    "ACA.PA", "CS.PA", "SU.PA", "SAF.PA", "DG.PA", "EL.PA", "CAP.PA", "BN.PA", "RI.PA", "STLAP.PA",
    "ENGI.PA", "ORA.PA", "PUB.PA", "DSY.PA", "STMPA.PA", "HO.PA", "SGO.PA", "ML.PA", "RNO.PA",
    "VIE.PA", "LR.PA", "TEP.PA", "EDEN.PA", "SW.PA", "CA.PA", "BVI.PA", "AC.PA", "ALO.PA", "AM.PA",
    "FGR.PA", "VIV.PA",
    # Alemania
    "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "MUV2.DE", "BAS.DE", "BAYN.DE", "BMW.DE", "MBG.DE",
    "VOW3.DE", "PAH3.DE", "P911.DE", "DBK.DE", "CBK.DE", "ADS.DE", "IFX.DE", "DHL.DE", "RWE.DE",
    "EOAN.DE", "HEN3.DE", "BEI.DE", "MRK.DE", "FRE.DE", "FME.DE", "SHL.DE", "ZAL.DE", "HEI.DE",
    "CON.DE", "DB1.DE", "SY1.DE", "QIA.DE", "HNR1.DE", "LHA.DE", "VNA.DE", "PUM.DE", "BOSS.DE",
    "TKA.DE", "ENR.DE", "HFG.DE", "RHM.DE", "MTX.DE", "LIN.DE",
    # Países Bajos
    "ASML.AS", "INGA.AS", "AD.AS", "PHIA.AS", "HEIA.AS", "PRX.AS", "WKL.AS", "AKZA.AS", "DSFIR.AS",
    "ADYEN.AS", "RAND.AS", "NN.AS", "ASM.AS", "BESI.AS", "IMCD.AS", "UMG.AS", "ABN.AS",
    # Italia
    "ENEL.MI", "ENI.MI", "ISP.MI", "UCG.MI", "G.MI", "RACE.MI", "TIT.MI", "PRY.MI", "BMPS.MI",
    "BPE.MI", "MONC.MI", "LDO.MI", "SRG.MI", "TRN.MI", "AMP.MI", "DIA.MI", "REC.MI", "PIRC.MI",
    "BAMI.MI", "A2A.MI", "HER.MI", "IG.MI", "NEXI.MI", "INW.MI", "BC.MI",
    # Bélgica
    "ABI.BR", "KBC.BR", "UCB.BR", "SOLB.BR", "UMI.BR", "ACKB.BR", "GBLB.BR", "AGS.BR", "ARGX.BR",
    # Finlandia
    "NOKIA.HE", "NESTE.HE", "SAMPO.HE", "KNEBV.HE", "UPM.HE", "STERV.HE", "FORTUM.HE", "ELISA.HE",
    "WRT1V.HE", "ORNBV.HE", "NDA-FI.HE",
    # Suecia
    "VOLV-B.ST", "ERIC-B.ST", "ATCO-A.ST", "ASSA-B.ST", "SEB-A.ST", "SWED-A.ST", "HM-B.ST",
    "INVE-B.ST", "SAND.ST", "ESSITY-B.ST", "SHB-A.ST", "TELIA.ST", "ALFA.ST", "EVO.ST", "HEXA-B.ST",
    "NIBE-B.ST", "SKF-B.ST", "EQT.ST", "BOL.ST",
    # Dinamarca
    "NOVO-B.CO", "DSV.CO", "MAERSK-B.CO", "VWS.CO", "ORSTED.CO", "CARL-B.CO", "PNDORA.CO",
    "COLO-B.CO", "DANSKE.CO", "GMAB.CO", "TRYG.CO", "NSIS-B.CO", "DEMANT.CO", "GN.CO", "ISS.CO",
    # Noruega
    "EQNR.OL", "DNB.OL", "MOWI.OL", "TEL.OL", "YAR.OL", "ORK.OL", "SALM.OL", "AKRBP.OL", "NHY.OL",
    "STB.OL",
    # Suiza
    "NESN.SW", "NOVN.SW", "ROG.SW", "UBSG.SW", "ZURN.SW", "ABBN.SW", "CFR.SW", "SREN.SW", "GIVN.SW",
    "LONN.SW", "SIKA.SW", "ALC.SW", "HOLN.SW", "GEBN.SW", "SLHN.SW", "PGHN.SW", "SCMN.SW", "LOGN.SW",
    "KNIN.SW", "UHR.SW", "SOON.SW", "BAER.SW", "STMN.SW", "TEMN.SW", "SGSN.SW", "CLN.SW",
    # Reino Unido
    "SHEL.L", "AZN.L", "HSBA.L", "ULVR.L", "BP.L", "GSK.L", "RIO.L", "DGE.L", "BATS.L", "REL.L",
    "LSEG.L", "NG.L", "BARC.L", "LLOY.L", "NWG.L", "VOD.L", "GLEN.L", "AAL.L", "BA.L", "RR.L",
    "CPG.L", "PRU.L", "AV.L", "LGEN.L", "STAN.L", "TSCO.L", "SSE.L", "IMB.L", "EXPN.L", "ABF.L",
    "WPP.L", "BT-A.L", "NXT.L", "JD.L", "KGF.L", "MKS.L", "SBRY.L", "OCDO.L", "ITV.L", "INF.L",
    "ANTO.L", "SGE.L", "AHT.L", "RKT.L", "HLMA.L", "SPX.L", "SMIN.L",
    # Irlanda, Portugal y Austria
    "RYA.IR", "KRZ.IR", "BIRG.IR", "EDP.LS", "GALP.LS", "JMT.LS", "BCP.LS", "EDPR.LS",
    "OMV.VI", "EBS.VI", "VER.VI", "VOE.VI", "ANDR.VI", "RBI.VI",
]

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
def market_for_weekday(weekday):
    return AUTO_SCHEDULE.get(weekday, "es")


def sp500_tickers():
    """Lista del S&P 500 desde Wikipedia (necesita lxml). Lanza error si no se puede bajar."""
    resp = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0; personal use)"},
        timeout=30,
    )
    resp.raise_for_status()
    table = pd.read_html(io.StringIO(resp.text))[0]
    out = [str(s).strip().replace(".", "-") for s in table["Symbol"]]
    if len(out) < 400:
        raise ValueError(f"la tabla del S&P 500 trae solo {len(out)} filas")
    return out


def get_universe(market="es"):
    if market == "eu":
        return sorted(set(EUROPE_TICKERS + EXTRA_TICKERS_EU))
    if market == "us":
        try:
            base = sp500_tickers()
        except Exception as e:
            print(f"[aviso] no pude bajar el S&P 500 ({e}); uso la lista corta de reserva")
            base = US_FALLBACK
        return sorted(set(base + EXTRA_TICKERS_US))
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


def _vs(x, lim, dec=0):
    """Texto de un valor que NO llega al umbral (o lo supera), sin que el redondeo lo iguale a él.
    Ej.: 29,7 frente a 30 -> "29,7" (y no "30"); 25,04 frente a 25 -> "25,0" -> "25,04"."""
    d = dec
    s = f"{x:.{d}f}"
    while abs(float(s) - lim) < 1e-9 and d < dec + 4:
        d += 1
        s = f"{x:.{d}f}"
    return s.replace(".", ",").replace("-", "−")


def _sym(cur):
    return {"EUR": "€", "USD": "$", "GBP": "£"}.get(cur or "", cur or "")


def _m(v, cur=""):
    sym = _sym(cur)
    if abs(v) >= 1e9:
        return f"{v / 1e6:,.0f}".replace(",", ".") + f"M{sym}"
    return f"{v / 1e6:.0f}M{sym}"


_SUFFIX = re.compile(
    r"[,\s]+(S\.?A\.?U?\.?|S\.?L\.?|PLC|SE|N\.?V\.?|INC\.?|CORP\.?|CORPORATION|LTD\.?|LIMITED|AG|"
    r"S\.?P\.?A\.?|A/S|AB|ASA|OYJ|CO\.?|COMPANY|HOLDINGS?)\.?$",
    re.I,
)


def _name(t, f):
    if t in TICKER_NAMES:
        return TICKER_NAMES[t]
    raw = str(f.get("name") or t).strip()
    for _ in range(3):  # quita sufijos legales: S.A., Inc., PLC, AG...
        new = _SUFFIX.sub("", raw)
        if new == raw or not new:
            break
        raw = new
    raw = raw.rstrip(" &,") or raw  # "KKR & Co." -> "KKR"
    if raw.isupper() and " " in raw:  # "BNP PARIBAS" -> "Bnp Paribas"; siglas sueltas (SAP, ASML) se dejan
        raw = raw.title()
    return raw if len(raw) <= 22 else raw[:21].rstrip() + "…"


def _tk(t):
    """Ticker para mostrar, sin el sufijo de bolsa: SAP.DE -> SAP."""
    return t.split(".")[0]


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
        shown = _vs(abs(dd) * 100, rules["min_drawdown"] * 100, 0)
        out.append(("fail", f"Caída {shown}% (pide {_pc(rules['min_drawdown'], 0)})", cap(gap)))

    net = _num(f, "profitMargins")
    lim = rules["min_net_margin"]
    if net is None:
        out.append(("nodata", "margen neto", 0.5))
    elif net < lim:
        out.append(("fail", f"Margen neto {_vs(net * 100, lim * 100, 1)}% (pide {_pc(lim, 0)})", cap((lim - net) / lim)))
    else:
        out.append(("ok", "", 0.0))

    if "min_operating_margin" in rules:
        op = _num(f, "operatingMargins")
        lim = rules["min_operating_margin"]
        if op is None:
            out.append(("nodata", "margen operativo", 0.5))
        elif op < lim:
            out.append(("fail", f"Margen operativo {_vs(op * 100, lim * 100, 1)}% (pide {_pc(lim, 0)})", cap((lim - op) / lim)))
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
        out.append(("fail", f"Ingresos {_vs(rev * 100, lim * 100, 1)}% (pide {_pc(lim, 0)})", cap((lim - rev) / abs(lim))))

    if "max_debt_to_equity" in rules:
        de = _num(f, "debtToEquity")
        lim = rules["max_debt_to_equity"]
        if de is not None and de > lim:
            out.append(("fail", f"Deuda/capital {_vs(de, lim, 0)}% (máx {lim:.0f}%)", cap((de - lim) / lim)))

    fpe = _num(f, "forwardPE")
    lim = rules["max_forward_pe"]
    if fpe is not None:
        if fpe <= 0:
            out.append(("fail", "PER a futuro negativo", 1.0))
        elif fpe > lim:
            out.append(("fail", f"PER {_vs(fpe, lim, 1)} (máx {lim:.0f})", cap((fpe - lim) / lim)))

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
def _dd_text(r):
    """Caída para mostrar: si no llega al umbral, con los decimales necesarios para que no parezca que sí llega."""
    dd = abs(r["stat"]["drawdown"]) * 100
    lim = RULES["min_drawdown"] * 100
    return (_vs(dd, lim, 0) if dd < lim else f"{dd:.0f}") + "%"


def _head(r, icon):
    t, f = r["t"], r["f"]
    yahoo = f'<a href="https://finance.yahoo.com/quote/{html.escape(t)}">Yahoo</a>'
    name = _name(t, f)
    label = f"<b>{html.escape(name)}</b>" + ("" if name.upper() == _tk(t) else f" ({html.escape(_tk(t))})")
    return f"{icon} {label} · {yahoo}"


def _block(r, icon, lines):
    """Una empresa: cabecera con icono + sus datos en un bloque de cita (barra a la izquierda) con viñetas."""
    body = "\n".join("• " + x for x in lines)
    return f"{_head(r, icon)}\n<blockquote>{body}</blockquote>"


def _cumple_lines(r):
    f = r["f"]
    L = [f"Cae {_dd_text(r)}"]
    per = _num(f, "trailingPE")
    if per is None or per <= 0:
        per = _num(f, "forwardPE")
    bits = []
    if per and per > 0:
        bits.append(f"PER {_n(per)} (rentabilidad {100 / per:.0f}%)")
    pb = _num(f, "priceToBook")
    if pb is not None:
        bits.append("valor contable negativo" if pb <= 0 else f"valor contable {_n(pb, 2)}x")
    if bits:
        L.append(" · ".join(bits))
    L += r.get("extra", [])
    return [html.escape(x) for x in L]


def _near_lines(r):
    motivos = [f"❌ {t}" for t in r["fails"]] + [f"❔ Sin dato de {x}" for x in r["nodata"]]
    if len(motivos) > 3:
        motivos = motivos[:3] + [f"❌ y {len(motivos) - 3} más"]
    if not any(t.startswith("Caída") for t in r["fails"]):
        motivos.insert(0, f"Cae {_dd_text(r)}")
    return [html.escape(m) for m in motivos]


def _fit(parts, limit=3900):
    """Une los bloques sin pasar del límite de Telegram, cortando siempre entre bloques
    (así nunca queda una etiqueta HTML a medias)."""
    out, size = [], 0
    for p in parts:
        if size + len(p) + 1 > limit:
            out.append("…")
            break
        out.append(p)
        size += len(p) + 1
    return "\n".join(out)


def build_report(cumple, near, n_review, no_data=0, n_total=0, market=""):
    cover = f" · {n_total} empresas revisadas" if n_total else ""
    where = f" · {market}" if market else ""
    L = [f"📉 <b>Calidad en caída{where} · {date.today():%d/%m}</b>",
         f"<i>Caída desde el máximo de 12 meses{cover}</i>",
         "", "✅ <b>CUMPLEN LAS PAUTAS</b>", ""]
    if cumple:
        for i, r in enumerate(cumple):
            if i:
                L.append("")
            L.append(_block(r, "🟢", _cumple_lines(r)))
    else:
        L.append("Ninguna hoy")
    if near:
        L += ["", "🟡 <b>NO CUMPLEN, PERO ESTÁN CERCA</b>", ""]
        for i, r in enumerate(near):
            if i:
                L.append("")
            L.append(_block(r, "🟡", _near_lines(r)))
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
    ap.add_argument("--tickers", default="", help="lista separada por comas (por defecto: el mercado elegido)")
    ap.add_argument("--market", default="auto", choices=["auto", "es", "eu", "us"],
                    help="mercado a revisar; 'auto' elige según el día (lun=España, mar=Europa, mié=EE. UU.)")
    args = ap.parse_args()

    if args.test:
        send("✅ Bot conectado. Aquí recibirás los avisos de calidad en caída.")
        print("Mensaje de prueba enviado.")
        return

    market = args.market
    if market == "auto":
        market = market_for_weekday(date.today().weekday())
    cfg = MARKETS[market]
    print(f"Mercado: {cfg['name']}")

    custom = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if custom:
        tickers = custom
    else:
        try:
            tickers = get_universe(market)
        except Exception as e:
            msg = f"No pude bajar la lista de empresas de {cfg['name']}: {e}"
            print(f"[error] {msg}")
            if not args.dry_run:
                send(f"⚠️ {html.escape(msg)}")
            return
    print(f"Analizando {len(tickers)} valores...")

    stats = price_stats(tickers)
    missing = [t for t in tickers if t not in stats]
    if missing:
        print(f"[aviso] sin datos de precio para {len(missing)}: {', '.join(missing)}")

    min_dd, max_review = cfg["review_min_drawdown"], cfg["max_review"]
    fallen = sorted(
        ((t, s) for t, s in stats.items() if s["drawdown"] <= -min_dd),
        key=lambda ts: ts[1]["drawdown"],
    )
    review = fallen[:max_review]
    print(f"{len(fallen)} con caída >= {min_dd:.0%}; reviso las {len(review)} que más han caído")

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
    many = len(cumple) >= MAX_CUMPLE          # hay de sobra: solo se muestran 10, sin "casi" ni "y N más"
    cumple = cumple[:MAX_CUMPLE]
    near = [] if many else sorted(
        (r for r in rows if r["kind"] == "no" and not r["missing"]), key=lambda r: r["score"]
    )[:TOP_NEAR]

    if SHOW_EXTRA:
        for r in cumple:
            r["extra"] = extras(r["t"], r["stat"], r["f"])

    warn = no_data if (rows and no_data * 2 >= len(rows)) else 0
    text = build_report(cumple, near, len(rows), warn, len(stats),
                        market=cfg["name"] if not custom else "")

    if args.dry_run:
        print("\n" + text)
        return

    send(text)
    for r in cumple:
        state[r["t"]] = {"date": date.today().isoformat(), "drawdown": r["stat"]["drawdown"]}
    save_state(state)


if __name__ == "__main__":
    main()
