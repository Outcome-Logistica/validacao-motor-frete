# -*- coding: utf-8 -*-
"""Um teste por suposição pendente de resposta da parceira.

Não é teste de correção — nenhum destes números foi confirmado por ela. É teste
de **amarração**: cada suposição está escrita num lugar só, e mudá-la quebra um
teste com nome que diz qual pergunta foi respondida. Quando as respostas
chegarem, o roteiro é: editar `pendencias.py`, rodar, e os testes que falharem
apontam o que mais precisa mudar.

Onde o corpus consegue provar a suposição, o teste prova — e aí deixa de ser
suposição. É o caso da pendência 1.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from motor_frete import generalidades as g
from motor_frete import pendencias as p

CORPUS = Path(__file__).parent / "fixtures" / "formulas_generalidades.json"


@pytest.fixture(scope="module")
def corpus():
    if not CORPUS.exists():
        pytest.skip(f"corpus ausente: {CORPUS.name}")
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _vars_de(texto: str | None) -> set[str]:
    return set(re.findall(r"\[([^\]]+)\]", texto or ""))


# ═══════════════════════════════════════════════════════ 1. ordem de avaliação
def test_pendencia_1_ordem_suposta():
    assert p.ORDEM_AVALIACAO is p.OrdemAvaliacao.ORDEM_DAS_DEPENDENCIAS


def test_pendencia_1_o_corpus_prova_que_o_grafo_e_aciclico(corpus):
    """A evidência que quase tira esta da lista de suposições.

    Nos 238 perfis × 19 colunas: toda vez que a fórmula de uma coluna lê
    `[tst]`, o `tst` daquele perfil deixa essa coluna de fora. Se um dado novo
    quebrar isso, é para descobrir aqui.
    """
    violacoes = []
    for perfil in corpus["perfis"]:
        dentro = {p.ALIAS_VARIAVEIS.get(v, v) for v in _vars_de(perfil.get("tst"))}
        for col in g.COLUNAS_COMPONENTE:
            if "tst" in _vars_de(perfil.get(col)) and col in dentro:
                violacoes.append((perfil["id_generalidade"], col))
    assert not violacoes, f"ciclo real no dado: {violacoes[:5]}"


def test_pendencia_1_ordem_topologica_respeita_as_dependencias(corpus):
    """E a ordem que produzimos tem que ser mesmo topológica, nos 238."""
    for linha in corpus["perfis"]:
        perfil = g.Perfil.de_linha(linha["id_generalidade"], linha)
        ordem = perfil.ordem_de_avaliacao()
        vistas: set[str] = set()
        for col in ordem:
            faltando = perfil.dependencias(col) & set(ordem) - vistas
            assert not faltando, (
                f"perfil {perfil.id}: {col} avaliada antes de {sorted(faltando)}")
            vistas.add(col)


# ═══════════════════════════════════════════════════════════ 2. excedente
def test_pendencia_2_modo_excedente_suposto():
    """Última faixa + excesso × tarifa. A outra leitura dá número PRÓXIMO, o
    que é pior — o erro não salta aos olhos."""
    assert p.MODO_EXCEDENTE is p.ModoExcedente.ULTIMA_FAIXA_MAIS_EXCESSO


# ═══════════════════════════════════════════════════════════════ 3. [fv]
def test_pendencia_3_fv_e_o_valor_da_nota():
    assert p.VARIAVEL_FV == "valor_nf"


def test_pendencia_3_fv_chega_pelo_contexto_do_embarque():
    ctx = g.contexto_do_embarque(valor_nf=Decimal("15000"))
    assert ctx["fv"] == Decimal("15000")


def test_pendencia_3_fv_e_muito_usada(corpus):
    """567 usos. Se a suposição estiver errada, erra quase toda generalidade
    percentual — GRIS, ad valorem, TDE, TRT."""
    usos = sum(1 for e in corpus["expressoes"] if "fv" in _vars_de(e["expressao"]))
    assert usos > 500, f"só {usos} usos de [fv] — corpus mudou?"


# ═══════════════════════════════════════════════════════════════ 4. [tarcid]
def test_pendencia_4_tarcid_vem_da_vigencia_da_rota():
    assert p.VARIAVEL_TARCID == "regioes_atendidas_vigencia.tar_cid"


def test_pendencia_4_tarcid_chega_pelo_contexto_do_embarque():
    ctx = g.contexto_do_embarque(tarcid="1268")
    assert ctx["tarcid"] == "1268"


# ═══════════════════════════════════════════════════ 5. [outros1] / [outros2]
def test_pendencia_5_apelidos_supostos():
    assert p.ALIAS_VARIAVEIS == {"outros1": "generalidade1",
                                 "outros2": "generalidade2"}


def test_pendencia_5_sem_o_apelido_o_tst_nao_fecha(corpus):
    """A prova de que [outros1]/[outros2] têm que virar coluna: são as únicas
    variáveis do `tst` que não são nome de coluna."""
    colunas = set(g.COLUNAS_COMPONENTE) | {"tst"}
    orfas: set[str] = set()
    for perfil in corpus["perfis"]:
        orfas |= _vars_de(perfil.get("tst")) - colunas
    assert orfas == {"outros1", "outros2"}, f"órfãs no tst: {sorted(orfas)}"


def test_pendencia_5_o_apelido_alimenta_o_tst():
    """generalidade1 avaliada tem que chegar ao `tst` como [outros1]."""
    perfil = g.Perfil.de_linha("teste", {
        "gris": "1*10",
        "generalidade1": "1*7",
        "tst": "[gris]+[outros1]",
    })
    r = g.avaliar_perfil(perfil, g.contexto_do_embarque())
    assert r.tst == Decimal("17")


# ═══════════════════════════════════════════════════════════ 6. eventuais
def test_pendencia_6_eventuais_supostos():
    assert p.COMPONENTES_EVENTUAIS == frozenset({"reentrega", "devolucao", "fluvial"})


def test_pendencia_6_sao_as_unicas_que_nunca_entram_em_nenhum_tst(corpus):
    """A evidência da suposição — e a correção da versão anterior, que também
    listava generalidade1 e generalidade2."""
    dentro_de_algum: set[str] = set()
    for perfil in corpus["perfis"]:
        dentro_de_algum |= {p.ALIAS_VARIAVEIS.get(v, v)
                            for v in _vars_de(perfil.get("tst"))}

    nunca = set(g.COLUNAS_COMPONENTE) - dentro_de_algum
    assert nunca == p.COMPONENTES_EVENTUAIS, (
        f"nunca entram: {sorted(nunca)} · supostas eventuais: "
        f"{sorted(p.COMPONENTES_EVENTUAIS)}")


def test_pendencia_6_generalidade1_e_2_entram_sim(corpus):
    """A correção de 26/09, travada: elas entram, via [outros1]/[outros2]."""
    conta = {"generalidade1": 0, "generalidade2": 0}
    for perfil in corpus["perfis"]:
        vs = _vars_de(perfil.get("tst"))
        for apelido, col in p.ALIAS_VARIAVEIS.items():
            if apelido in vs and perfil.get(col):
                conta[col] += 1
    assert conta["generalidade1"] > 100
    assert conta["generalidade2"] > 50


def test_pendencia_6_eventual_nao_soma_no_total():
    perfil = g.Perfil.de_linha("teste", {
        "gris": "1*10",
        "reentrega": "[tst]*0.5",
        "tst": "[gris]",
    })
    r = g.avaliar_perfil(perfil, g.contexto_do_embarque())
    assert r.total_cotado == Decimal("10")
    assert r.eventuais == {"reentrega": Decimal("5.0")}


# ═══════════════════════════════════════════════════ 7. Frete_Minimo
def test_pendencia_7_frete_minimo_suposto():
    assert p.MODO_FRETE_MINIMO is p.ModoFreteMinimo.PISO_DO_FRETE_PESO


def test_pendencia_7_vai_para_fp_minimo_e_nao_para_o_total():
    """A diferença entre as duas leituras: como piso do frete-peso, as
    generalidades e o ICMS somam POR CIMA do mínimo; como piso do total, elas
    ficariam engolidas por ele."""
    from decimal import Decimal

    from motor_frete.calculo import tarifa_do_contexto
    from motor_frete.resolucao import Contexto

    ctx = Contexto(
        transportadora_id=1, transportadora="T", regiao_atendida_id=1,
        praca_id=0, praca="INTERIOR", modalidade_id=1, modalidade="R",
        fator_cubagem=Decimal("300"), preco_frete_peso=Decimal("40"),
        frete_minimo=Decimal("77.40"))

    t = tarifa_do_contexto(ctx)
    assert t.fp_minimo == Decimal("77.40"), "piso do frete-peso"
    assert t.minimo_geral == Decimal("0"), "não é piso do total"


# ═══════════════════════════════════════════════════════════════ o resumo
def test_resumo_traz_todas_numeradas_em_sequencia():
    """Eram seis; a carga do dado real trouxe a sétima (o Frete_Minimo)."""
    r = p.resumo()
    assert [x["n"] for x in r] == list(range(1, len(r) + 1))
    assert len(r) >= 7
    for x in r:
        assert x["assunto"] and x["suposto"] and x["impacto"]


def test_resumo_acompanha_as_constantes():
    """O resumo vai para o /health e para os relatórios. Não pode envelhecer
    em silêncio quando a constante mudar."""
    por_n = {x["n"]: x["suposto"] for x in p.resumo()}
    assert por_n[1] == p.ORDEM_AVALIACAO.value
    assert por_n[2] == p.MODO_EXCEDENTE.value
    assert por_n[3] == p.VARIAVEL_FV
    assert por_n[4] == p.VARIAVEL_TARCID
    assert por_n[6] == ", ".join(sorted(p.COMPONENTES_EVENTUAIS))
    assert por_n[7] == p.MODO_FRETE_MINIMO.value
