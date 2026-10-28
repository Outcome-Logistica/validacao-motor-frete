# -*- coding: utf-8 -*-
"""Como os testes acham um banco — em um lugar só.

`MOTOR_FRETE_TESTE_DB` aceita duas formas, e as duas significam a mesma coisa:
"pode usar este banco no teste".

    MOTOR_FRETE_TESTE_DB=1                        usa a configuração do serviço
                                                  (o .env do motor-frete)
    MOTOR_FRETE_TESTE_DB=host|porta|user|senha|banco
                                                  usa esta conexão, ignorando o .env

A primeira forma é a do dia a dia: não exige senha na linha de comando e não
deixa credencial no histórico do shell. A segunda serve para apontar para um
banco diferente do que o serviço usa — um banco descartável, por exemplo.

**Atenção:** os testes de CEP APAGAM `ceps_resolvidos` para começar do zero.
Não aponte isto para um banco de produção.
"""

from __future__ import annotations

import os

import pytest

#  O que vale para todos: sem a variável, os testes de banco pulam.
precisa_db = pytest.mark.skipif(
    not os.getenv("MOTOR_FRETE_TESTE_DB"),
    reason="defina MOTOR_FRETE_TESTE_DB=1 para rodar contra um banco")


def _conectar():
    """Abre a conexão do teste, ou falha dizendo exatamente o que arrumar.

    Antes isto estourava um traceback de 200 linhas do pymysql quando o host
    estava errado — o que esconde a única informação útil, que é o host tentado.
    """
    import pymysql

    dsn = os.getenv("MOTOR_FRETE_TESTE_DB", "").strip()
    if not dsn:
        pytest.skip("sem banco de teste")

    if "|" in dsn:
        partes = dsn.split("|")
        if len(partes) != 5:
            pytest.fail(
                f"MOTOR_FRETE_TESTE_DB tem {len(partes)} campo(s), esperava 5: "
                f"host|porta|user|senha|banco. Ou use apenas '1' para herdar o .env.")
        host, porta, user, senha, banco = partes
        porta = int(porta)
    else:
        from motor_frete.config import settings
        host, porta = settings.MYSQL_HOST, settings.MYSQL_PORT
        user, senha, banco = (settings.MYSQL_USER, settings.MYSQL_PASSWORD,
                              settings.MYSQL_DATABASE)

    try:
        return pymysql.connect(
            host=host, port=porta, user=user, password=senha, database=banco,
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
            autocommit=True, connect_timeout=5)
    except pymysql.err.OperationalError as e:
        codigo = e.args[0]
        dica = ""
        if host == "db":
            #  O erro que eu mesmo causei na primeira tentativa: `db` é o nome
            #  do serviço DENTRO da rede do compose. Da máquina, não resolve.
            dica = ("\n   'db' é o nome do serviço dentro da rede do Docker e não "
                    "resolve aqui.\n   Crie motor-frete/.env com o MYSQL_HOST que "
                    "funciona na sua máquina\n   (o mesmo do outcome_api/.env), ou passe "
                    "o DSN:\n   $env:MOTOR_FRETE_TESTE_DB = 'localhost|3306|root|SENHA|outcome_db'")
        pytest.fail(
            f"não consegui conectar em {host}:{porta}/{banco} como {user} "
            f"(MySQL {codigo}).{dica}")


@pytest.fixture
def cur():
    """Cursor para teste de leitura. Não apaga nada."""
    conn = _conectar()
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.close()


@pytest.fixture
def cur_cache_limpo():
    """Cursor com `ceps_resolvidos` zerado — para os testes da cascata de CEP.

    A cascata inteira gira em volta do cache, e teste de cache que começa com
    cache sujo não prova nada.
    """
    conn = _conectar()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM ceps_resolvidos")
            yield c
    finally:
        conn.close()
