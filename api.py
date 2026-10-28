# -*- coding: utf-8 -*-
"""motor-frete — o cálculo de frete do Outcome, em um lugar só.

Chamado pela cotação (antes do embarque, para escolher a transportadora) e pela
auditoria (depois, para conferir o que o CT-e cobrou). Enquanto eram dois
motores, os dois lados nunca fechavam o mesmo número.

Subir em desenvolvimento:
    uv run uvicorn api:app --host 0.0.0.0 --port 8003 --reload

## O que a resposta traz de diferente do motor antigo

**Rastreio.** Cada cotação diz qual tabela, praça e faixa produziram o número.
Sem isso, um valor contestado não tem como ser defendido.

**Componentes item a item**, na granularidade de `tb_cte_tarifas`. É o que torna
a conferência da auditoria um `JOIN` em vez de comparar totais.

**Descartadas com motivo.** Hoje, quando uma transportadora desaparece da tela
de cotação, ninguém sabe por quê.

**Avisos.** Quando o cadastro da parceira é ambíguo, o motor escolhe — e diz que
escolheu. Tarifa duplicada com preços diferentes, ICMS estimado, código de
serviços do cliente não encontrado.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date
from pathlib import Path

import pymysql
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent / "src"))

from motor_frete.calculo import calcular as calcular_cotacao   # noqa: E402
from motor_frete.config import settings                        # noqa: E402
from motor_frete.db import checar_banco, cursor                # noqa: E402
from motor_frete.esquemas import (                             # noqa: E402
    Descartada, Embarque, RequisicaoCalculo, RequisicaoLote, RespostaCalculo,
    RespostaLote, ResumoCarga, ResumoLote, Saude,
)
from motor_frete.resolucao import DescarteError, resolver       # noqa: E402

logging.basicConfig(
    level=settings.LOG_NIVEL,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
)
log = logging.getLogger("motor-frete")

app = FastAPI(
    title="Outcome · motor-frete",
    description=(
        "Cálculo de frete VN1–VN6 sobre o schema relacional de cotação. "
        "Serve a cotação e a auditoria com o mesmo código, para que os dois "
        "lados fechem o mesmo número."
    ),
    version=settings.SERVICO_VERSAO,
)


# ════════════════════════════════════════════════════════════════ saúde
@app.get("/health", response_model=Saude, tags=["Operação"])
def health() -> Saude:
    """Serviço de pé, banco alcançável e schema de cotação presente.

    Checa as 21 tabelas porque subir contra um banco sem o schema é o erro mais
    provável no começo — e o sintoma seria um 500 obscuro no primeiro cálculo.
    """
    banco, schema, detalhe = checar_banco()
    degradado = banco != "ok" or schema != "ok"
    return Saude(
        status="degradado" if degradado else "ok",
        servico=settings.SERVICO_NOME,
        versao=settings.SERVICO_VERSAO,
        banco=banco,
        schema_cotacao=schema,
        detalhe=detalhe,
    )


@app.get("/", include_in_schema=False)
def raiz():
    return {
        "servico": settings.SERVICO_NOME,
        "versao": settings.SERVICO_VERSAO,
        "docs": "/docs",
    }


@app.get("/pendencias", tags=["Operação"])
def pendencias():
    """O que o motor está supondo enquanto a parceira não responde.

    Existe como rota para a tela de parâmetros poder mostrar, e para quem
    integra saber que há escolhas nossas embutidas no número. Cada item tem uma
    constante em `src/motor_frete/pendencias.py` e um teste que a trava.
    """
    from motor_frete import pendencias as p
    return {"total": len(p.resumo()), "itens": p.resumo()}


# ════════════════════════════════════════════════════════════════ cálculo
def _cotar(cur, embarque: Embarque, quando: date,
           transportadoras: list[int] | None,
           incluir_descartadas: bool, incluir_memoria: bool) -> RespostaCalculo:
    """Resolve e calcula um embarque. É o corpo que as duas rotas compartilham.

    A ordem importa: a resolução decide QUEM pode cotar e com quais números, e
    só então o cálculo roda. Uma transportadora que cai em qualquer um dos onze
    passos vira `Descartada` com o motivo, em vez de simplesmente não aparecer.
    """
    contextos, descartadas = resolver(cur, embarque, quando, transportadoras)

    cotacoes = []
    for ctx in contextos:
        try:
            cotacoes.append(calcular_cotacao(embarque, ctx, quando,
                                             incluir_memoria=incluir_memoria))
        except DescarteError as e:
            #  Um descarte pode nascer no cálculo, não só na resolução.
            descartadas.append({
                "transportadora_id": ctx.transportadora_id,
                "transportadora": ctx.transportadora,
                "motivo": e.motivo, "detalhe": e.detalhe,
            })

    #  Da mais barata para a mais cara. É a ordem que a tela usa, e deixá-la
    #  aqui evita que cada chamador reinvente o critério.
    cotacoes.sort(key=lambda c: c.frete_total)

    return RespostaCalculo(
        referencia=embarque.referencia,
        data_referencia=quando,
        resumo_carga=ResumoCarga(
            origem=_local(cur, embarque.origem_cep),
            destino=_local(cur, embarque.destino_cep),
            peso_bruto_kg=embarque.peso_bruto_kg,
            volume_m3=embarque.volume_m3,
            valor_nf=embarque.valor_nf,
        ),
        cotacoes=cotacoes,
        descartadas=([Descartada(**d) for d in descartadas]
                     if incluir_descartadas else []),
    )


BANCO_FORA = (
    "O banco não respondeu. O motor lê o schema de cotação direto do MySQL, "
    "então sem banco não há cotação. Veja GET /health para o diagnóstico."
)


def _nao_da_para_cotar(e: DescarteError) -> HTTPException:
    """Descarte no nível do embarque → 422 com corpo ESTRUTURADO.

    O 422 do pydantic (campo faltando, CEP com 7 dígitos) e o 422 de "não dá
    para cotar isto" (CEP válido mas desconhecido) são coisas diferentes com o
    mesmo código HTTP. Para o chamador distinguir, o formato do `detail` difere:

        validação  → detail é uma LISTA de erros de campo (padrão do FastAPI)
        descarte   → detail é um OBJETO com `motivo` e `detalhe`

    Importa para a tela: "CEP não encontrado" e "nenhuma transportadora atende"
    pedem mensagens diferentes ao usuário, e sem isso as duas viram o mesmo
    "erro ao cotar".
    """
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"motivo": e.motivo.value, "detalhe": e.detalhe,
                "erro": "nao_da_para_cotar"})


def _banco_fora(e: Exception) -> HTTPException:
    """Banco fora do ar vira 503, não 500.

    A diferença não é cosmética: 500 diz "defeito no serviço" e manda o
    chamador abrir um ticket; 503 diz "a dependência caiu, tente de novo".
    Quem integra trata os dois de formas diferentes.

    `db.cursor()` é um contextmanager — a conexão só abre no `with`. Então o
    tratamento tem que envolver o bloco, não a chamada. Foi o que eu errei na
    primeira versão: o `try` ficava em volta de `cursor()` e não pegava nada.
    """
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                         f"{BANCO_FORA}  ({e.args[0] if e.args else e})")


def _local(cur, cep: str):
    """O CEP já resolvido. Vem do cache — a resolução acabou de gravá-lo."""
    from motor_frete.cep import CepNaoMapeado, resolver as resolver_cep
    from motor_frete.esquemas import Local
    try:
        return resolver_cep(cur, cep, permitir_externa=False)
    except CepNaoMapeado:
        return Local(cep=cep)


@app.post("/calcular", response_model=RespostaCalculo, tags=["Cálculo"])
def calcular(req: RequisicaoCalculo):
    """Cota um embarque em todas as transportadoras que atendem a rota.

    Devolve as cotações ordenadas da mais barata para a mais cara, cada uma com
    VN1–VN6 abertos, os componentes item a item e o rastreio de qual tabela,
    praça e faixa produziram o número.

    `data_referencia` resolve a vigência: a cotação manda hoje, a auditoria
    manda a data de saída do embarque. É o que torna o recálculo reprodutível —
    recalcular um CT-e de março com a tabela de setembro dá divergência falsa.
    """
    quando = req.data_referencia or date.today()
    log.info("calcular ref=%s %s→%s em %s",
             req.embarque.referencia, req.embarque.origem_cep,
             req.embarque.destino_cep, quando)
    try:
        with cursor() as cur:
            r = _cotar(cur, req.embarque, quando, req.transportadoras,
                       req.incluir_descartadas, req.incluir_memoria)
    except pymysql.err.OperationalError as e:
        raise _banco_fora(e) from e
    except DescarteError as e:
        #  Descarte no nível do embarque: o CEP não resolveu, ou ninguém atende
        #  a rota. Não é erro de servidor — é resposta.
        raise _nao_da_para_cotar(e) from e

    log.info("calcular ref=%s → %d cotação(ões), %d descartada(s)",
             req.embarque.referencia, len(r.cotacoes), len(r.descartadas))
    return r


@app.post("/calcular-lote", response_model=RespostaLote, tags=["Cálculo"])
def calcular_lote(req: RequisicaoLote):
    """Cota N embarques numa chamada.

    É a rota principal, não uma conveniência: a auditoria recalcula milhares de
    CT-e por fechamento. Uma chamada por CT-e transformaria cada auditoria em
    milhares de round-trips — o motivo de a interface nascer em lote.

    Com `parar_no_erro=false` (padrão), um embarque que falha volta com o campo
    `erro` preenchido e não derruba o resto do lote. Uma conexão só atende o
    lote inteiro: abrir uma por embarque é o que faz lote grande ficar lento.
    """
    t0 = time.time()
    quando = req.data_referencia or date.today()
    log.info("calcular-lote %d embarques em %s", len(req.embarques), quando)

    resultados: list[RespostaCalculo] = []
    contagem = {"com_cotacao": 0, "sem_cotacao": 0, "com_erro": 0}

    try:
        with cursor() as cur:
            for emb in req.embarques:
                try:
                    r = _cotar(cur, emb, quando, req.transportadoras,
                               req.incluir_descartadas, req.incluir_memoria)
                    resultados.append(r)
                    contagem["com_cotacao" if r.cotacoes else "sem_cotacao"] += 1
                except pymysql.err.OperationalError:
                    #  Banco caiu no meio do lote: não há como seguir, e
                    #  marcar 5.000 embarques como "com erro" esconderia a
                    #  causa. Sobe para o tratador de fora.
                    raise
                except Exception as e:
                    if req.parar_no_erro:
                        raise
                    contagem["com_erro"] += 1
                    #  O erro vai NO ITEM, com a referência: num lote de 5.000,
                    #  saber qual embarque falhou é a diferença entre arrumar e
                    #  recomeçar.
                    motivo = (f"{e.motivo.value}: {e.detalhe or ''}"
                              if isinstance(e, DescarteError) else str(e))
                    log.warning("lote: embarque %s falhou — %s",
                                emb.referencia, motivo)
                    resultados.append(RespostaCalculo(
                        referencia=emb.referencia, data_referencia=quando,
                        resumo_carga=ResumoCarga(
                            origem=_local(cur, emb.origem_cep),
                            destino=_local(cur, emb.destino_cep),
                            peso_bruto_kg=emb.peso_bruto_kg,
                            volume_m3=emb.volume_m3, valor_nf=emb.valor_nf),
                        erro=motivo))
    except pymysql.err.OperationalError as e:
        raise _banco_fora(e) from e

    duracao = int((time.time() - t0) * 1000)
    log.info("calcular-lote: %d com cotação, %d sem, %d com erro em %d ms",
             contagem["com_cotacao"], contagem["sem_cotacao"],
             contagem["com_erro"], duracao)
    return RespostaLote(
        data_referencia=quando,
        resumo=ResumoLote(embarques=len(req.embarques), duracao_ms=duracao,
                          **contagem),
        resultados=resultados,
    )


# ════════════════════════════════════════════════════════════════ erros
@app.exception_handler(NotImplementedError)
def _nao_implementado(request, exc: NotImplementedError):
    """Nada no motor levanta mais isto, mas o tratador fica.

    Se alguém acrescentar um passo novo e esquecer de implementá-lo, o sintoma
    é um 501 com o nome da função — não um 500 anônimo.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"detail": f"passo do motor ainda sem implementação: {exc}"},
    )
