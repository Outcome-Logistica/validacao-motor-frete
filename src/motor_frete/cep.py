# -*- coding: utf-8 -*-
"""Resolução de CEP → localidade.

O dado da parceira trouxe os 5.594 municípios com nome, UF e código IBGE, mas
sem faixa de CEP. Em vez de depender de uma carga única que envelhece, a
resolução é uma cascata com cache permanente:

    1. ceps_resolvidos      já vimos este CEP? responde sem sair do banco
    2. localidades          cai numa faixa de município? (quando existirem)
    3. consulta externa     ViaCEP/BrasilAPI devolvem o código IBGE, que casa
                            com localidades.codigo_ibge — 100% preenchido
    4. faixas_cep_uf        último recurso: resolve só a UF

Todo acerto vai para o cache. Um CEP novo dos Correios custa uma consulta, uma
única vez. Nunca quebra o sistema, e nunca é consultado duas vezes.

Uma resolução que ficou só na UF (`localidade_id` nulo) é reexaminada nas
próximas vezes, até `CEP_MAX_TENTATIVAS`: assim, quando a consulta externa
voltar a funcionar, o cache se corrige sozinho em vez de congelar o palpite.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Optional

from .config import settings
from .esquemas import Local

log = logging.getLogger(__name__)

SO_DIGITOS = re.compile(r"\D")


class CepNaoMapeado(Exception):
    """Nenhum nível da cascata resolveu o CEP."""


def normalizar(cep: str) -> str:
    """'60160-230' e ' 60160230 ' viram '60160230'."""
    limpo = SO_DIGITOS.sub("", str(cep or ""))
    if len(limpo) != 8:
        raise CepNaoMapeado(f"CEP inválido: {cep!r} — esperado 8 dígitos, veio {len(limpo)}")
    return limpo


# ═══════════════════════════════════════════════════ níveis da cascata
def _do_cache(cur, cep: str) -> Optional[dict]:
    cur.execute("""
        SELECT c.cep, c.uf_id, c.localidade_id, c.origem, c.tentativas_externas,
               u.sigla AS uf, l.nome AS localidade
          FROM ceps_resolvidos c
          JOIN ufs u ON u.id = c.uf_id
          LEFT JOIN localidades l ON l.id = c.localidade_id
         WHERE c.cep = %s""", (cep,))
    return cur.fetchone()


def _por_faixa_de_localidade(cur, cep: str) -> Optional[dict]:
    """A faixa mais específica ganha: município tem prioridade sobre distrito."""
    cur.execute("""
        SELECT l.id AS localidade_id, l.nome AS localidade, l.uf_id, u.sigla AS uf
          FROM localidades l
          JOIN ufs u ON u.id = l.uf_id
         WHERE l.cep_inicio IS NOT NULL AND l.cep_fim IS NOT NULL
           AND %s BETWEEN l.cep_inicio AND l.cep_fim
         ORDER BY (CAST(l.cep_fim AS UNSIGNED) - CAST(l.cep_inicio AS UNSIGNED)) ASC
         LIMIT 1""", (cep,))
    return cur.fetchone()


def _por_faixa_de_uf(cur, cep: str) -> Optional[dict]:
    cur.execute("""
        SELECT f.uf_id, u.sigla AS uf
          FROM faixas_cep_uf f
          JOIN ufs u ON u.id = f.uf_id
         WHERE %s BETWEEN f.cep_inicio AND f.cep_fim
         LIMIT 1""", (cep,))
    return cur.fetchone()


def consultar_externo(cep: str) -> Optional[dict]:
    """Pergunta o código IBGE a um serviço público.

    Devolve {'codigo_ibge', 'uf', 'municipio'} ou None. Nunca levanta: se o
    serviço está fora, a cascata segue para a faixa de UF.

    Usa urllib de propósito — não vale acrescentar uma dependência ao
    microserviço por causa de uma chamada.
    """
    if not settings.CEP_CONSULTA_EXTERNA:
        return None

    tentativas = [
        (f"https://viacep.com.br/ws/{cep}/json/", "ibge", "uf", "localidade"),
        (f"https://brasilapi.com.br/api/cep/v2/{cep}", None, "state", "city"),
    ]
    for url, campo_ibge, campo_uf, campo_cidade in tentativas:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "outcome-motor-frete/0.1"})
            with urllib.request.urlopen(req, timeout=settings.CEP_TIMEOUT_S) as r:
                dados = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            log.debug("consulta externa falhou em %s: %s", url, e)
            continue

        if dados.get("erro"):  # ViaCEP devolve {"erro": true} para CEP inexistente
            return None

        ibge = dados.get(campo_ibge) if campo_ibge else (
            (dados.get("location") or {}).get("ibge") or dados.get("ibge"))
        uf = dados.get(campo_uf)
        cidade = dados.get(campo_cidade)
        if uf:
            return {"codigo_ibge": str(ibge) if ibge else None,
                    "uf": uf, "municipio": cidade}
    return None


def _localidade_por_ibge(cur, codigo_ibge: str) -> Optional[dict]:
    """O código IBGE é a ponte: 100% preenchido nas 5.594 localidades."""
    if not codigo_ibge:
        return None
    cur.execute("""
        SELECT l.id AS localidade_id, l.nome AS localidade, l.uf_id, u.sigla AS uf
          FROM localidades l JOIN ufs u ON u.id = l.uf_id
         WHERE l.codigo_ibge = %s LIMIT 1""", (str(codigo_ibge),))
    return cur.fetchone()


def _localidade_por_nome(cur, municipio: str, uf: str) -> Optional[dict]:
    """Plano B quando a consulta externa não trouxe o IBGE."""
    if not (municipio and uf):
        return None
    import unicodedata
    limpo = "".join(c for c in unicodedata.normalize("NFKD", municipio)
                    if not unicodedata.combining(c)).upper()
    cur.execute("""
        SELECT l.id AS localidade_id, l.nome AS localidade, l.uf_id, u.sigla AS uf
          FROM localidades l JOIN ufs u ON u.id = l.uf_id
         WHERE u.sigla = %s AND l.nome_limpo = %s LIMIT 1""", (uf.upper(), limpo))
    return cur.fetchone()


def _gravar(cur, cep, uf_id, localidade_id, origem, codigo_ibge=None, tentativas=0):
    cur.execute("""
        INSERT INTO ceps_resolvidos
              (cep, uf_id, localidade_id, origem, codigo_ibge, tentativas_externas, ultima_tentativa)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
              uf_id = VALUES(uf_id),
              localidade_id = VALUES(localidade_id),
              origem = VALUES(origem),
              codigo_ibge = VALUES(codigo_ibge),
              tentativas_externas = VALUES(tentativas_externas),
              ultima_tentativa = NOW()""",
                (cep, uf_id, localidade_id, origem, codigo_ibge, tentativas))


# ═══════════════════════════════════════════════════ a cascata
def resolver(cur, cep_bruto: str, permitir_externa: bool = True) -> Local:
    """CEP → Local. Levanta CepNaoMapeado quando nenhum nível resolve."""
    cep = normalizar(cep_bruto)

    # 1 · cache
    c = _do_cache(cur, cep)
    if c and c["localidade_id"]:
        return Local(cep=cep, localidade_id=c["localidade_id"],
                     localidade=c["localidade"], uf=c["uf"])

    # o cache tem só a UF — vale tentar melhorar, dentro do limite de tentativas
    tentativas = c["tentativas_externas"] if c else 0
    tentar_externa = (permitir_externa and settings.CEP_CONSULTA_EXTERNA
                      and tentativas < settings.CEP_MAX_TENTATIVAS)

    # 2 · faixa de município
    r = _por_faixa_de_localidade(cur, cep)
    if r:
        _gravar(cur, cep, r["uf_id"], r["localidade_id"], "FAIXA_LOCALIDADE")
        return Local(cep=cep, localidade_id=r["localidade_id"],
                     localidade=r["localidade"], uf=r["uf"])

    # 3 · consulta externa → código IBGE → localidade
    if tentar_externa:
        ext = consultar_externo(cep)
        if ext:
            r = (_localidade_por_ibge(cur, ext["codigo_ibge"])
                 or _localidade_por_nome(cur, ext["municipio"], ext["uf"]))
            if r:
                _gravar(cur, cep, r["uf_id"], r["localidade_id"], "CONSULTA_EXTERNA",
                        ext["codigo_ibge"], tentativas)
                return Local(cep=cep, localidade_id=r["localidade_id"],
                             localidade=r["localidade"], uf=r["uf"])
            log.warning("CEP %s: a consulta externa respondeu %s/%s, mas nenhuma "
                        "localidade casou", cep, ext.get("municipio"), ext.get("uf"))
        tentativas += 1

    # 4 · faixa de UF — resolve o estado, não o município
    r = _por_faixa_de_uf(cur, cep)
    if r:
        _gravar(cur, cep, r["uf_id"], None, "FAIXA_UF", None, tentativas)
        return Local(cep=cep, localidade_id=None, localidade=None, uf=r["uf"])

    # nada resolveu
    if c:
        _gravar(cur, cep, c["uf_id"], None, c["origem"], None, tentativas)
    raise CepNaoMapeado(
        f"CEP {cep} não caiu em nenhuma faixa conhecida e a consulta externa "
        f"não resolveu. Confira se o seed schema_cep.sql foi aplicado.")
