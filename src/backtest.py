"""
Backtest del modelo de cierre.

La pregunta que quiero responder: si me paro en distintos días del mes y proyecto
el cierre, ¿qué tan cerca quedo del cierre real?

Como estos datos son simulados, sí tengo el "cierre real" del mes en curso (el
archivo _cierre_real), que en la vida real todavía no existiría. Lo uso solo aquí,
para medir el error.

Corro la estimación parándome en varios días de corte (5, 10, 15, 20) y comparo
contra el total real de cada llave. Reporto:
  - MAPE  (error porcentual absoluto medio) por llave
  - error global (sobre el total de unidades)
La idea es que entre más avanza el mes, el error debería bajar.
"""

import calendar
from pathlib import Path

import numpy as np
import pandas as pd

import modelo_cierre as mc

DATA_DIR = Path("data")
DIAS_CORTE = [5, 10, 15, 20]


def cargar_cierre_real():
    """Órdenes completas del mes en curso (lo que de verdad cerró)."""
    ruta = DATA_DIR / f"ordenes_{mc.ANIO}_{mc.MES_EN_CURSO:02d}_cierre_real.xlsx"
    df = pd.read_excel(ruta)
    df["fecha_entrega"] = pd.to_datetime(df["fecha_entrega"])
    df["dia"] = df["fecha_entrega"].dt.day
    df["llave"] = df["pais"] + "_" + df["sku"]
    return df


def total_real_por_llave(df):
    return (
        df.groupby("llave")["cantidad"].sum()
        .reset_index().rename(columns={"cantidad": "uds_reales"})
    )


def mape(estimado, real):
    """Error porcentual absoluto medio, ignorando reales en cero."""
    estimado = np.asarray(estimado, dtype=float)
    real = np.asarray(real, dtype=float)
    valido = real > 0
    return np.mean(np.abs(estimado[valido] - real[valido]) / real[valido])


def correr_backtest():
    df_real = cargar_cierre_real()
    reales = total_real_por_llave(df_real)
    filas = []

    print("Backtest — proyección de cierre vs cierre real\n")
    print(f"{'Dia corte':>10} | {'MAPE por llave':>14} | {'Error global':>13} | "
          f"{'Avance real':>12}")
    print("-" * 60)

    for dia in DIAS_CORTE:
        # el "mes a medias" es el cierre real truncado a este día de corte
        df_curso = df_real[df_real["dia"] <= dia].copy()
        res = mc.correr(dia_hoy=dia, df_curso=df_curso, verbose=False)
        comp = res.merge(reales, on="llave", how="left")
        comp["uds_reales"] = comp["uds_reales"].fillna(0)

        error_mape = mape(comp["uds_cierre_est"], comp["uds_reales"])

        est_total = comp["uds_cierre_est"].sum()
        real_total = comp["uds_reales"].sum()
        error_global = abs(est_total - real_total) / real_total

        # cuánto del cierre real ya había llegado al día de corte
        avance_real = comp["uds_observadas"].sum() / real_total

        filas.append({
            "dia_corte": dia,
            "mape_llave": error_mape,
            "error_global": error_global,
            "avance_real": avance_real,
        })

        print(f"{dia:>10} | {error_mape:>13.1%} | {error_global:>12.1%} | "
              f"{avance_real:>11.1%}")

    print("\nLectura rápida:")
    print("- El error global (sobre el total de unidades) es el que más importa")
    print("  para planeación agregada; el MAPE por llave es más duro porque castiga")
    print("  mucho a los SKUs chicos.")
    print("- Conforme avanza el mes y hay más órdenes confirmadas, el error baja.")

    return pd.DataFrame(filas)


if __name__ == "__main__":
    correr_backtest()
