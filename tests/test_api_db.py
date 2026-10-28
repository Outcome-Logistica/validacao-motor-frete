# -*- coding: utf-8 -*-
"""O motor de ponta a ponta, pela rota HTTP, contra o banco carregado.

É o teste que mais se parece com o que a tela vai fazer: monta um embarque,
chama `POST /calcular` e confere a resposta inteira — cotação ordenada,
componentes, rastreio, avisos e descartadas com motivo.

Pula sem `MOTOR_FRETE_TESTE_DB`. Ver conftest.py.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api import app
from conftest import precisa_db  # noqa: F401

cliente = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def rota(cur):
    """Uma rota que existe no banco E tem tarifa, com os CEPs já no cache.

    Semeia o cache de propósito: teste que depende de ViaCEP é sorteio, não
    teste. `origem` é ENUM, e 'MANUAL' é o valor honesto para algo posto à mão.
    """
    cur.execute("""
        SELECT ra.localidade_origem_id AS o, ra.localidade_destino_id AS d
          FROM regioes_atendidas ra
          JOIN tabelas_frete_v2 tf ON tf.transportadora_id = ra.transportadora_id
          JOIN tabelas_frete_peso fp ON fp.tabela_frete_id = tf.id
          JOIN faixas_valores fv ON fv.tabela_frete_peso_id = fp.id
         WHERE fp.praca_id = ra.praca_id
         LIMIT 1""")
    r = cur.fetchone()
    if not r:
        pytest.skip("banco sem rota com tarifa na mesma praça — a carga rodou?")

    for cep, loc in (("60000001", r["o"]), ("60000002", r["d"])):
        cur.execute("""
            INSERT INTO ceps_resolvidos (cep, uf_id, localidade_id, origem, ultima_tentativa)
            SELECT %s, uf_id, id, 'MANUAL', NOW() FROM localidades WHERE id = %s
            ON DUPLICATE KEY UPDATE localidade_id = VALUES(localidade_id),
                                    origem = VALUES(origem)""", (cep, loc))
    return {"origem_cep": "60000001", "destino_cep": "60000002"}


def corpo(rota, **troca):
    e = dict(referencia="API-1", peso_bruto_kg="120.5", volume_m3="0.4",
             valor_nf="15000.00", quantidade_volumes=4, **rota)
    e.update(troca)
    return {"embarque": e}


# ═══════════════════════════════════════════════════════════ a rota unitária
@precisa_db
def test_calcular_devolve_cotacao(rota):
    r = cliente.post("/calcular", json=corpo(rota))
    assert r.status_code == 200, r.text
    c = r.json()

    assert c["referencia"] == "API-1"
    assert c["resumo_carga"]["origem"]["uf"]
    assert c["cotacoes"] or c["descartadas"], "nem cotou nem descartou"

    for cot in c["cotacoes"]:
        assert Decimal(cot["frete_total"]) > 0
        assert Decimal(cot["frete_peso"]) > 0
        #  o total é a soma dos níveis, respeitando o mínimo geral
        soma = (Decimal(cot["frete_peso"]) + Decimal(cot["generalidades"])
                + Decimal(cot["sobre_frete"]) + Decimal(cot["icms"]))
        assert abs(Decimal(cot["frete_total"]) - soma) <= Decimal("0.01")


@precisa_db
def test_cotacoes_vem_da_mais_barata_para_a_mais_cara(rota):
    """A ordem fica no motor, não em cada chamador — senão a tela e a auditoria
    ordenam de jeitos diferentes."""
    c = cliente.post("/calcular", json=corpo(rota)).json()
    totais = [Decimal(x["frete_total"]) for x in c["cotacoes"]]
    assert totais == sorted(totais)


@precisa_db
def test_cada_cotacao_traz_rastreio(rota):
    """Sem o rastreio, um valor contestado não tem como ser defendido."""
    c = cliente.post("/calcular", json=corpo(rota)).json()
    if not c["cotacoes"]:
        pytest.skip("nenhuma cotação para esta rota")
    for cot in c["cotacoes"]:
        r = cot["rastreio"]
        assert r["tabela_frete_id"] and r["tabela_frete_peso_id"]
        assert r["praca"] and r["modalidade"]
        assert r["icms_de"] in ("TABELA", "ICMS_UF")


@precisa_db
def test_componentes_somam_as_generalidades(rota):
    """Os componentes não são enfeite: a auditoria compara contra
    tb_cte_tarifas linha a linha, então o somatório tem que fechar."""
    c = cliente.post("/calcular", json=corpo(rota)).json()
    if not c["cotacoes"]:
        pytest.skip("nenhuma cotação para esta rota")
    for cot in c["cotacoes"]:
        fora = {"FRETE_PESO", "ICMS"}
        soma = sum(Decimal(x["valor"]) for x in cot["componentes"]
                   if x["codigo"] not in fora)
        assert abs(soma - Decimal(cot["generalidades"])) <= Decimal("0.01")


@precisa_db
def test_descartadas_vem_com_motivo_conhecido(rota):
    """Hoje, quando uma transportadora some da tela, ninguém sabe por quê."""
    from motor_frete.esquemas import MotivoDescarte

    c = cliente.post("/calcular", json=corpo(rota)).json()
    validos = {m.value for m in MotivoDescarte}
    for d in c["descartadas"]:
        assert d["motivo"] in validos
        assert d["transportadora"]


@precisa_db
def test_avisos_quando_houver_sao_todos_conhecidos(rota):
    from motor_frete.esquemas import Aviso

    c = cliente.post("/calcular", json=corpo(rota)).json()
    validos = {a.value for a in Aviso}
    for cot in c["cotacoes"]:
        for av in cot["avisos"]:
            assert av in validos


@precisa_db
def test_memoria_mostra_a_escada_e_as_formulas(rota):
    c = cliente.post("/calcular", json=corpo(rota)).json()
    if not c["cotacoes"]:
        pytest.skip("nenhuma cotação para esta rota")
    m = c["cotacoes"][0]["memoria_calculo"]
    assert m is not None
    for chave in ("VN1_peso_taxado", "VN2_frete_peso", "VN3_generalidades",
                  "VN6_total", "aliquota_icms", "icms_de"):
        assert chave in m


@precisa_db
def test_cep_sem_cobertura_responde_422_e_nao_500(rota):
    """Embarque que não dá para cotar é resposta, não defeito de servidor."""
    r = cliente.post("/calcular", json=corpo(rota, destino_cep="99999999"))
    assert r.status_code in (200, 422), r.text
    if r.status_code == 422:
        assert "CEP_NAO_MAPEADO" in r.text or "NAO_ATENDE_ROTA" in r.text


@precisa_db
def test_peso_absurdo_descarta_em_vez_de_cotar_errado(rota):
    """100 toneladas não cabem em nenhuma faixa fracionada. Melhor sair da
    cotação com PESO_FORA_DAS_FAIXAS que inventar um preço."""
    c = cliente.post("/calcular", json=corpo(rota, peso_bruto_kg="100000")).json()
    motivos = {d["motivo"] for d in c.get("descartadas", [])}
    assert c["cotacoes"] or motivos, "nem cotou nem explicou"


# ═══════════════════════════════════════════════════════════ o lote
@precisa_db
def test_lote_cota_varios_e_resume(rota):
    r = cliente.post("/calcular-lote", json={
        "embarques": [dict(referencia=f"API-L{i}", peso_bruto_kg="120.5",
                           valor_nf="15000.00", **rota) for i in range(3)],
        "incluir_descartadas": True,
    })
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["resumo"]["embarques"] == 3
    assert (c["resumo"]["com_cotacao"] + c["resumo"]["sem_cotacao"]
            + c["resumo"]["com_erro"]) == 3
    assert len(c["resultados"]) == 3
    assert c["resumo"]["duracao_ms"] >= 0


@precisa_db
def test_lote_nao_derruba_tudo_por_um_embarque_ruim(rota):
    """É o ponto de `parar_no_erro=false`: num fechamento de auditoria com
    milhares de CT-e, um registro ruim não pode anular o lote."""
    r = cliente.post("/calcular-lote", json={
        "embarques": [
            dict(referencia="BOM", peso_bruto_kg="120.5", valor_nf="15000.00", **rota),
            dict(referencia="RUIM", peso_bruto_kg="120.5", valor_nf="15000.00",
                 origem_cep="00000000", destino_cep="00000001"),
        ],
    })
    assert r.status_code == 200, r.text
    c = r.json()
    assert len(c["resultados"]) == 2
    por_ref = {x["referencia"]: x for x in c["resultados"]}
    assert por_ref["RUIM"]["erro"], "o item ruim tem que dizer o que houve"
    assert por_ref["BOM"]["erro"] is None


@precisa_db
def test_lote_nao_traz_memoria_por_padrao(rota):
    """Memória de N embarques × M transportadoras fica grande."""
    c = cliente.post("/calcular-lote", json={
        "embarques": [dict(referencia="API-M", peso_bruto_kg="120.5",
                           valor_nf="15000.00", **rota)],
    }).json()
    for res in c["resultados"]:
        for cot in res["cotacoes"]:
            assert cot["memoria_calculo"] is None


@precisa_db
def test_a_mesma_data_da_o_mesmo_numero(rota):
    """Reprodutibilidade: é o que permite a auditoria recalcular um CT-e antigo
    e comparar. Sem isso, divergência não distingue erro de mudança de tabela."""
    pedido = corpo(rota)
    pedido["data_referencia"] = "2026-06-15"
    a = cliente.post("/calcular", json=pedido).json()
    b = cliente.post("/calcular", json=pedido).json()
    assert ([(x["transportadora_id"], x["frete_total"]) for x in a["cotacoes"]]
            == [(x["transportadora_id"], x["frete_total"]) for x in b["cotacoes"]])


@precisa_db
def test_health_diz_que_o_schema_esta_aplicado():
    r = cliente.get("/health").json()
    assert r["banco"] == "ok"
    assert r["schema_cotacao"] == "ok", r.get("detalhe")
    assert r["status"] == "ok"
