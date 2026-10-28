# -*- coding: utf-8 -*-
"""Testes do VN3 — a avaliação de um perfil de generalidades.

O teste que importa é o último: os 238 perfis reais, avaliados de ponta a ponta
com um embarque plausível. Ele pega o que nenhum caso montado à mão pega —
ordem furada, variável que ninguém definiu, perfil com forma diferente.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from motor_frete.generalidades import (
    COLUNAS_COMPONENTE, DependenciaCircular, Perfil, avaliar_perfil,
    contexto_do_embarque,
)

CORPUS = Path(__file__).parent / "fixtures" / "formulas_generalidades.json"


@pytest.fixture(scope="module")
def corpus():
    if not CORPUS.exists():
        pytest.skip(f"corpus ausente: {CORPUS.name}")
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def embarque():
    return contexto_do_embarque(
        valor_nf=Decimal("15000.00"),
        peso_para_calculo=Decimal("120.5"),
        tarcli="PDAC",
        tarcid="1268",
        uf_destino="SP",
        pallets=Decimal("4"),
        cnpj_terceiro_destino=None,
    )


# ═══════════════════════════════════════════════════════════════ o perfil
def test_coluna_vazia_e_ausencia_nao_zero():
    perfil = Perfil.de_linha("1", {"gris": "1*10", "adv": None, "pedagio": "",
                                   "tst": "[gris]+[adv]+[pedagio]"})
    assert set(perfil.expressoes) == {"gris", "tst"}
    r = avaliar_perfil(perfil, embarque())
    assert "adv" not in r.valores, "coluna sem fórmula não vira componente"
    assert r.tst == Decimal("10"), "e vale 0 dentro do tst"


def test_cubagem_nao_e_generalidade():
    """`cubagem` é o fator de cubagem (o 300 de "300 kg por m³"), e quem usa é
    o VN1. Não pode virar cobrança."""
    perfil = Perfil.de_linha("1", {"cubagem": "300", "gris": "1*10", "tst": "[gris]"})
    assert perfil.fator_cubagem == Decimal("300")
    assert "cubagem" not in perfil.expressoes
    r = avaliar_perfil(perfil, embarque())
    assert r.total_cotado == Decimal("10")


def test_ordem_poe_o_tst_antes_de_quem_o_le():
    perfil = Perfil.de_linha("1", {
        "gris": "1*10", "tst": "[gris]", "reentrega": "[tst]*0.5"})
    ordem = perfil.ordem_de_avaliacao()
    assert ordem.index("tst") < ordem.index("reentrega")
    assert ordem.index("gris") < ordem.index("tst")


def test_ciclo_de_verdade_levanta():
    """Não existe no dado de hoje. Se aparecer, tem que aparecer alto."""
    perfil = Perfil.de_linha("1", {"tst": "[trt]", "trt": "[tst]*0.08"})
    with pytest.raises(DependenciaCircular) as e:
        perfil.ordem_de_avaliacao()
    assert "trt" in str(e.value)


def test_ordem_e_estavel():
    """Duas execuções, mesma memória de cálculo — senão o diff contra o CT-e
    fica ilegível."""
    linha = {"gris": "1*10", "adv": "1*5", "pedagio": "1*7",
             "tst": "[gris]+[adv]+[pedagio]"}
    a = Perfil.de_linha("1", linha).ordem_de_avaliacao()
    b = Perfil.de_linha("1", linha).ordem_de_avaliacao()
    assert a == b


# ═══════════════════════════════════════════════════════════════ o total
def test_total_e_o_tst_quando_tudo_esta_dentro():
    perfil = Perfil.de_linha("1", {
        "gris": "1*10", "adv": "1*5", "tst": "[gris]+[adv]"})
    r = avaliar_perfil(perfil, embarque())
    assert r.tst == Decimal("15")
    assert r.total_cotado == Decimal("15")


def test_cobranca_fora_do_tst_soma_por_cima():
    """Caso real do perfil 234: `ctrc = [tst]*0.0174`, e o tst dele omite
    [ctrc]. É cobrança sobre o total, não substituição — ignorar barateia a
    cotação em silêncio."""
    perfil = Perfil.de_linha("234", {
        "gris": "1*100", "ctrc": "[tst]*0.0174", "tst": "[gris]"})
    r = avaliar_perfil(perfil, embarque())
    assert r.tst == Decimal("100")
    assert r.valores["ctrc"] == Decimal("1.7400")
    assert r.total_cotado == Decimal("101.7400")


def test_icm_iss_tambem_incide_sobre_o_tst():
    """Perfil 260: `icm_iss = [tst]*0.04`, fora do tst dele."""
    perfil = Perfil.de_linha("260", {
        "gris": "1*200", "icm_iss": "[tst]*0.04", "tst": "[gris]"})
    r = avaliar_perfil(perfil, embarque())
    assert r.total_cotado == Decimal("208.00")


def test_eventual_sai_separado_e_calculado():
    """A tela precisa mostrar "se houver reentrega, +R$ X" — então o número é
    calculado, só não somado."""
    perfil = Perfil.de_linha("1", {
        "gris": "1*100", "tst": "[gris]",
        "reentrega": "[tst]*0.5", "devolucao": "[tst]*1"})
    r = avaliar_perfil(perfil, embarque())
    assert r.total_cotado == Decimal("100")
    assert r.eventuais == {"reentrega": Decimal("50.0"),
                           "devolucao": Decimal("100")}
    assert all(not m.entra_no_total for m in r.memoria if m.eventual)


def test_lista_de_eventuais_e_substituivel():
    """O gancho para quando a resposta dela chegar: muda num lugar só."""
    perfil = Perfil.de_linha("1", {
        "gris": "1*100", "tst": "[gris]", "reentrega": "[tst]*0.5"})
    r = avaliar_perfil(perfil, embarque(), eventuais=())
    assert r.total_cotado == Decimal("150.0"), "reentrega passou a somar"
    assert r.eventuais == {}


# ═══════════════════════════════════════════════════════════ memória
def test_memoria_traz_expressao_e_valor():
    """Sem isto, um valor contestado não tem como ser defendido."""
    perfil = Perfil.de_linha("1", {"gris": "maximo(5,[fv]*0.002)", "tst": "[gris]"})
    r = avaliar_perfil(perfil, embarque())
    linha = next(m for m in r.memoria if m.coluna == "gris")
    assert linha.expressao == "maximo(5,[fv]*0.002)"
    assert linha.valor == Decimal("30.000")       # 15.000 × 0,2%
    assert linha.entra_no_total is True


def test_componentes_sao_so_os_que_somam():
    perfil = Perfil.de_linha("1", {
        "gris": "1*10", "tst": "[gris]", "reentrega": "[tst]*0.5"})
    cols = {m.coluna for m in avaliar_perfil(perfil, embarque()).componentes()}
    assert "reentrega" not in cols
    assert {"gris", "tst"} <= cols


# ═══════════════════════════════════════════════════ os 238 perfis reais
def test_os_238_perfis_avaliam_de_ponta_a_ponta(corpus):
    """O teste que vale por todos os outros."""
    ctx = embarque()
    falhas = []
    for linha in corpus["perfis"]:
        try:
            perfil = Perfil.de_linha(linha["id_generalidade"], linha)
            r = avaliar_perfil(perfil, ctx)
            assert r.total_cotado >= 0, "total negativo"
            assert r.total_cotado >= r.tst or r.tst < 0
        except Exception as erro:
            falhas.append((linha["id_generalidade"], f"{type(erro).__name__}: {erro}"))
    assert not falhas, f"{len(falhas)} perfis falharam; 3 primeiros: {falhas[:3]}"


def test_nenhum_perfil_real_tem_ciclo(corpus):
    for linha in corpus["perfis"]:
        Perfil.de_linha(linha["id_generalidade"], linha).ordem_de_avaliacao()


def test_todo_componente_do_corpus_e_coluna_conhecida(corpus):
    """Se a planilha ganhar coluna nova, é para falhar aqui."""
    conhecidas = set(COLUNAS_COMPONENTE) | {"tst", "cubagem", "obs",
                                            "id_generalidade",
                                            "generalidades_nome1",
                                            "generalidades_nome2"}
    for linha in corpus["perfis"]:
        assert set(linha) <= conhecidas, f"coluna nova: {set(linha) - conhecidas}"


def test_o_total_muda_com_o_embarque(corpus):
    """Sanidade: se o total não reagir ao valor da nota, alguma coisa está
    devolvendo constante."""
    barato = contexto_do_embarque(valor_nf=Decimal("100"),
                                  peso_para_calculo=Decimal("10"),
                                  tarcli="PDAC", tarcid="1268",
                                  uf_destino="SP", pallets=Decimal("1"))
    caro = contexto_do_embarque(valor_nf=Decimal("500000"),
                                peso_para_calculo=Decimal("10"),
                                tarcli="PDAC", tarcid="1268",
                                uf_destino="SP", pallets=Decimal("1"))
    mudou = 0
    for linha in corpus["perfis"]:
        perfil = Perfil.de_linha(linha["id_generalidade"], linha)
        if avaliar_perfil(perfil, caro).total_cotado > \
           avaliar_perfil(perfil, barato).total_cotado:
            mudou += 1
    assert mudou > 200, f"só {mudou} de 238 perfis reagiram ao valor da nota"
