# -*- coding: utf-8 -*-
"""Testes da resolução de CEP.

Rodam contra um banco de verdade quando `MOTOR_FRETE_TESTE_DB` estiver
configurado; senão, pulam. A consulta externa é sempre simulada — teste que
depende de internet não é teste, é sorteio.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from motor_frete.cep import CepNaoMapeado, normalizar


# ─────────────────────────────────────────── normalização (não precisa de banco)
@pytest.mark.parametrize("entrada,esperado", [
    ("60160230", "60160230"),
    ("60160-230", "60160230"),
    (" 60160230 ", "60160230"),
    ("60.160-230", "60160230"),
])
def test_normaliza_formatos_comuns(entrada, esperado):
    assert normalizar(entrada) == esperado


@pytest.mark.parametrize("ruim", ["6016023", "601602300", "", None, "abcdefgh", "6016-230"])
def test_rejeita_cep_invalido(ruim):
    with pytest.raises(CepNaoMapeado):
        normalizar(ruim)


def test_a_mensagem_de_erro_diz_o_que_veio():
    """Erro de CEP precisa dizer o que chegou — senão vira caça ao fantasma."""
    with pytest.raises(CepNaoMapeado, match="7"):
        normalizar("6016023")


# ─────────────────────────────────────────── cascata (precisa de banco)
#  A conexão e o significado de MOTOR_FRETE_TESTE_DB ficam em conftest.py —
#  um lugar só, para os dois arquivos de teste não divergirem (foi o que
#  aconteceu: um esperava DSN, o outro a configuração do serviço).
from conftest import cur_cache_limpo as cur  # noqa: F401,E402
from conftest import precisa_db as pytestmark_db  # noqa: F401,E402


@pytestmark_db
def test_faixa_de_uf_resolve_o_estado(cur):
    """Sem faixa de município e sem internet, ainda dá para saber a UF."""
    from motor_frete.cep import resolver
    with patch("motor_frete.cep.consultar_externo", return_value=None):
        for cep, uf in [("60160230", "CE"), ("01310100", "SP"), ("20040020", "RJ"),
                        ("70040010", "DF"), ("90010150", "RS"), ("76801000", "RO")]:
            local = resolver(cur, cep)
            assert local.uf == uf, f"{cep} deveria ser {uf}"
            assert local.localidade_id is None, "faixa de UF não sabe o município"


@pytestmark_db
def test_consulta_externa_resolve_o_municipio_pelo_ibge(cur):
    """O código IBGE é a ponte: 100% preenchido nas 5.594 localidades."""
    from motor_frete.cep import resolver
    falso = {"codigo_ibge": "2304400", "uf": "CE", "municipio": "Fortaleza"}
    with patch("motor_frete.cep.consultar_externo", return_value=falso):
        local = resolver(cur, "60160230")
    assert local.uf == "CE"
    assert local.localidade_id is not None
    assert "Fortaleza" in (local.localidade or "")


@pytestmark_db
def test_o_cache_evita_a_segunda_consulta(cur):
    """É o ponto do desenho: um CEP novo custa uma consulta, UMA vez."""
    from motor_frete import cep as mod
    falso = {"codigo_ibge": "2304400", "uf": "CE", "municipio": "Fortaleza"}
    chamadas = []

    def espiao(c):
        chamadas.append(c)
        return falso

    with patch.object(mod, "consultar_externo", espiao):
        mod.resolver(cur, "60160230")
        mod.resolver(cur, "60160230")
        mod.resolver(cur, "60160230")
    assert len(chamadas) == 1, f"consultou {len(chamadas)}× — o cache não pegou"


@pytestmark_db
def test_resolucao_so_por_uf_e_reexaminada_depois(cur):
    """Se a internet cair, o palpite de UF não pode congelar para sempre."""
    from motor_frete import cep as mod
    with patch.object(mod, "consultar_externo", return_value=None):
        primeiro = mod.resolver(cur, "60160230")
    assert primeiro.localidade_id is None

    falso = {"codigo_ibge": "2304400", "uf": "CE", "municipio": "Fortaleza"}
    with patch.object(mod, "consultar_externo", return_value=falso):
        segundo = mod.resolver(cur, "60160230")
    assert segundo.localidade_id is not None, "o cache deveria ter se corrigido"


@pytestmark_db
def test_cep_fora_de_qualquer_faixa_levanta(cur):
    from motor_frete.cep import CepNaoMapeado, resolver
    with patch("motor_frete.cep.consultar_externo", return_value=None):
        with pytest.raises(CepNaoMapeado):
            resolver(cur, "00500000")


@pytestmark_db
def test_nao_martela_api_que_nao_responde(cur):
    """Depois de CEP_MAX_TENTATIVAS, para de tentar — a UF já está no cache."""
    from motor_frete import cep as mod
    from motor_frete.config import settings
    chamadas = []

    def espiao(c):
        chamadas.append(c)
        return None

    with patch.object(mod, "consultar_externo", espiao):
        for _ in range(settings.CEP_MAX_TENTATIVAS + 3):
            mod.resolver(cur, "60160230")
    assert len(chamadas) <= settings.CEP_MAX_TENTATIVAS, \
        f"tentou {len(chamadas)}× com o limite em {settings.CEP_MAX_TENTATIVAS}"


@pytestmark_db
def test_descarte_do_motor_vem_com_o_motivo_certo(cur):
    from motor_frete.esquemas import MotivoDescarte
    from motor_frete.resolucao import DescarteError, resolver_local
    with patch("motor_frete.cep.consultar_externo", return_value=None):
        with pytest.raises(DescarteError) as e:
            resolver_local(cur, "00500000")
    assert e.value.motivo == MotivoDescarte.CEP_NAO_MAPEADO
