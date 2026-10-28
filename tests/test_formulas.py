# -*- coding: utf-8 -*-
"""Testes do interpretador de fórmulas.

Três camadas, de fora para dentro:

1. **O corpus inteiro** — as 2.770 expressões reais de `tb_generalidades` têm
   que compilar, e as 679 distintas têm que avaliar sem erro. É o teste que
   pega gramática incompleta: se a parceira usa algo que eu não previ, falha
   aqui e não em produção.
2. **Cada construção da linguagem** — uma asserção por função e por operador,
   com número esperado escrito à mão.
3. **Os erros** — o que tem que levantar. Interpretador que devolve 0 no que não
   entende é o pior defeito possível: barateia a cotação em silêncio.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from motor_frete.formulas import (
    ErroDeFormula, ErroDeSintaxe, FuncaoDesconhecida, VariavelIndefinida,
    avaliar, avaliar_numero, compilar, variaveis_de,
)

CORPUS = Path(__file__).parent / "fixtures" / "formulas_generalidades.json"


@pytest.fixture(scope="module")
def corpus():
    if not CORPUS.exists():
        pytest.skip(f"corpus ausente: {CORPUS.name}")
    return json.loads(CORPUS.read_text(encoding="utf-8"))


#  Um embarque plausível, com toda variável que o corpus cita. Serve para
#  avaliar as 679 expressões distintas de ponta a ponta.
EMBARQUE = {
    "fv": Decimal("15000.00"),
    "peso_para_calculo": Decimal("120.5"),
    "tarcli": "PDAC",
    "tarcid": "1268",
    "ufdest": "SP",
    "pallets": Decimal("4"),
    "cnpj_terceiro_destino": None,
    # as colunas do próprio perfil, como se já avaliadas
    **{c: Decimal("10") for c in (
        "ctrc", "adv", "gris", "pedagio", "coleta", "entrega", "pallet",
        "agendar", "tde", "trt", "tas", "suframa", "segfluvial", "icm_iss",
        "generalidade1", "generalidade2", "outros1", "outros2", "tst",
        "reentrega", "devolucao", "fluvial")},
}


# ═══════════════════════════════════════════════════════════ 1. o corpus
def test_todas_as_expressoes_do_corpus_compilam(corpus):
    """2.770 expressões reais. Nenhuma pode ficar de fora da gramática."""
    falhas = []
    for e in corpus["expressoes"]:
        try:
            compilar(e["expressao"])
        except ErroDeFormula as erro:
            falhas.append((e["perfil"], e["coluna"], e["expressao"], str(erro)))
    assert not falhas, f"{len(falhas)} não compilaram; 3 primeiras: {falhas[:3]}"


def test_todas_as_expressoes_distintas_avaliam(corpus):
    """Compilar não basta: tem que produzir número."""
    distintas = {e["expressao"] for e in corpus["expressoes"]}
    assert len(distintas) > 600, "corpus pequeno demais — fixture truncado?"

    falhas = []
    for fonte in sorted(distintas):
        try:
            v = avaliar_numero(fonte, EMBARQUE)
            assert isinstance(v, Decimal)
        except Exception as erro:
            falhas.append((fonte, f"{type(erro).__name__}: {erro}"))
    assert not falhas, f"{len(falhas)} não avaliaram; 3 primeiras: {falhas[:3]}"


def test_a_gramatica_nao_tem_funcao_fora_das_seis(corpus):
    """Se aparecer uma sétima função num dado novo, é para descobrir aqui."""
    import re

    from motor_frete.formulas import FUNCOES

    achadas = set()
    for e in corpus["expressoes"]:
        sem_texto = re.sub(r"'(?:[^']|'')*'", "''", e["expressao"])
        achadas |= {m.lower() for m in
                    re.findall(r"([A-Za-z_][A-Za-z_0-9]*)\s*\(", sem_texto)}
    assert achadas <= set(FUNCOES), f"função nova no corpus: {achadas - set(FUNCOES)}"


def test_nenhuma_expressao_devolve_float(corpus):
    """Dinheiro em float acumula centavo. Nada de float entra nem sai."""
    for fonte in sorted({e["expressao"] for e in corpus["expressoes"]})[:200]:
        v = avaliar(fonte, EMBARQUE)
        assert not isinstance(v, float), fonte


# ═══════════════════════════════════════════════════════ 2. a linguagem
def test_numero_e_aritmetica():
    assert avaliar_numero("1*15") == Decimal("15")
    assert avaliar_numero("300") == Decimal("300")
    assert avaliar_numero("(([p]-10)*0.32)+16.91", {"p": Decimal("60")}) == Decimal("32.91")


def test_precedencia_produto_antes_de_soma():
    assert avaliar_numero("2+3*4") == Decimal("14")
    assert avaliar_numero("(2+3)*4") == Decimal("20")


def test_divisao_e_exata_em_decimal():
    """`[peso]/100` alimenta o teto do pedágio — não pode chegar lá com resíduo
    de float."""
    assert avaliar_numero("1/8") == Decimal("0.125")


def test_variavel_entre_colchetes():
    assert avaliar_numero("[fv]*0.004", {"fv": Decimal("15000")}) == Decimal("60.000")


def test_variavel_nula_vale_zero_na_soma():
    """É o que faz o `tst` fechar: metade das colunas do perfil vem vazia."""
    ctx = {"a": Decimal("10"), "b": None, "c": Decimal("5")}
    assert avaliar_numero("[a]+[b]+[c]", ctx) == Decimal("15")


def test_maximo_e_o_piso_do_gris():
    """`maximo(5,[fv]*0.002)`: 0,2% do valor, nunca menos de R$ 5."""
    assert avaliar_numero("maximo(5,[fv]*0.002)", {"fv": Decimal("1000")}) == Decimal("5")
    assert avaliar_numero("maximo(5,[fv]*0.002)", {"fv": Decimal("10000")}) == Decimal("20.000")


def test_minimo_e_teto():
    assert avaliar_numero("minimo(4,[p]*100)", {"p": Decimal("0.01")}) == Decimal("1.00")
    assert avaliar_numero("minimo(4,[p]*100)", {"p": Decimal("1")}) == Decimal("4")


def test_arredondar_para_cima0_e_teto_inteiro():
    """101 kg paga 2 frações de 100 kg — é assim que o pedágio conta."""
    assert avaliar_numero("arredondar_para_cima0([p]/100)", {"p": Decimal("101")}) == Decimal("2")
    assert avaliar_numero("arredondar_para_cima0([p]/100)", {"p": Decimal("100")}) == Decimal("1")
    assert avaliar_numero("arredondar_para_cima0([p]/100)*7", {"p": Decimal("250")}) == Decimal("21")


def test_se_escolhe_o_ramo():
    assert avaliar_numero("se([p]>100,1*54.34,0)", {"p": Decimal("150")}) == Decimal("54.34")
    assert avaliar_numero("se([p]>100,1*54.34,0)", {"p": Decimal("50")}) == Decimal("0")


def test_se_nao_avalia_o_ramo_morto():
    """`se(falso, [tst]*0.5, 0)` não pode exigir [tst] — em muitos perfis a
    coluna que o cita é exatamente a que fica fora do total."""
    assert avaliar_numero("se([p]>100,[tst]*0.5,0)", {"p": Decimal("1")}) == Decimal("0")


def test_contem_procura_a_agulha_no_palheiro():
    """A agulha vem PRIMEIRO — confirmado nas duas formas de uso do corpus."""
    assert avaliar("contem('P',[tarcli])", {"tarcli": "PDAC"}) is True
    assert avaliar("contem('X',[tarcli])", {"tarcli": "PDAC"}) is False
    assert avaliar("contem([ufdest],'DF, RJ, SP, MT')", {"ufdest": "SP"}) is True
    assert avaliar("contem([ufdest],'DF, RJ, SP, MT')", {"ufdest": "BA"}) is False


def test_contem_ignora_caixa_e_espaco_em_volta():
    assert avaliar("contem([ufdest],'ac, ro, ba')", {"ufdest": "BA"}) is True
    assert avaliar("contem([u],'AC, RO, BA')", {"u": " ba "}) is True


def test_contem_com_nulo_e_falso():
    """Cliente sem código de serviço não paga pallet, agendamento nem TDE."""
    assert avaliar("contem('P',[tarcli])", {"tarcli": None}) is False
    assert avaliar_numero("se(contem('P',[tarcli]),[pallets]*29.68,0)",
                          {"tarcli": None, "pallets": Decimal("4")}) == Decimal("0")


def test_ou_e_disjuncao():
    ctx = {"tarcid": "268", "p": Decimal("50")}
    assert avaliar("ou(contem('1',[tarcid]),[p]>30)", ctx) is True
    assert avaliar("ou(contem('1',[tarcid]),[p]>100)", ctx) is False


def test_comparador_diferente_de_null():
    """`[cnpj_terceiro_destino]<>null` pergunta se o campo está preenchido."""
    assert avaliar("se([c]<>null,1*150,0)", {"c": "12345678000199"}) == Decimal("150")
    assert avaliar("se([c]<>null,1*150,0)", {"c": None}) == Decimal("0")


def test_texto_com_maior_que_dentro_das_aspas():
    """'PFP>000001' tem `>` no meio. O tokenizador não pode confundir com
    comparador."""
    ctx = {"tarcli": "PFP>000001", "p": Decimal("5"), "pallets": Decimal("3")}
    assert avaliar_numero(
        "se(ou(contem('PFP>000001',[tarcli]),[p]>1),[pallets]*0.8,0)", ctx
    ) == Decimal("2.4")


def test_lista_de_ufs_com_virgula_sobrando():
    """Caso real do corpus: espaço à frente e vírgula no fim."""
    assert avaliar(
        "contem([u],' AC, RO, BA, ES, MG, RJ, SP, PR, RS, SC, DF, GO, MS, MT,')",
        {"u": "MT"}) is True


def test_variaveis_de_lista_as_dependencias():
    assert variaveis_de("maximo(5,[fv]*0.002)") == frozenset({"fv"})
    assert variaveis_de("[tst]*0.5") == frozenset({"tst"})
    assert variaveis_de("1*15") == frozenset()
    assert "outros1" in variaveis_de("[ctrc]+[outros1]+[outros2]")


def test_compilar_reaproveita():
    """679 expressões distintas, milhares de avaliações num lote."""
    assert compilar("maximo(5,[fv]*0.002)") is compilar("maximo(5,[fv]*0.002)")


# ═══════════════════════════════════════════════════════════ 3. os erros
def test_variavel_ausente_levanta():
    """O erro que denuncia ordem de avaliação errada. Não pode virar 0."""
    with pytest.raises(VariavelIndefinida) as e:
        avaliar_numero("[tst]*0.5", {})
    assert "tst" in str(e.value)


def test_funcao_desconhecida_levanta():
    with pytest.raises(FuncaoDesconhecida):
        avaliar_numero("arredondar_para_baixo([p])", {"p": Decimal("1")})


def test_sintaxe_invalida_levanta():
    for ruim in ("maximo(5,", "1 +", "", "se(1,2)3", "[fv", "@", "2 ** 3"):
        with pytest.raises(ErroDeFormula):
            compilar(ruim)


def test_variavel_sem_colchete_e_erro_explicito():
    """Erro de digitação comum. A mensagem tem que dizer o que fazer."""
    with pytest.raises(ErroDeSintaxe) as e:
        compilar("fv*0.004")
    assert "colchete" in str(e.value)


def test_aridade_errada_levanta():
    with pytest.raises(ErroDeFormula):
        avaliar_numero("se(1,2)")
    with pytest.raises(ErroDeFormula):
        avaliar_numero("contem('P')")
    with pytest.raises(ErroDeFormula):
        avaliar_numero("arredondar_para_cima0(1,2)")


def test_float_no_contexto_e_recusado():
    """Se um float entrar pelo contexto, o motor perde a garantia toda."""
    with pytest.raises(ErroDeFormula) as e:
        avaliar_numero("[fv]*0.004", {"fv": 15000.0})
    assert "float" in str(e.value)


def test_divisao_por_zero_levanta():
    with pytest.raises(ErroDeFormula):
        avaliar_numero("[p]/0", {"p": Decimal("1")})


def test_condicao_como_valor_levanta():
    """`gris = contem(...)` é cadastro errado, não R$ 1,00."""
    with pytest.raises(ErroDeFormula):
        avaliar_numero("contem('P',[t])", {"t": "P"})
