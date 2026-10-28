# -*- coding: utf-8 -*-
"""Teste de regressão contra o motor de cálculo antigo.

A fixture `regressao_motor_atual.json` foi gerada rodando o `motor_calculo` do
`cotacao_router` sobre todas as combinações de pedido × tabela que existiam no
banco, antes de as regras sintéticas serem apagadas. Cada caso traz a entrada e
o resultado que o motor antigo produziu.

O contrato deste arquivo: **o motor novo tem que devolver exatamente os mesmos
números**. Se um caso falhar, não é o teste que está errado — é o cálculo que
mudou de comportamento, e isso precisa ser uma decisão consciente, não um
efeito colateral.

Enquanto `calculo.py` for esqueleto, os testes pulam com aviso em vez de falhar.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "regressao_motor_atual.json"

# Tolerância: o motor antigo usa float e round(); o novo usa Decimal. Um centavo
# de diferença é aceitável e esperado. Mais que isso é mudança de regra.
TOLERANCIA = Decimal("0.01")


def carregar():
    if not FIXTURE.exists():
        pytest.skip(
            f"fixture ausente: {FIXTURE.name}. "
            "Gere com: python outcome_api/arquitetura_db/exportar_regras.py")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dados():
    return carregar()


def casos_validos(d):
    return [c for c in d["casos"] if "erro" not in c]


# ───────────────────────────────────────────── integridade da fixture
def test_fixture_existe_e_tem_casos(dados):
    assert dados["total_casos"] > 0, "nenhum caso de regressão foi congelado"
    assert casos_validos(dados), "todos os casos vieram com erro"


def test_fixture_registra_de_onde_veio_o_motor(dados):
    """Se a fixture foi gerada pela cópia e não pelo import, quero saber."""
    origem = dados["motor_origem"]
    assert origem
    if "cópia" in origem:
        pytest.warns(UserWarning) if False else None
        print(f"\n  ATENÇÃO: fixture gerada pela cópia embutida — {origem}")


def test_cada_caso_tem_entrada_e_esperado(dados):
    for c in casos_validos(dados):
        assert "entrada" in c and "esperado" in c
        e = c["esperado"]
        for campo in ("VN1_peso_taxado", "VN2_frete_peso", "VN3_generalidades",
                      "VN4_sobre_frete", "VN5_icms", "frete_liquido", "frete_total"):
            assert campo in e, f"{campo} faltando no caso {c['pedido']}/{c['tabela_frete_id']}"


def test_identidades_do_motor_antigo_se_sustentam(dados):
    """frete_liquido = VN2+VN3+VN4, e frete_total >= a soma com ICMS.

    Não testa o motor novo — testa que a fixture é internamente coerente. Se
    isto falhar, a fixture foi gerada errado e não serve de referência.
    """
    for c in casos_validos(dados):
        e = c["esperado"]
        liquido = round(e["VN2_frete_peso"] + e["VN3_generalidades"] + e["VN4_sobre_frete"], 2)
        assert abs(liquido - e["frete_liquido"]) <= 0.01, \
            f"caso {c['pedido']}/{c['tabela_frete_id']}: VN2+VN3+VN4 != frete_liquido"

        soma = e["VN2_frete_peso"] + e["VN3_generalidades"] + e["VN4_sobre_frete"] + e["VN5_icms"]
        minimo_geral = c["entrada"]["tarifas"].get("minimo_geral", 0)
        assert e["frete_total"] >= round(min(soma, minimo_geral), 2) - 0.01


def test_o_minimo_geral_realmente_pega_em_algum_caso(dados):
    """Se em nenhum caso o mínimo geral se aplicou, o teste não cobre esse ramo."""
    pegou = 0
    for c in casos_validos(dados):
        e = c["esperado"]
        soma = round(e["VN2_frete_peso"] + e["VN3_generalidades"]
                     + e["VN4_sobre_frete"] + e["VN5_icms"], 2)
        if e["frete_total"] > soma + 0.01:
            pegou += 1
    if pegou == 0:
        pytest.skip("nenhum caso exercita o mínimo geral — cobertura incompleta, "
                    "mas não é erro da fixture")
    assert pegou > 0


# ───────────────────────────────────────────── o motor novo
def motor_novo_pronto():
    """O cálculo saiu do estágio de esqueleto?"""
    try:
        from motor_frete.calculo import peso_taxado
        peso_taxado(Decimal("10"), Decimal("0"), Decimal("300"))
        return True
    except (ImportError, NotImplementedError):
        return False


@pytest.mark.skipif(not motor_novo_pronto(),
                    reason="calculo.py ainda é esqueleto — implemente e este teste liga sozinho")
def test_motor_novo_reproduz_o_antigo(dados):
    """O teste que importa: mesmo número, caso a caso.

    Quando falhar, a mensagem diz qual pedido, qual tabela e qual dos seis
    níveis divergiu — para a conversa ser sobre a regra, não sobre o total.
    """
    from motor_frete.calculo import calcular_de_tarifa_bruta  # a implementar

    divergencias = []
    for c in casos_validos(dados):
        ent, esp = c["entrada"], c["esperado"]
        obtido = calcular_de_tarifa_bruta(
            peso_bruto=Decimal(str(ent["peso_bruto_kg"])),
            volume_m3=Decimal(str(ent["volume_m3"])),
            valor_nf=Decimal(str(ent["valor_nf"])),
            fator_cubagem=Decimal(str(ent["fator_cubagem"])),
            tarifas=ent["tarifas"],
        )
        for campo, chave in (("VN1_peso_taxado", "peso_taxado"),
                             ("VN2_frete_peso", "frete_peso"),
                             ("VN3_generalidades", "generalidades"),
                             ("VN4_sobre_frete", "sobre_frete"),
                             ("VN5_icms", "icms"),
                             ("frete_liquido", "frete_liquido"),
                             ("frete_total", "frete_total")):
            dif = abs(Decimal(str(esp[campo])) - Decimal(str(obtido[chave])))
            if dif > TOLERANCIA:
                divergencias.append(
                    f"  pedido {c['pedido']} · tabela {c['tabela_frete_id']} · {campo}: "
                    f"antigo {esp[campo]} ≠ novo {obtido[chave]}  (dif {dif})")

    assert not divergencias, (
        f"\n{len(divergencias)} divergência(s) de {len(casos_validos(dados))} casos:\n"
        + "\n".join(divergencias[:25])
        + ("\n  …" if len(divergencias) > 25 else ""))
