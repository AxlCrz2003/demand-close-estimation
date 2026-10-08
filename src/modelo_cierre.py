"""
Modelo de estimación de cierre de demanda.

La idea es simple: estamos a mitad de mes y queremos saber en cuánto va a cerrar
la demanda de cada producto-país antes de que termine el mes. En vez de asumir que
lo que falta llega parejo, uso la historia para saber qué porcentaje de las órdenes
suele haber "caído" a estas alturas del mes.

Flujo:
  1. Con 3 meses de historia armo, para cada combinación país+SKU, una curva de
     acumulado: qué fracción del total del mes llega al día 1, al día 2, etc.
  2. Para el mes en curso miro cuántas unidades llevo contra el forecast (avance).
  3. Con la curva histórica sé qué fracción del mes ya debería haber pasado, y de ahí
     proyecto el cierre.
  4. Un semáforo marca si un producto va sobre, bajo o dentro del forecast.

Todo corre con los datos de la carpeta data/ (generados por generar_datos.py).
"""

import calendar
from pathlib import Path

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------
# Parámetros (los que uno cambiaría cada mes)
# ----------------------------------------------------------------------
ANIO = 2026
MES_EN_CURSO = 6          # junio
DIA_HOY = 12              # día de corte: "hoy" es el 12
MESES_HISTORIA = [3, 4, 5]

UMBRAL_SOBRE = 1.20       # arriba de 120% del forecast -> sobre demanda
UMBRAL_BAJA = 0.80        # abajo de 80% -> baja demanda

DATA_DIR = Path("data")


# ----------------------------------------------------------------------
# Carga
# ----------------------------------------------------------------------
def cargar_ordenes(mes, parcial=False):
    sufijo = "_parcial" if parcial else ""
    ruta = DATA_DIR / f"ordenes_{ANIO}_{mes:02d}{sufijo}.xlsx"
    df = pd.read_excel(ruta)
    df["fecha_entrega"] = pd.to_datetime(df["fecha_entrega"])
    df["dia"] = df["fecha_entrega"].dt.day
    # llave = la combinación que seguimos: país + producto
    df["llave"] = df["pais"] + "_" + df["sku"]
    return df


def cargar_forecast():
    df = pd.read_excel(DATA_DIR / f"forecast_{ANIO}_{MES_EN_CURSO:02d}.xlsx")
    df["llave"] = df["pais"] + "_" + df["sku"]
    return df[["llave", "pais", "sku", "forecast"]]


# ----------------------------------------------------------------------
# Paso 1: curva histórica de acumulado por llave
# ----------------------------------------------------------------------
def perfil_de_un_mes(df):
    """Para cada llave: qué proporción de su total mensual llega cada día."""
    por_dia = (
        df.groupby(["llave", "dia"])["cantidad"].sum().reset_index()
    )
    total = (
        df.groupby("llave")["cantidad"].sum()
        .reset_index().rename(columns={"cantidad": "total_mes"})
    )
    perfil = por_dia.merge(total, on="llave")
    perfil["proporcion"] = perfil["cantidad"] / perfil["total_mes"]
    return perfil[["llave", "dia", "proporcion"]]


def construir_perfil_historico(meses):
    """
    Promedia el perfil de varios meses y lo convierte en una curva de acumulado
    normalizada (de 0 a 1) por llave.
    """
    perfiles = []
    for mes in meses:
        df_mes = cargar_ordenes(mes)
        perfiles.append(perfil_de_un_mes(df_mes))

    todos = pd.concat(perfiles, ignore_index=True)
    n_meses = len(meses)

    # Promedio de la proporción diaria entre los meses disponibles.
    # Uso sum()/n_meses en vez de mean() a propósito: si una llave no tuvo
    # ventas un día en alguno de los meses, ese cero debe contar en el promedio.
    perfil = (
        todos.groupby(["llave", "dia"])["proporcion"].sum().reset_index()
    )
    perfil["proporcion"] = perfil["proporcion"] / n_meses

    # Acumulado día a día y normalización para que cada llave llegue a 1.0
    perfil = perfil.sort_values(["llave", "dia"]).reset_index(drop=True)
    perfil["acum"] = perfil.groupby("llave")["proporcion"].cumsum()

    maximos = (
        perfil.groupby("llave")["acum"].max()
        .reset_index().rename(columns={"acum": "acum_max"})
    )
    perfil = perfil.merge(maximos, on="llave")
    perfil["acum_norm"] = perfil["acum"] / perfil["acum_max"]

    return perfil[["llave", "dia", "acum_norm"]]


def fraccion_hasta_dia(perfil, dia):
    """
    Devuelve, por llave, qué fracción del mes ya debería haber llegado al 'dia'
    según la curva histórica.
    """
    hasta = perfil[perfil["dia"] <= dia]
    frac = (
        hasta.sort_values(["llave", "dia"])
        .groupby("llave")["acum_norm"].last()
        .reset_index().rename(columns={"acum_norm": "frac_historica"})
    )
    return frac


# ----------------------------------------------------------------------
# Paso 2 y 3: avance real + proyección de cierre
# ----------------------------------------------------------------------
def estimar_cierre(perfil, df_curso, fcst, dia_hoy):
    # cuánto llevo confirmado este mes por llave, hasta el día de corte
    avance = (
        df_curso[df_curso["dia"] <= dia_hoy]
        .groupby("llave")["cantidad"].sum()
        .reset_index().rename(columns={"cantidad": "uds_observadas"})
    )

    res = fcst.merge(avance, on="llave", how="left")
    res["uds_observadas"] = res["uds_observadas"].fillna(0)

    # qué % del forecast llevo hasta hoy
    res["avance_vs_fcst"] = res["uds_observadas"] / res["forecast"]

    # qué fracción del mes ya pasó según la historia
    frac = fraccion_hasta_dia(perfil, dia_hoy)
    res = res.merge(frac, on="llave", how="left")

    # llaves sin historia: caigo a un supuesto lineal (día / días del mes)
    dias_mes = calendar.monthrange(ANIO, MES_EN_CURSO)[1]
    frac_lineal = dia_hoy / dias_mes
    res["usa_lineal"] = res["frac_historica"].isna()
    res["frac_historica"] = res["frac_historica"].fillna(frac_lineal)

    # proyección: lo que llevo + lo que, según la historia, falta por llegar
    res["frac_restante"] = 1 - res["frac_historica"]
    res["cierre_pct"] = res["avance_vs_fcst"] + res["frac_restante"]
    res["uds_cierre_est"] = res["cierre_pct"] * res["forecast"]
    res["uds_adicionales_est"] = (
        res["uds_cierre_est"] - res["uds_observadas"]
    ).clip(lower=0)

    return res


# ----------------------------------------------------------------------
# Paso 4: semáforo
# ----------------------------------------------------------------------
def semaforo(pct, hay_ordenes):
    if not hay_ordenes:
        return "Sin ordenes"
    if pct > UMBRAL_SOBRE:
        return "Sobre demanda"
    if pct < UMBRAL_BAJA:
        return "Baja demanda"
    return "En rango"


def agregar_semaforos(res):
    res["semaforo_avance"] = [
        semaforo(p, obs > 0)
        for p, obs in zip(res["avance_vs_fcst"], res["uds_observadas"])
    ]
    res["semaforo_cierre"] = [
        semaforo(p, obs > 0)
        for p, obs in zip(res["cierre_pct"], res["uds_observadas"])
    ]
    return res


# ----------------------------------------------------------------------
# Orquestación
# ----------------------------------------------------------------------
def correr(dia_hoy=DIA_HOY, df_curso=None, verbose=True):
    perfil = construir_perfil_historico(MESES_HISTORIA)
    if df_curso is None:
        df_curso = cargar_ordenes(MES_EN_CURSO, parcial=True)
    fcst = cargar_forecast()

    res = estimar_cierre(perfil, df_curso, fcst, dia_hoy)
    res = agregar_semaforos(res)

    # región, para poder agrupar
    geo = pd.read_excel(DATA_DIR / "geografia.xlsx")
    res = res.merge(geo, on="pais", how="left")

    res = res.sort_values(["semaforo_cierre", "cierre_pct"],
                          ascending=[True, False]).reset_index(drop=True)

    if verbose:
        _imprimir_resumen(res, dia_hoy)

    return res


def _imprimir_resumen(res, dia_hoy):
    print(f"Estimación de cierre — corte al día {dia_hoy}/{MES_EN_CURSO}/{ANIO}")
    print(f"Llaves (país+SKU): {len(res):,}\n")

    print("Semáforo de cierre:")
    print(res["semaforo_cierre"].value_counts().to_string())

    fcst_total = res["forecast"].sum()
    obs_total = res["uds_observadas"].sum()
    cierre_total = res["uds_cierre_est"].sum()
    print(f"\nForecast total:        {fcst_total:>12,.0f} uds")
    print(f"Observado al corte:    {obs_total:>12,.0f} uds "
          f"({obs_total / fcst_total:.1%} del forecast)")
    print(f"Cierre estimado:       {cierre_total:>12,.0f} uds "
          f"({cierre_total / fcst_total:.1%} del forecast)")

    lineal = res["usa_lineal"].sum()
    if lineal:
        print(f"\nOjo: {lineal} llave(s) sin historia usaron supuesto lineal.")


if __name__ == "__main__":
    resultado = correr()

    cols = [
        "llave", "region", "pais", "sku", "forecast",
        "uds_observadas", "avance_vs_fcst", "frac_historica",
        "cierre_pct", "uds_cierre_est", "semaforo_cierre",
    ]
    print("\nPrimeras filas:")
    with pd.option_context("display.max_columns", None, "display.width", 140):
        print(resultado[cols].head(10).to_string(index=False))
