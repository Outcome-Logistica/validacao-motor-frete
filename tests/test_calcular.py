# -*- coding: utf-8 -*-
"""Testes de `calculo.calcular()` — a costura entre a resolução e a conta.

Cada caso monta um `Contexto` à mão (como a resolução o entregaria) e confere o
número final **escrito à mão** no teste. Sem banco: o que se testa aqui é a
aritmética e a montagem da resposta, não a consulta.

Os três pontos onde a costura pode dar errado, e que cada bloco cobre:

1. o VN2 já vem resolvido pela faixa — multiplicar pelo peso de novo cobraria
   duas vezes;
2. o VN3 agora vem de fórmula, não de número;
3. o `AdValore` da tarifa e a fórmula `adv` do perfil nunca somam juntos.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from motor_frete import generalidades as ger
from motor_frete.calculo import calcular, tarifa_do_contexto
from motor_frete.esquemas import Aviso, Embarque
from motor_frete.resolucao import Contexto, Faixas

HOJE = date(2026, 10, 1)


def embarque(**troca):
    base = dict(referencia="PED-1", origem_cep="60000000", destino_cep="20000000",
                peso_bruto_kg=Decimal("100"), volume_m3=Decimal("0"),
                valor_nf=Decimal("10000"), quantidade_volumes=4)
    base.update(troca)
    return Embarque(**base)


def contexto(*, preco=Decimal("250"), icms=Decimal("0"), perfil=None,
             peso=Decimal("100"), **troca) -> Contexto:
    c = Contexto(
        transportadora_id=7, transportadora="JAMEF",
        regiao_atendida_id=55, praca_id=10, praca="CAPITAL",
        modalidade_id=1, modalidade="RODOVIARIO",
        fator_cubagem=Decimal("300"), uf_destino="RJ",
        tabela_frete_id=201, nome_tabela="JAMEF - CE - RODOVIARIO - SAIDA",
        tabela_frete_peso_id=9722, faixa_valor_id=66597,
        peso_taxado=peso, preco_frete_peso=preco, icms=icms,
        perfil=perfil, prazo_dias=3, tarcli="AD",
    )
    for k, v in troca.items():
        setattr(c, k, v)
    return c


def perfil_de(**colunas) -> ger.Perfil:
    return ger.Perfil.de_linha("201", colunas)


# ═══════════════════════════════════════════════════ 1 · o VN2 já vem pronto
def test_o_preco_da_faixa_nao_e_multiplicado_pelo_peso_de_novo():
    """A resolução já converteu a faixa em reais. Se a tarifa entrasse como
    POR_KG, 100 kg × R$ 250 daria R$ 25.000 em vez de R$ 250."""
    t = tarifa_do_contexto(contexto(preco=Decimal("250"), peso=Decimal("100")))
    assert t.tipo_calculo == "FIXO"
    assert t.fp_valor == Decimal("250")

    c = calcular(embarque(), contexto(preco=Decimal("250")), HOJE)
    assert c.frete_peso == Decimal("250.00")
    assert c.frete_total == Decimal("250.00")


def test_frete_minimo_e_piso_do_frete_peso():
    """`Frete_Minimo` virou `fp_minimo` — é a pendência 7."""
    c = calcular(embarque(), contexto(preco=Decimal("40"),
                                      frete_minimo=Decimal("77.40")), HOJE)
    assert c.frete_peso == Decimal("77.40")
    linha = next(x for x in c.componentes if x.codigo == "FRETE_PESO")
    assert linha.aplicou_minimo is True, "a tela precisa saber que o mínimo pegou"


def test_frete_minimo_nao_pega_quando_o_frete_e_maior():
    c = calcular(embarque(), contexto(preco=Decimal("250"),
                                      frete_minimo=Decimal("77.40")), HOJE)
    assert c.frete_peso == Decimal("250.00")


# ═══════════════════════════════════════════════════ 2 · o VN3 vem de fórmula
def test_generalidade_de_formula_entra_no_total():
    """`gris = maximo(5,[fv]*0.002)` com nota de R$ 10.000 → R$ 20."""
    p = perfil_de(gris="maximo(5,[fv]*0.002)", tst="[gris]")
    c = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p), HOJE)
    assert c.generalidades == Decimal("20.00")
    assert c.frete_liquido == Decimal("270.00")
    assert c.frete_total == Decimal("270.00")


def test_componentes_saem_item_a_item():
    """É o que torna a conferência da auditoria um JOIN em vez de comparar
    totais."""
    p = perfil_de(gris="maximo(5,[fv]*0.002)", ctrc="1*15",
                  tst="[gris]+[ctrc]")
    c = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p), HOJE)
    por_codigo = {x.codigo: x.valor for x in c.componentes}
    assert por_codigo["FRETE_PESO"] == Decimal("250.00")
    assert por_codigo["GRIS"] == Decimal("20.00")
    assert por_codigo["CTRC"] == Decimal("15.00")
    assert c.generalidades == Decimal("35.00")


def test_formula_condicional_respeita_o_tarcli():
    """`se(contem('P',[tarcli]),[pallets]*29.68,0)` — o pallet só entra se o
    cliente contratou. É o que o `tarcli` liga e desliga."""
    p = perfil_de(pallet="se(contem('P',[tarcli]),[pallets]*29.68,0)",
                  tst="[pallet]")

    com_p = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p,
                                          tarcli="ADP"), HOJE)
    assert com_p.generalidades == Decimal("118.72")      # 4 volumes × 29,68

    sem_p = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p,
                                          tarcli="AD"), HOJE)
    assert sem_p.generalidades == Decimal("0.00")


def test_sem_tarcli_a_generalidade_condicional_zera_e_avisa():
    """O defeito que o aviso existe para denunciar: sem o código de serviços, o
    TDE sai zero e o frete fica barato em silêncio."""
    p = perfil_de(tde="se(contem('D',[tarcli]),maximo(150,[fv]*0.2),0)",
                  tst="[tde]")
    ctx = contexto(preco=Decimal("250"), perfil=p, tarcli=None)
    ctx.avisar(Aviso.SEM_CODIGO_SERVICOS)

    c = calcular(embarque(), ctx, HOJE)
    assert c.generalidades == Decimal("0.00")
    assert Aviso.SEM_CODIGO_SERVICOS in c.avisos, "o barato tem que vir explicado"


def test_formula_usa_a_uf_de_destino():
    """`[ufdest]` tem 297 usos. Sem ele, pedágio e SUFRAMA zeram."""
    p = perfil_de(pedagio="se(contem([ufdest],'DF, RJ, SP, MT'),"
                          "arredondar_para_cima0([peso_para_calculo]/100)*7,0)",
                  tst="[pedagio]")

    rj = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p,
                                       uf_destino="RJ"), HOJE)
    assert rj.generalidades == Decimal("7.00")           # 100 kg → 1 fração × 7

    ba = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p,
                                       uf_destino="BA"), HOJE)
    assert ba.generalidades == Decimal("0.00")


def test_eventual_nao_entra_no_total_mas_aparece_na_memoria():
    """Reentrega é cobrada se a entrega falhar — calculada, não somada."""
    p = perfil_de(gris="1*100", tst="[gris]", reentrega="[tst]*0.5")
    c = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p), HOJE)
    assert c.generalidades == Decimal("100.00")
    assert "REENTREGA" not in {x.codigo for x in c.componentes}
    assert c.memoria_calculo["eventuais"] == {"reentrega": "50.0"}


def test_pallets_vem_do_campo_proprio_quando_informado():
    """40 caixas podem ir em 2 pallets — volume e pallet não são a mesma coisa."""
    p = perfil_de(pallet="se(contem('P',[tarcli]),[pallets]*29.68,0)",
                  tst="[pallet]")
    c = calcular(embarque(quantidade_volumes=40, quantidade_pallets=2),
                 contexto(preco=Decimal("250"), perfil=p, tarcli="P"), HOJE)
    assert c.generalidades == Decimal("59.36")           # 2 × 29,68, não 40 ×
    assert Aviso.PALLETS_ESTIMADOS not in c.avisos


def test_sem_pallets_usa_volumes_e_avisa():
    p = perfil_de(pallet="se(contem('P',[tarcli]),[pallets]*29.68,0)",
                  tst="[pallet]")
    c = calcular(embarque(quantidade_volumes=4, quantidade_pallets=None),
                 contexto(preco=Decimal("250"), perfil=p, tarcli="P"), HOJE)
    assert c.generalidades == Decimal("118.72")          # 4 volumes, chutado
    assert Aviso.PALLETS_ESTIMADOS in c.avisos, "chute tem que vir dito"


def test_nao_avisa_pallets_quando_o_perfil_nao_cobra_por_pallet():
    """Aviso que aparece sempre é aviso que ninguém lê."""
    p = perfil_de(gris="maximo(5,[fv]*0.002)", tst="[gris]")
    c = calcular(embarque(quantidade_pallets=None),
                 contexto(preco=Decimal("250"), perfil=p), HOJE)
    assert Aviso.PALLETS_ESTIMADOS not in c.avisos


# ═══════════════════════════════════════════════════ 3 · o ad valorem
def test_advalore_da_tarifa_entra_como_componente():
    """Quando o frete-peso tem linha AdValore, o perfil não tem a fórmula
    `adv` — conferido nos 638 casos."""
    c = calcular(embarque(), contexto(preco=Decimal("250"),
                                      ad_valorem=Decimal("0.0032")), HOJE)
    assert c.generalidades == Decimal("32.00")           # 10.000 × 0,32%
    assert "ADVALORE" in {x.codigo for x in c.componentes}


def test_advalore_e_formula_adv_nunca_coexistem_no_dado():
    """Se um dia coexistirem, o ad valorem seria cobrado em dobro. O teste não
    impede — documenta o número, para a soma dupla aparecer se acontecer."""
    p = perfil_de(adv="[fv]*0.004", tst="[adv]")
    c = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p,
                                      ad_valorem=Decimal("0.0032")), HOJE)
    #  40 (fórmula) + 32 (faixa) = 72. No dado real isto não ocorre.
    assert c.generalidades == Decimal("72.00")


# ═══════════════════════════════════════════════════ a escada inteira
def test_vn1_a_vn6_fecham_na_ordem():
    """250 de frete, 50 de generalidade, ICMS de 12% sobre o líquido."""
    p = perfil_de(gris="1*50", tst="[gris]")
    c = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p,
                                      icms=Decimal("0.12")), HOJE)
    assert c.frete_peso == Decimal("250.00")
    assert c.generalidades == Decimal("50.00")
    assert c.sobre_frete == Decimal("0.00")      # a tarifa dela não tem VN4
    assert c.frete_liquido == Decimal("300.00")
    assert c.icms == Decimal("36.00")            # 300 × 0,12
    assert c.frete_total == Decimal("336.00")


def test_icms_vira_componente():
    c = calcular(embarque(), contexto(preco=Decimal("250"),
                                      icms=Decimal("0.12")), HOJE)
    icms = next(x for x in c.componentes if x.codigo == "ICMS")
    assert icms.valor == Decimal("30.00")
    assert icms.base_calculo == Decimal("250.00")


def test_peso_cubado_vence_o_peso_real():
    """VN1: 1 m³ × 300 = 300 kg contra 100 kg reais."""
    c = calcular(embarque(volume_m3=Decimal("1")),
                 contexto(preco=Decimal("250"), peso=Decimal("300")), HOJE)
    assert c.peso_taxado_kg == Decimal("300.00")


def test_tudo_decimal_no_resultado():
    """Dinheiro em float acumula centavo, e o ponto do motor único é os dois
    lados fecharem o MESMO número."""
    c = calcular(embarque(), contexto(preco=Decimal("250"),
                                      icms=Decimal("0.12")), HOJE)
    for v in (c.peso_taxado_kg, c.frete_peso, c.generalidades,
              c.sobre_frete, c.icms, c.frete_liquido, c.frete_total):
        assert isinstance(v, Decimal)


# ═══════════════════════════════════════════════════ rastreio, prazo e memória
def test_rastreio_aponta_as_linhas_do_banco():
    """Sem isto, um valor contestado não tem como ser defendido."""
    c = calcular(embarque(), contexto(preco=Decimal("250")), HOJE)
    r = c.rastreio
    assert r.tabela_frete_id == 201
    assert r.tabela_frete_peso_id == 9722
    assert r.faixa_valor_id == 66597
    assert r.praca == "CAPITAL"
    assert r.nome_tabela
    assert r.icms_de in ("TABELA", "ICMS_UF")


def test_previsao_de_entrega_sai_do_prazo():
    c = calcular(embarque(), contexto(preco=Decimal("250")), HOJE)
    assert c.prazo_dias == 3
    assert c.previsao_entrega == date(2026, 10, 4)


def test_sem_prazo_sem_previsao():
    c = calcular(embarque(), contexto(preco=Decimal("250"), prazo_dias=None), HOJE)
    assert c.previsao_entrega is None


def test_memoria_traz_a_escada_e_as_formulas():
    p = perfil_de(gris="maximo(5,[fv]*0.002)", tst="[gris]")
    c = calcular(embarque(), contexto(preco=Decimal("250"), perfil=p,
                                      icms=Decimal("0.12")), HOJE)
    m = c.memoria_calculo
    assert m["VN2_frete_peso"] == "250.00"
    assert m["VN3_generalidades"] == "20.00"
    assert m["VN6_total"] == "302.40"
    assert m["politica_arredondamento"] == "POR_NIVEL"
    assert m["tarcli"] == "AD"
    formulas = {f["coluna"]: f["expressao"] for f in m["formulas"]}
    assert formulas["gris"] == "maximo(5,[fv]*0.002)"


def test_memoria_pode_ser_desligada():
    """Em lote, a memória de N embarques × M transportadoras fica grande."""
    c = calcular(embarque(), contexto(preco=Decimal("250")), HOJE,
                 incluir_memoria=False)
    assert c.memoria_calculo is None


def test_avisos_viajam_com_a_cotacao():
    ctx = contexto(preco=Decimal("250"))
    ctx.avisar(Aviso.TARIFA_AMBIGUA)
    ctx.avisar(Aviso.ICMS_ESTIMADO)
    c = calcular(embarque(), ctx, HOJE)
    assert Aviso.TARIFA_AMBIGUA in c.avisos
    assert Aviso.ICMS_ESTIMADO in c.avisos


def test_sem_perfil_cota_sem_generalidade():
    """12 dos 1.018 frete-peso não apontam para perfil. Cotam, com VN3 zero."""
    c = calcular(embarque(), contexto(preco=Decimal("250"), perfil=None), HOJE)
    assert c.generalidades == Decimal("0.00")
    assert c.frete_total == Decimal("250.00")
    assert c.memoria_calculo["formulas"] is None
