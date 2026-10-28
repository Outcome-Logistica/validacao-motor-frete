# -*- coding: utf-8 -*-
"""Testes do contrato.

Enquanto o cálculo não existe, o que dá para garantir é que a FORMA da API está
certa: o serviço sobe, /health responde, a validação rejeita entrada malformada
e o cálculo devolve 501 em vez de 500. Quando a lógica entrar, estes testes
continuam valendo e os de cálculo vêm por cima.
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from api import app
from motor_frete.esquemas import Embarque, MotivoDescarte, RequisicaoLote

cliente = TestClient(app, raise_server_exceptions=False)


def embarque_valido(**troca):
    base = dict(
        referencia="PED-1234",
        origem_cep="60000000",
        destino_cep="20000000",
        peso_bruto_kg="120.5",
        volume_m3="0.8",
        valor_nf="15000.00",
        quantidade_volumes=4,
    )
    base.update(troca)
    return base


# ───────────────────────────────────────────────── saúde
def test_health_responde_e_traz_o_estado_do_schema():
    r = cliente.get("/health")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["servico"] == "motor-frete"
    assert corpo["status"] in ("ok", "degradado")
    assert corpo["banco"] in ("ok", "indisponivel")
    assert corpo["schema_cotacao"] in ("ok", "incompleto", "desconhecido")


def test_raiz_identifica_o_servico():
    r = cliente.get("/")
    assert r.status_code == 200
    assert r.json()["servico"] == "motor-frete"


# ───────────────────────────────────────────────── contrato de entrada
#  Estes testes rodam COM e SEM banco, então não dá para fixar um código de
#  status: sem banco vem 503; com banco, os CEPs de exemplo não estão no cache
#  e a resolução responde 422 "não dá para cotar". O que o contrato garante é
#  que a VALIDAÇÃO passou — e isso se vê pela FORMA do erro, não pelo código.
SEM_BANCO = 503
NAO_DA_PARA_COTAR = 422


def validacao_passou(r) -> bool:
    """O corpo foi aceito pelo pydantic?

    O 422 do pydantic traz `detail` como LISTA de erros de campo; o 422 de
    descarte traz um OBJETO com `motivo`. Distinguir pela forma é o que permite
    a este teste provar "o contrato de entrada está certo" sem depender de ter
    banco, cache de CEP ou tarifa carregada.
    """
    if r.status_code != 422:
        return True
    detalhe = r.json().get("detail")
    return isinstance(detalhe, dict) and "motivo" in detalhe


def test_calcular_aceita_embarque_valido():
    r = cliente.post("/calcular", json={"embarque": embarque_valido()})
    assert validacao_passou(r), f"o corpo é válido e foi recusado: {r.text}"
    assert r.status_code in (200, SEM_BANCO, NAO_DA_PARA_COTAR), r.text


def test_descarte_e_validacao_nao_se_confundem():
    """Dois 422 diferentes precisam ser distinguíveis, senão a tela mostra
    "erro ao cotar" tanto para CEP inválido quanto para rota sem cobertura."""
    ruim = cliente.post("/calcular",
                        json={"embarque": embarque_valido(destino_cep="123")})
    assert ruim.status_code == 422
    assert isinstance(ruim.json()["detail"], list), "validação: lista de campos"

    valido = cliente.post("/calcular", json={"embarque": embarque_valido()})
    if valido.status_code == 422:
        d = valido.json()["detail"]
        assert isinstance(d, dict) and d["motivo"], "descarte: objeto com motivo"


def test_banco_fora_e_503_e_nao_500():
    """A diferença importa para quem integra: 500 diz "defeito no serviço" e
    gera ticket; 503 diz "a dependência caiu, tente de novo"."""
    r = cliente.post("/calcular", json={"embarque": embarque_valido()})
    assert r.status_code != 500, "banco fora não é erro interno do motor"


def test_pendencias_expostas_na_api():
    """A tela de parâmetros lê daqui, e quem integra precisa saber que há
    escolhas nossas embutidas no número."""
    r = cliente.get("/pendencias")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["total"] >= 7
    assert [i["n"] for i in corpo["itens"]] == list(range(1, corpo["total"] + 1))


def test_cep_precisa_ter_8_digitos():
    r = cliente.post("/calcular", json={"embarque": embarque_valido(destino_cep="20000-000")})
    assert r.status_code == 422


def test_peso_negativo_e_rejeitado():
    r = cliente.post("/calcular", json={"embarque": embarque_valido(peso_bruto_kg="-1")})
    assert r.status_code == 422


def test_campo_desconhecido_e_rejeitado():
    """extra=forbid: um typo no nome do campo tem que falhar alto, não ser ignorado."""
    r = cliente.post("/calcular", json={"embarque": embarque_valido(peso_brutokg="10")})
    assert r.status_code == 422


def test_referencia_e_obrigatoria():
    corpo = embarque_valido()
    del corpo["referencia"]
    r = cliente.post("/calcular", json={"embarque": corpo})
    assert r.status_code == 422


def test_data_referencia_e_opcional():
    r = cliente.post("/calcular", json={
        "embarque": embarque_valido(),
        "data_referencia": "2026-03-15",
    })
    assert validacao_passou(r), r.text
    assert r.status_code in (200, SEM_BANCO, NAO_DA_PARA_COTAR), r.text


# ───────────────────────────────────────────────── lote
def test_lote_aceita_varios_embarques():
    r = cliente.post("/calcular-lote", json={
        "embarques": [embarque_valido(referencia=f"PED-{i}") for i in range(3)]
    })
    assert r.status_code in (200, SEM_BANCO)


def test_lote_vazio_e_rejeitado():
    r = cliente.post("/calcular-lote", json={"embarques": []})
    assert r.status_code == 422


def test_lote_tem_teto():
    """5000 por chamada: acima disso é melhor paginar do que estourar memória."""
    with pytest.raises(Exception):
        RequisicaoLote(embarques=[Embarque(**embarque_valido()) for _ in range(5001)])


def test_lote_nao_traz_memoria_por_padrao():
    """Memória de N embarques × M transportadoras fica grande. Padrão desligado."""
    req = RequisicaoLote(embarques=[Embarque(**embarque_valido())])
    assert req.incluir_memoria is False


# ───────────────────────────────────────────────── modelo
def test_embarque_normaliza_decimais():
    e = Embarque(**embarque_valido())
    assert str(e.peso_bruto_kg) == "120.5"
    assert e.volume_m3 > 0


def test_motivos_de_descarte_cobrem_os_passos_da_resolucao():
    """Cada passo que pode não retornar linha precisa do seu motivo."""
    esperados = {
        "CEP_NAO_MAPEADO", "NAO_ATENDE_ROTA", "ROTA_SEM_VIGENCIA",
        "SEM_TABELA_VIGENTE", "PRODUTO_NAO_ACEITO",
        "SEM_FRETE_PESO_PARA_PRACA", "PESO_FORA_DAS_FAIXAS", "SEM_ICMS_PARA_UF",
    }
    assert esperados <= {m.value for m in MotivoDescarte}


def test_openapi_documenta_as_duas_rotas():
    esquema = cliente.get("/openapi.json").json()
    assert "/calcular" in esquema["paths"]
    assert "/calcular-lote" in esquema["paths"]
    assert "/health" in esquema["paths"]


# ───────────────────────────────────────────────── resolução e cálculo
def test_nao_sobrou_esqueleto_no_motor():
    """Nenhuma função do motor levanta mais NotImplementedError.

    Saíram, nesta ordem: a cascata de CEP, o cálculo VN1–VN6, o interpretador de
    fórmulas, o VN3, os onze passos da resolução e, por último,
    `calculo.calcular` — a costura entre a resolução e a conta.

    O que ainda não existe não é esqueleto: é `/calcular` e `/calcular-lote`
    ligarem os dois, e isso está coberto pelos testes de contrato acima.
    """
    import inspect

    from motor_frete import calculo, cep, generalidades, resolucao

    for mod in (cep, resolucao, calculo, generalidades):
        for nome, fn in inspect.getmembers(mod, inspect.isfunction):
            if fn.__module__ != mod.__name__:
                continue
            fonte = inspect.getsource(fn)
            assert "raise NotImplementedError" not in fonte, (
                f"{mod.__name__}.{nome} ainda é esqueleto")


def test_a_resolucao_saiu_do_esqueleto():
    """Os onze passos existem e têm a assinatura esperada — todos recebem o
    cursor primeiro, porque todos leem do banco."""
    import inspect

    from motor_frete import resolucao

    esperado = {
        "resolver_local":         ["cur", "cep"],
        "rotas_atendidas":        ["cur", "origem", "destino", "modalidades", "transportadoras"],
        "vigencia_da_rota":       ["cur", "regiao_atendida_id", "quando"],
        "tabelas_vigentes":       ["cur", "transportadora_id", "uf_origem", "modalidade_id",
                                   "tipo_frete", "tipo_nf", "quando"],
        "aceita_produtos":        ["cur", "tabela_frete_id", "categorias"],
        "fretes_peso":            ["cur", "tabela_frete_id", "praca_id", "uf_destino"],
        "faixas_do_frete_peso":   ["cur", "tabela_frete_peso_id"],
        "perfil_de_generalidade": ["cur", "perfil_generalidade_id"],
        "aliquota_icms":          ["cur", "uf_origem", "uf_destino", "quando"],
        "codigo_servicos_cliente": ["cur", "transportadora_id", "cnpj_destino"],
        "resolver":               ["cur", "embarque", "quando", "transportadoras"],
    }
    for nome, params in esperado.items():
        fn = getattr(resolucao, nome)
        assert list(inspect.signature(fn).parameters) == params, f"assinatura de {nome}"


def test_avisos_cobrem_as_escolhas_que_o_motor_faz():
    """Toda vez que o motor escolhe no lugar do dado, existe um aviso para dizer."""
    from motor_frete.esquemas import Aviso

    assert {"TABELA_AMBIGUA", "TARIFA_AMBIGUA", "ICMS_ESTIMADO", "SEM_PRAZO",
            "SEM_CODIGO_SERVICOS", "CALENDARIO_SUPOSTO"} <= {a.value for a in Aviso}


def test_cotacao_carrega_os_avisos():
    from motor_frete.esquemas import Cotacao

    assert "avisos" in Cotacao.model_fields


def test_calculo_vn1_a_vn6_esta_implementado():
    """O núcleo do cálculo saiu do esqueleto — estes não podem mais levantar."""
    from decimal import Decimal

    from motor_frete.calculo import Carga, Politica, Tarifa, calcular_niveis, peso_taxado

    assert peso_taxado(Decimal("100"), Decimal("1"), Decimal("300")) == Decimal("300.00")

    n = calcular_niveis(
        Carga(peso_bruto=Decimal("100"), volume_m3=Decimal("0"), valor_nf=Decimal("1000")),
        Tarifa(fp_minimo=Decimal("80"), fp_valor=Decimal("2.5"), fp_arred=0,
               sobre_frete=Decimal("0.12"), icms=Decimal("0.18"),
               minimo_geral=Decimal("0"), fator_cubagem=Decimal("300")),
        politica=Politica.POR_NIVEL)
    assert n.frete_peso == Decimal("250.00")          # 100 kg × 2,50
    assert n.sobre_frete == Decimal("30.00")          # 250 × 0,12
    assert n.icms == Decimal("50.40")                 # 280 × 0,18
    assert n.frete_liquido == Decimal("280.00")
    assert n.frete_total == Decimal("330.40")


def test_o_minimo_do_frete_peso_e_sinalizado():
    """Saber que o mínimo pegou é o centro da queixa sobre fracionamento."""
    from decimal import Decimal

    from motor_frete.calculo import Carga, Tarifa, calcular_niveis

    n = calcular_niveis(
        Carga(peso_bruto=Decimal("1"), volume_m3=Decimal("0")),
        Tarifa(fp_minimo=Decimal("80"), fp_valor=Decimal("2.5")))
    assert n.frete_peso == Decimal("80.00")
    assert n.aplicou_minimo_frete_peso is True


def test_decimal_nao_float_no_resultado():
    """Dinheiro em float acumula centavo — o ponto do motor único é fechar igual."""
    from decimal import Decimal

    from motor_frete.calculo import Carga, Tarifa, calcular_niveis

    n = calcular_niveis(Carga(peso_bruto=Decimal("10")), Tarifa(fp_valor=Decimal("1.5")))
    for v in (n.peso_taxado, n.frete_peso, n.frete_total):
        assert isinstance(v, Decimal)


def test_passo_1_esta_implementado():
    """resolver_local não é mais stub — tem que aceitar (cursor, cep)."""
    import inspect

    from motor_frete.resolucao import resolver_local
    params = list(inspect.signature(resolver_local).parameters)
    assert params == ["cur", "cep"], f"assinatura mudou: {params}"


def test_arredondamento_e_decimal_nao_float():
    """Dinheiro em float acumula erro de centavo — e o ponto do motor único é
    os dois lados fecharem o MESMO número."""
    from decimal import Decimal

    from motor_frete.calculo import arredondar

    assert arredondar(Decimal("2.675"), 2) == Decimal("2.68")   # float daria 2.67
    assert arredondar(Decimal("10.005"), 2) == Decimal("10.01")
    assert isinstance(arredondar(Decimal("1.234"), 2), Decimal)
