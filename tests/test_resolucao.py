# -*- coding: utf-8 -*-
"""Testes da resolução — do CEP até a faixa de peso.

Duas metades:

**A aritmética das faixas** (não precisa de banco). É aqui que mora o risco de
verdade: qual coluna carrega o preço, onde o excedente começa, o que acontece no
ponto de virada. Cada caso abaixo tem o número esperado escrito à mão, e vários
vêm de linhas reais da parceira.

**A cascata inteira** (precisa de banco). Roda contra o banco carregado quando
`MOTOR_FRETE_TESTE_DB` estiver definido; senão pula.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from motor_frete.esquemas import Aviso, MotivoDescarte
from motor_frete.resolucao import (
    Contexto, DescarteError, Faixas, escolher_tabela, preco_da_faixa,
    previsao_de_entrega,
)


def ctx() -> Contexto:
    return Contexto(
        transportadora_id=1, transportadora="TESTE",
        regiao_atendida_id=1, praca_id=0, praca="INTERIOR",
        modalidade_id=1, modalidade="RODOVIARIO", fator_cubagem=Decimal("300"))


def banda(ini, fim, preco):
    """Uma linha `Faixa`: o preço vai em `minimo`, e `valor` fica vazio."""
    return {"id": int(Decimal(str(ini)) * 100) + 1,
            "peso_inicio": Decimal(str(ini)),
            "peso_fim": None if fim is None else Decimal(str(fim)),
            "minimo": Decimal(str(preco)), "valor": None, "tipo_calculo": "Faixa"}


def exc(ini, fim, por_kg):
    """Uma linha `Excedente`: R$/kg, e o preço vai em `valor`."""
    return {"id": 9001, "peso_inicio": Decimal(str(ini)),
            "peso_fim": None if fim is None else Decimal(str(fim)),
            "minimo": None, "valor": Decimal(str(por_kg)), "tipo_calculo": "Excedente"}


# ═══════════════════════════════════════════════ o preço da faixa
def test_faixa_traz_o_preco_em_minimo():
    """As 3.585 linhas `Faixa` têm `valor` vazio — o preço está em `minimo`.

    Eu havia lido isso como lacuna de cadastro e relatado "59% das faixas sem
    valor". Estava errado: a coluna usada depende do tipo_calculo.
    """
    f = Faixas(bandas=(banda(0, 10, "200.00"),))
    preco, faixa_id, com_exc = preco_da_faixa(f, Decimal("7"))
    assert preco == Decimal("200.00")
    assert faixa_id == f.bandas[0]["id"]
    assert com_exc is False


def test_escolhe_a_banda_certa_entre_varias():
    f = Faixas(bandas=(banda(0, 10, "80"), banda(10, 30, "120"), banda(30, 70, "161.88")))
    assert preco_da_faixa(f, Decimal("5"))[0] == Decimal("80")
    assert preco_da_faixa(f, Decimal("10"))[0] == Decimal("80"), "o limite pertence à banda de baixo"
    assert preco_da_faixa(f, Decimal("10.5"))[0] == Decimal("120")
    assert preco_da_faixa(f, Decimal("70"))[0] == Decimal("161.88")


def test_excedente_soma_sobre_a_ultima_faixa():
    """O caso que a pendência 2 descreve: 100 kg numa tabela que termina em 70.

        161,88 + (100 − 70) × 2,32 = 231,48
    """
    f = Faixas(bandas=(banda(30, 70, "161.88"),), excedente=exc(70, 99999, "2.32"))
    preco, faixa_id, com_exc = preco_da_faixa(f, Decimal("100"))
    assert preco == Decimal("231.48")
    assert com_exc is True
    assert faixa_id == 9001, "o rastreio aponta a linha do excedente"


def test_excedente_nao_da_salto_no_ponto_de_virada():
    """A razão de ser da pendência 2: no teto, os dois modos têm que coincidir.

    Se o excedente fosse `peso × tarifa`, 70 kg custaria 70 × 2,32 = R$ 162,40
    pela faixa e o preço saltaria de forma descontínua logo acima. Somando sobre
    a última faixa, 70,001 kg custa praticamente o mesmo que 70 kg.
    """
    f = Faixas(bandas=(banda(30, 70, "161.88"),), excedente=exc(70, 99999, "2.32"))
    no_teto = preco_da_faixa(f, Decimal("70"))[0]
    logo_acima = preco_da_faixa(f, Decimal("70.001"))[0]
    assert no_teto == Decimal("161.88")
    assert abs(logo_acima - no_teto) < Decimal("0.01")


def test_caso_real_da_tabela_209():
    """Linha real da parceira: R$ 200 até 10 kg, excedente de R$ 7,50/kg.

    Para 25 kg: 200 + (25 − 10) × 7,50 = R$ 312,50
    """
    f = Faixas(bandas=(banda(0, 10, "200.00"),), excedente=exc(10, 99999, "7.50"))
    assert preco_da_faixa(f, Decimal("25"))[0] == Decimal("312.50")


def test_excedente_respeita_o_proprio_piso():
    """`ExcedenteMinimo` — 22 linhas no dado dela."""
    f = Faixas(bandas=(banda(0, 10, "10"),), excedente=exc(10, 99999, "0.50"),
               excedente_minimo=Decimal("300"))
    assert preco_da_faixa(f, Decimal("20"))[0] == Decimal("300")


def test_excedente_sozinho_e_tarifa_por_kg():
    """Frete-peso sem banda de faixa: o excedente é a tarifa inteira."""
    f = Faixas(excedente=exc(0, 99999, "3.00"))
    assert preco_da_faixa(f, Decimal("50"))[0] == Decimal("150.00")


def test_peso_fora_das_faixas_descarta_com_motivo():
    """Sem excedente, acima do teto não há preço — e a transportadora sai da
    cotação dizendo por quê."""
    f = Faixas(bandas=(banda(0, 10, "200"),))
    with pytest.raises(DescarteError) as e:
        preco_da_faixa(f, Decimal("500"))
    assert e.value.motivo is MotivoDescarte.PESO_FORA_DAS_FAIXAS
    assert "10" in (e.value.detalhe or ""), "a mensagem diz até onde a tabela vai"


def test_banda_sem_teto_atende_qualquer_peso():
    f = Faixas(bandas=(banda(0, None, "500"),))
    assert preco_da_faixa(f, Decimal("100000"))[0] == Decimal("500")


def test_tudo_decimal_nunca_float():
    f = Faixas(bandas=(banda(0, 10, "200.00"),), excedente=exc(10, 99999, "7.50"))
    for peso in (Decimal("5"), Decimal("25"), Decimal("1000")):
        assert isinstance(preco_da_faixa(f, peso)[0], Decimal)


# ═══════════════════════════════════════════════ a assinatura das faixas
def test_assinatura_iguala_duplicata_pura():
    """Duas das dez chaves repetidas de frete-peso têm faixas idênticas —
    nessas, tanto faz qual escolher."""
    a = Faixas(bandas=(banda(0, 10, "200"),), excedente=exc(10, 99999, "7.50"))
    b = Faixas(bandas=(banda(0, 10, "200"),), excedente=exc(10, 99999, "7.50"))
    assert a.assinatura() == b.assinatura()


def test_assinatura_separa_precos_diferentes():
    """As outras oito: R$ 200 contra R$ 190, R$ 7,50/kg contra R$ 6,00/kg."""
    a = Faixas(bandas=(banda(0, 10, "200"),), excedente=exc(10, 99999, "7.50"))
    b = Faixas(bandas=(banda(0, 10, "190"),), excedente=exc(10, 99999, "6.00"))
    assert a.assinatura() != b.assinatura()


def test_faixas_vazia_e_detectada():
    assert Faixas().vazia() is True
    assert Faixas(bandas=(banda(0, 10, "1"),)).vazia() is False


# ═══════════════════════════════════════════════ escolha entre tabelas
def test_tabela_sem_frete_peso_e_descartada():
    """139 das 164 tabelas dela não têm linha de frete-peso — não produzem
    preço, então não servem para cotar."""
    c = ctx()
    with pytest.raises(DescarteError) as e:
        escolher_tabela([{"id": 1, "data_inicio": date(2021, 1, 1), "qtd_frete_peso": 0}], c)
    assert e.value.motivo is MotivoDescarte.SEM_TABELA_VIGENTE


def test_entre_duas_tabelas_ganha_a_mais_recente_sem_aviso():
    """Datas diferentes não são ambiguidade: a mais nova é a vigente."""
    c = ctx()
    escolhida = escolher_tabela([
        {"id": 96, "data_inicio": date(2022, 4, 16), "qtd_frete_peso": 3},
        {"id": 56, "data_inicio": date(2021, 1, 4), "qtd_frete_peso": 3},
    ], c)
    assert escolhida["id"] == 96
    assert c.avisos == []


def test_empate_de_data_avisa_tabela_ambigua():
    """O caso real: tabelas 74 e 76 da transportadora 79, idênticas em tudo."""
    c = ctx()
    escolhida = escolher_tabela([
        {"id": 76, "data_inicio": date(2021, 6, 1), "qtd_frete_peso": 2},
        {"id": 74, "data_inicio": date(2021, 6, 1), "qtd_frete_peso": 2},
    ], c)
    assert escolhida["id"] == 76, "maior id — cadastro mais novo"
    assert Aviso.TABELA_AMBIGUA in c.avisos


def test_tabela_unica_nao_avisa():
    c = ctx()
    escolher_tabela([{"id": 1, "data_inicio": date(2021, 1, 1), "qtd_frete_peso": 5}], c)
    assert c.avisos == []


# ═══════════════════════════════════════════════ previsão de entrega
def test_sem_prazo_nao_ha_previsao():
    """28% das vigências vieram sem prazo. O preço vale; a data, não."""
    assert previsao_de_entrega(date(2026, 9, 29), None) is None


def test_sem_calendario_conta_dia_corrido():
    assert previsao_de_entrega(date(2026, 9, 29), 3) == date(2026, 10, 2)


def test_com_calendario_pula_o_que_nao_atende():
    """Só 634 das 152.141 vigências têm calendário, mas quando têm, vale."""
    so_dia_util = {"atende_seg": 1, "atende_ter": 1, "atende_qua": 1,
                   "atende_qui": 1, "atende_sex": 1, "atende_sab": 0, "atende_dom": 0}
    #  29/09/2026 é uma terça. Três dias úteis à frente: 30 (qua), 1 (qui), 2 (sex).
    assert previsao_de_entrega(date(2026, 9, 29), 3, so_dia_util) == date(2026, 10, 2)
    #  Cinco dias úteis atravessam o fim de semana: 30, 1, 2, então 5 (seg) e 6.
    assert previsao_de_entrega(date(2026, 9, 29), 5, so_dia_util) == date(2026, 10, 6)


def test_calendario_sem_nenhum_dia_nao_inventa_data():
    nunca = {d: 0 for d in ("atende_seg", "atende_ter", "atende_qua", "atende_qui",
                            "atende_sex", "atende_sab", "atende_dom")}
    assert previsao_de_entrega(date(2026, 9, 29), 3, nunca) is None


# ═══════════════════════════════════════════════ a cascata (precisa de banco)
#  `cur` e `precisa_db` vêm de conftest.py. A primeira versão disto abria a
#  conexão pela configuração do serviço, cujo padrão é MYSQL_HOST=db — o nome
#  do serviço dentro da rede do Docker, que não resolve da máquina. Dava um
#  traceback de 200 linhas do pymysql em vez de dizer o que arrumar.
from conftest import precisa_db  # noqa: F401,E402


@precisa_db
@pytest.mark.parametrize("origem,destino,aliquota,porque", [
    ("SP", "BA", "0.0700", "Sudeste -> Nordeste: o caso dos 7%"),
    ("SP", "AM", "0.0700", "Sudeste -> Norte"),
    ("SP", "GO", "0.0700", "Sudeste -> Centro-Oeste"),
    ("SP", "ES", "0.0700", "o ES entra junto com N/NE/CO quando é DESTINO"),
    ("RS", "PA", "0.0700", "Sul -> Norte"),
    ("SP", "RJ", "0.1200", "Sudeste -> Sudeste: não é o caso dos 7%"),
    ("SP", "RS", "0.1200", "Sudeste -> Sul"),
    ("CE", "SP", "0.1200", "saindo do Nordeste é 12% para qualquer destino"),
    ("CE", "BA", "0.1200", "Nordeste -> Nordeste também é 12%"),
    ("ES", "SP", "0.1200", "o ES é exceção quando é ORIGEM: não dá 7%"),
    ("ES", "BA", "0.1200", "nem para destino no Nordeste"),
])
def test_icms_interestadual_segue_a_resolucao_22(cur, origem, destino, aliquota, porque):
    """Resolução do Senado Federal nº 22/1989, art. 1º.

    A regra depende da ORIGEM, e é aí que é fácil errar — eu errei:

      7%   só quando a origem é Sul/Sudeste **exceto ES** e o destino é
           Norte, Nordeste, Centro-Oeste ou ES
      12%  todas as outras interestaduais

    Então "CE → BA é 7%" é falso: saindo do Nordeste, é 12% para todo lugar.
    A versão anterior deste teste afirmava os 7% e trocava origem por destino
    na própria docstring. O seed estava certo; o teste, não.
    """
    from motor_frete.resolucao import aliquota_icms

    assert aliquota_icms(cur, origem, destino, date.today()) == Decimal(aliquota), porque


@precisa_db
def test_uf_sem_icms_descarta(cur):
    from motor_frete.resolucao import aliquota_icms

    with pytest.raises(DescarteError) as e:
        aliquota_icms(cur, "XX", "SP", date.today())
    assert e.value.motivo is MotivoDescarte.SEM_ICMS_PARA_UF


@precisa_db
def test_perfil_de_generalidade_compila_as_formulas_reais(cur):
    """Pega um perfil que algum frete-peso usa e confere que as fórmulas dele
    compilam e avaliam."""
    from motor_frete.generalidades import avaliar_perfil, contexto_do_embarque
    from motor_frete.resolucao import perfil_de_generalidade

    cur.execute("""
        SELECT DISTINCT perfil_generalidade_id FROM tabelas_frete_peso
         WHERE perfil_generalidade_id IS NOT NULL LIMIT 5""")
    ids = [r["perfil_generalidade_id"] for r in cur.fetchall()]
    if not ids:
        pytest.skip("nenhum frete-peso aponta para perfil — carga não rodou?")

    for pid in ids:
        perfil = perfil_de_generalidade(cur, pid)
        assert perfil is not None, f"perfil {pid} veio sem fórmula"
        r = avaliar_perfil(perfil, contexto_do_embarque(
            valor_nf=Decimal("15000"), peso_para_calculo=Decimal("120.5"),
            tarcli="PDAC", tarcid="1268", uf_destino="SP", pallets=Decimal("4")))
        assert r.total_cotado >= 0


@precisa_db
def test_faixas_reais_sempre_tem_preco(cur):
    """Nenhuma linha de faixa tem `minimo` e `valor` vazios ao mesmo tempo —
    conferido nas 6.430. Se um dado novo quebrar isso, é para aparecer."""
    cur.execute("""
        SELECT COUNT(*) AS n FROM faixas_valores
         WHERE minimo IS NULL AND valor IS NULL""")
    assert cur.fetchone()["n"] == 0


@precisa_db
def test_resolve_uma_rota_de_ponta_a_ponta(cur):
    """O teste que vale por todos: pega uma rota que existe no banco e roda os
    onze passos."""
    from motor_frete.esquemas import Embarque
    from motor_frete.resolucao import resolver

    cur.execute("""
        SELECT lo.id AS o, ld.id AS d
          FROM regioes_atendidas ra
          JOIN localidades lo ON lo.id = ra.localidade_origem_id
          JOIN localidades ld ON ld.id = ra.localidade_destino_id
          JOIN tabelas_frete_v2 tf ON tf.transportadora_id = ra.transportadora_id
          JOIN tabelas_frete_peso fp ON fp.tabela_frete_id = tf.id
         LIMIT 1""")
    r = cur.fetchone()
    if not r:
        pytest.skip("banco sem rota com tarifa — a carga rodou?")

    #  Semeia o cache de CEP para não depender de consulta externa no teste.
    #  `origem` é ENUM: CACHE | FAIXA_LOCALIDADE | FAIXA_UF | CONSULTA_EXTERNA |
    #  MANUAL. 'MANUAL' é o valor honesto aqui — foi posto à mão. A primeira
    #  versão usava 'TESTE' e o MySQL recusou (1265, Data truncated).
    for cep, loc in (("60000001", r["o"]), ("60000002", r["d"])):
        cur.execute("""
            INSERT INTO ceps_resolvidos (cep, uf_id, localidade_id, origem, ultima_tentativa)
            SELECT %s, uf_id, id, 'MANUAL', NOW() FROM localidades WHERE id = %s
            ON DUPLICATE KEY UPDATE localidade_id = VALUES(localidade_id),
                                    origem = VALUES(origem)""", (cep, loc))

    contextos, descartadas = resolver(
        cur,
        Embarque(referencia="TESTE-1", origem_cep="60000001", destino_cep="60000002",
                 peso_bruto_kg=Decimal("120.5"), valor_nf=Decimal("15000")),
        date.today(), None)

    assert contextos or descartadas, "nem cotação nem descarte: a resolução não rodou"
    for c in contextos:
        assert c.preco_frete_peso > 0
        assert c.tabela_frete_id and c.tabela_frete_peso_id
        assert c.icms_de in ("TABELA", "ICMS_UF")
    for d in descartadas:
        assert d["motivo"] in set(MotivoDescarte), "todo descarte tem motivo conhecido"
