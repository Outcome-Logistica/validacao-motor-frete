# -*- coding: utf-8 -*-
"""Acesso ao banco.

O motor lê direto do schema relacional de cotação, sem passar pela API do
Outcome. É a decisão registrada no diagrama de arquitetura: `depends_on: db`.
"""

from __future__ import annotations

import contextlib
import logging

import pymysql

from .config import settings

log = logging.getLogger(__name__)

# As 21 tabelas do schema_cotacao.sql. O /health confere se estão todas lá —
# subir o serviço contra um banco sem o schema é o erro mais provável no começo.
TABELAS_ESPERADAS = (
    "ufs", "pracas", "localidades", "icms_uf",
    "modalidades", "tipos_frete", "segmentos", "categorias_produto",
    "componentes_tarifa", "componentes_tarifa_alias",
    "perfis_generalidade", "perfis_generalidade_itens",
    "regioes_atendidas", "regioes_atendidas_vigencia",
    "tabelas_frete_v2", "tabelas_frete_peso", "faixas_valores",
    "tabelas_frete_categorias", "tarifas_cliente_destino",
    "cotacoes", "cotacoes_componentes",
)


def conectar():
    """Uma conexão nova. Sem pool por enquanto — entra quando houver carga real."""
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


@contextlib.contextmanager
def cursor():
    conn = conectar()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def checar_banco() -> tuple[str, str, str | None]:
    """Devolve (banco, schema_cotacao, detalhe) para o /health."""
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            cur.execute(
                "SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                (settings.MYSQL_DATABASE,))
            existentes = {r["t"] for r in cur.fetchall()}
    except Exception as e:
        log.warning("banco indisponível: %s", e)
        return "indisponivel", "desconhecido", str(e)

    faltando = [t for t in TABELAS_ESPERADAS if t not in existentes]
    if faltando:
        return "ok", "incompleto", (
            f"{len(faltando)} de {len(TABELAS_ESPERADAS)} tabelas do schema de cotação "
            f"não existem: {', '.join(faltando[:6])}"
            + (" …" if len(faltando) > 6 else "")
            + ". Rode: python arquitetura_db/run_sql.py schema_cotacao.sql")
    return "ok", "ok", None
