"""
Generador de datos sintéticos para el modelo de estimación de cierre de demanda.

Crea órdenes de venta inventadas con un patrón de llegada realista: hay productos
que concentran sus pedidos al inicio del mes y otros que cargan al final. Con eso
el modelo tiene algo parecido a lo que uno ve en la vida real, pero sin usar datos
de ninguna empresa.

Genera:
  - 3 meses de historia (para armar el perfil de llegada)
  - 1 mes "en curso" cortado a un día (para simular que estamos a mitad de mes)
  - un archivo de forecast por país y SKU

Todo sale a la carpeta data/ como archivos .xlsx.
"""

import numpy as np
import pandas as pd
import calendar
from pathlib import Path

# Semilla fija para que los datos salgan iguales cada vez que se corre
SEMILLA = 42
rng = np.random.default_rng(SEMILLA)

# ----------------------------------------------------------------------
# Parámetros del universo inventado
# ----------------------------------------------------------------------
PAISES = ["Mexico", "Guatemala", "Panama", "Colombia", "Ecuador", "Brasil"]

REGIONES = {
    "Mexico": "Norte",
    "Guatemala": "Centro",
    "Panama": "Centro",
    "Colombia": "Sur",
    "Ecuador": "Sur",
    "Brasil": "Sur",
}

N_SKUS = 25                     # cuántos productos distintos
MESES_HISTORIA = [3, 4, 5]      # marzo, abril, mayo
ANIO = 2026
MES_EN_CURSO = 6                # junio
DIA_CORTE = 12                  # "hoy" es el día 12 del mes en curso

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ----------------------------------------------------------------------
# Cada SKU tiene un "perfil de llegada": cómo reparte sus pedidos a lo
# largo del mes. Unos cargan temprano, otros tarde, otros parejo.
# ----------------------------------------------------------------------
def perfil_llegada(tipo, dias_mes):
    """Devuelve un vector de pesos por día (suma 1) según el tipo de producto."""
    dias = np.arange(1, dias_mes + 1)

    if tipo == "temprano":
        # más peso en la primera quincena
        pesos = np.exp(-0.12 * dias)
    elif tipo == "tarde":
        # más peso al final del mes
        pesos = np.exp(0.10 * dias)
    elif tipo == "medio":
        # concentrado a mitad de mes (forma de campana)
        centro = dias_mes / 2
        pesos = np.exp(-((dias - centro) ** 2) / (2 * (dias_mes / 4) ** 2))
    else:  # parejo
        pesos = np.ones(dias_mes)

    return pesos / pesos.sum()


# Asignamos a cada SKU un tipo de perfil y una demanda base
tipos = rng.choice(
    ["temprano", "tarde", "medio", "parejo"],
    size=N_SKUS,
    p=[0.3, 0.3, 0.2, 0.2],
)
demanda_base = rng.integers(200, 3000, size=N_SKUS)  # unidades promedio al mes

skus = [f"SKU-{i:03d}" for i in range(1, N_SKUS + 1)]
descripciones = [f"Producto generico {i:03d}" for i in range(1, N_SKUS + 1)]

sku_info = pd.DataFrame({
    "sku": skus,
    "descripcion": descripciones,
    "tipo_perfil": tipos,
    "demanda_base": demanda_base,
})


def generar_mes(mes, anio, dia_corte=None):
    """
    Genera las órdenes de un mes. Si se pasa dia_corte, solo devuelve las
    órdenes hasta ese día (simula un mes a medias).
    """
    dias_mes = calendar.monthrange(anio, mes)[1]
    filas = []

    for _, s in sku_info.iterrows():
        perfil = perfil_llegada(s["tipo_perfil"], dias_mes)

        for pais in PAISES:
            # cada país pesa distinto para el mismo SKU
            factor_pais = rng.uniform(0.3, 1.5)
            # el mes trae algo de ruido respecto a la demanda base
            total_mes = s["demanda_base"] * factor_pais * rng.uniform(0.75, 1.25)
            total_mes = int(round(total_mes))
            if total_mes <= 0:
                continue

            # repartimos el total del mes entre los días según el perfil
            unidades_por_dia = rng.multinomial(total_mes, perfil)

            for dia, unidades in enumerate(unidades_por_dia, start=1):
                if unidades <= 0:
                    continue
                if dia_corte is not None and dia > dia_corte:
                    continue  # todavía no "llega" esa orden
                filas.append({
                    "fecha_entrega": pd.Timestamp(year=anio, month=mes, day=dia),
                    "pais": pais,
                    "sku": s["sku"],
                    "descripcion": s["descripcion"],
                    "cantidad": int(unidades),
                })

    return pd.DataFrame(filas)


# ----------------------------------------------------------------------
# Generar los 3 meses de historia completos
# ----------------------------------------------------------------------
for mes in MESES_HISTORIA:
    df = generar_mes(mes, ANIO)
    nombre = DATA_DIR / f"ordenes_{ANIO}_{mes:02d}.xlsx"
    df.to_excel(nombre, index=False)
    print(f"Historia {mes:02d}/{ANIO}: {len(df):,} ordenes -> {nombre.name}")

# ----------------------------------------------------------------------
# Mes en curso COMPLETO: es el "cierre real" que en la vida real todavía
# no conoceríamos. Se usa solo en el backtest para medir el error.
# ----------------------------------------------------------------------
df_curso_real = generar_mes(MES_EN_CURSO, ANIO)
nombre_real = DATA_DIR / f"ordenes_{ANIO}_{MES_EN_CURSO:02d}_cierre_real.xlsx"
df_curso_real.to_excel(nombre_real, index=False)
print(f"Cierre real {MES_EN_CURSO:02d}/{ANIO} (solo backtest): "
      f"{len(df_curso_real):,} ordenes -> {nombre_real.name}")

# ----------------------------------------------------------------------
# Mes en curso "parcial": es EXACTAMENTE el mes real cortado al día de
# corte. Así el parcial y el real son el mismo mes, no dos simulaciones
# distintas — es lo que pasaría en la vida real a mitad de mes.
# ----------------------------------------------------------------------
df_curso = df_curso_real[df_curso_real["fecha_entrega"].dt.day <= DIA_CORTE].copy()
nombre_curso = DATA_DIR / f"ordenes_{ANIO}_{MES_EN_CURSO:02d}_parcial.xlsx"
df_curso.to_excel(nombre_curso, index=False)
print(f"Mes en curso {MES_EN_CURSO:02d}/{ANIO} (hasta dia {DIA_CORTE}): "
      f"{len(df_curso):,} ordenes -> {nombre_curso.name}")

# ----------------------------------------------------------------------
# Forecast por país y SKU para el mes en curso.
# Lo armamos como "lo que esperábamos vender", con un error metido a
# propósito para que el modelo tenga algo que corregir.
# ----------------------------------------------------------------------
filas_fcst = []
for _, s in sku_info.iterrows():
    for pais in PAISES:
        factor_pais = rng.uniform(0.3, 1.5)
        esperado = s["demanda_base"] * factor_pais
        # el forecast se equivoca entre -20% y +20%
        fcst = esperado * rng.uniform(0.80, 1.20)
        fcst = int(round(fcst))
        if fcst > 0:
            filas_fcst.append({
                "pais": pais,
                "sku": s["sku"],
                "forecast": fcst,
            })

df_fcst = pd.DataFrame(filas_fcst)
nombre_fcst = DATA_DIR / f"forecast_{ANIO}_{MES_EN_CURSO:02d}.xlsx"
df_fcst.to_excel(nombre_fcst, index=False)
print(f"Forecast {MES_EN_CURSO:02d}/{ANIO}: {len(df_fcst):,} filas -> {nombre_fcst.name}")

# Tabla de regiones (para enriquecer resultados)
df_geo = pd.DataFrame({
    "pais": list(REGIONES.keys()),
    "region": list(REGIONES.values()),
})
df_geo.to_excel(DATA_DIR / "geografia.xlsx", index=False)

print("\nListo. Todos los archivos quedaron en la carpeta data/")
