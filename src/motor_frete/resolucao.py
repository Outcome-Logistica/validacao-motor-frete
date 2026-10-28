# -*- coding: utf-8 -*-
"""Resolução da tarifa — do CEP até a faixa de peso.

Esta é a metade do motor que **não calcula nada**: ela descobre quais números
entram na conta. Recebe "me cota de Fortaleza para o Rio, 120 kg, nota de
R$ 15.000, hoje" e devolve, por transportadora candidata, qual tabela vale,
como aquela transportadora classifica o destino, em que faixa o peso cai, qual
perfil de generalidades se aplica e qual alíquota incide. O `calculo.py` entra
depois e faz a aritmética.

A cadeia de três saltos vem do banco da parceira, confirmada pelo ER de
engenharia reversa:

    tabelas_frete_v2  →  tabelas_frete_peso  →  faixas_valores

## Descartar é informação

Cada passo que não retorna linha vira um `MotivoDescarte`, e a transportadora
volta na resposta com o motivo. Hoje, quando uma some da tela de cotação,
ninguém sabe por quê — e é o tipo de silêncio que faz o usuário desconfiar do
sistema inteiro.

## Ambiguidade também é informação

O cadastro da parceira tem casos em que duas tarifas diferentes servem a mesma
rota (ver `schema_tabelas_frete_duplicadas.sql` e
`schema_frete_peso_duplicados.sql`). Escolher uma em silêncio seria repetir o
defeito que este projeto existe para tirar. Então a escolha é explícita, está
documentada em `Aviso`, e viaja junto com a cotação.

Ver `docs/PROPOSTA_SCHEMA_COTACAO.html` no outcome_api para o raciocínio do
modelo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from . import generalidades as ger
from .calculo import D, ModoPeso, peso_taxado
from .esquemas import Aviso, Embarque, Local, MotivoDescarte

#  Vocabulário da parceira em `faixas_valores.tipo_calculo`. Não é o nosso —
#  ver o bloco 3 de schema_origem_dado.sql.
FAIXA = "Faixa"
EXCEDENTE = "Excedente"
EXCEDENTE_MINIMO = "ExcedenteMinimo"
FRETE_MINIMO = "Frete_Minimo"
IMPOSTO = "Imposto"
AD_VALORE = "AdValore"


class DescarteError(Exception):
    """Levantada quando um passo não encontra linha. Vira uma Descartada."""

    def __init__(self, motivo: MotivoDescarte, detalhe: str | None = None):
        self.motivo = motivo
        self.detalhe = detalhe
        super().__init__(f"{motivo.value}: {detalhe or ''}")


# ════════════════════════════════════════════════════════════════ estruturas
@dataclass(frozen=True)
class Faixas:
    """As linhas de `faixas_valores` de um frete-peso, separadas por espécie.

    As seis espécies não são alternativas — elas se **compõem** para formar uma
    tarifa. Um frete-peso típico tem uma ou mais `Faixa` (o preço por intervalo
    de peso), um `Excedente` (o R$/kg acima do teto) e um `Imposto` (a alíquota).

    E a armadilha que o dado esconde: **a coluna que carrega o preço depende da
    espécie.** `Faixa` e `Frete_Minimo` trazem em `minimo`; `Excedente`,
    `Imposto` e `AdValore` trazem em `valor`. Nenhuma linha tem as duas vazias.
    """

    bandas: tuple[dict, ...] = ()          # Faixa, ordenadas por peso_inicio
    excedente: Optional[dict] = None       # R$/kg acima do teto
    excedente_minimo: Optional[Decimal] = None
    frete_minimo: Optional[Decimal] = None
    imposto: Optional[Decimal] = None      # alíquota da própria tabela dela
    ad_valorem: Optional[Decimal] = None   # % sobre o valor da nota

    def vazia(self) -> bool:
        return not self.bandas and self.excedente is None

    @property
    def teto(self) -> Optional[Decimal]:
        """O peso máximo coberto pelas bandas de faixa."""
        if not self.bandas:
            return None
        fim = self.bandas[-1].get("peso_fim")
        return D(fim) if fim is not None else None

    def assinatura(self) -> tuple:
        """Identidade do conjunto, para comparar dois frete-peso candidatos.

        Dois frete-peso com a mesma assinatura cobram igual: a duplicata é pura
        e tanto faz qual escolher. Assinaturas diferentes significam **preços
        diferentes para a mesma rota**, e aí há decisão a tomar.
        """
        return (
            tuple((D(b.get("peso_inicio")), D(b.get("peso_fim")), D(b.get("minimo")))
                  for b in self.bandas),
            D((self.excedente or {}).get("valor")),
            self.excedente_minimo, self.frete_minimo, self.imposto, self.ad_valorem,
        )


@dataclass
class Contexto:
    """Tudo que o cálculo precisa de UMA transportadora candidata.

    É o contrato entre a resolução e o cálculo. Carrega também o rastreio — os
    ids das linhas que produziram cada número — porque sem ele um valor
    contestado não tem como ser defendido.
    """

    transportadora_id: int
    transportadora: str

    # cobertura
    regiao_atendida_id: int
    praca_id: int
    praca: str
    modalidade_id: int
    modalidade: str
    fator_cubagem: Decimal
    #  A UF de destino: é o `[ufdest]` das fórmulas, com 297 usos — o segundo
    #  mais citado depois do próprio [tst]. Sem ele, pedágio e SUFRAMA zeram.
    uf_destino: Optional[str] = None

    # vigência da rota
    prazo_dias: Optional[int] = None
    tipo_prazo: Optional[str] = None
    vigencia_inicio: Optional[date] = None
    vigencia_fim: Optional[date] = None
    tar_cid: Optional[str] = None
    #  A linha de vigência inteira, para `previsao_de_entrega` ler o calendário.
    calendario: Optional[dict] = None
    #  O `[tarcli]` das fórmulas: as letras dos serviços contratados pelo
    #  cliente de destino. Nulo quando o CNPJ não casa — e aí TDE, agendamento
    #  e pallet saem zero, o que a cotação avisa.
    tarcli: Optional[str] = None

    # a cadeia
    tabela_frete_id: Optional[int] = None
    nome_tabela: Optional[str] = None
    tabela_frete_peso_id: Optional[int] = None
    perfil_generalidade_id: Optional[int] = None

    # tarifa resolvida
    faixas: Faixas = field(default_factory=Faixas)
    faixa_valor_id: Optional[int] = None
    peso_taxado: Decimal = Decimal(0)
    preco_frete_peso: Decimal = Decimal(0)
    aplicou_excedente: bool = False
    frete_minimo: Optional[Decimal] = None
    perfil: Optional[ger.Perfil] = None
    icms: Decimal = Decimal(0)
    icms_de: str = "ICMS_UF"          # TABELA | ICMS_UF
    ad_valorem: Optional[Decimal] = None

    avisos: list[Aviso] = field(default_factory=list)

    def avisar(self, aviso: Aviso) -> None:
        if aviso not in self.avisos:
            self.avisos.append(aviso)


# ─────────────────────────────────────────────────── passo 1
def resolver_local(cur, cep: str) -> Local:
    """CEP → localidade. A cascata inteira está em cep.py.

    Resolve pelo cache, pela faixa de município, pela consulta externa (que
    devolve o código IBGE) ou, em último caso, pela faixa de UF. Todo acerto
    vira cache: um CEP novo custa uma consulta, uma vez só.

    Descarte: CEP_NAO_MAPEADO
    """
    from .cep import CepNaoMapeado, resolver as resolver_cep
    try:
        return resolver_cep(cur, cep)
    except CepNaoMapeado as e:
        raise DescarteError(MotivoDescarte.CEP_NAO_MAPEADO, str(e)) from e


# ─────────────────────────────────────────────────── passo 2
def rotas_atendidas(cur, origem: Local, destino: Local,
                    modalidades: list[str] | None = None,
                    transportadoras: list[int] | None = None) -> list[dict]:
    """Quais transportadoras atendem esta rota — e com que praça.

    A linha encontrada é o que **define** a praça de destino: praça não é
    atributo da localidade, é como aquela transportadora classifica aquele
    destino. A mesma cidade é CAPITAL para uma e INTERIOR 3 para outra, e isso
    muda o preço.

    Descarte: NAO_ATENDE_ROTA (quando volta vazio)
    """
    if not origem.localidade_id or not destino.localidade_id:
        #  A cascata de CEP parou no nível de UF. Cobertura é por município, e
        #  chutar "atende tudo no estado" produziria cotação para cidade que a
        #  transportadora não serve.
        raise DescarteError(
            MotivoDescarte.CEP_NAO_MAPEADO,
            f"cobertura é por município, e o CEP resolveu só até a UF "
            f"(origem={origem.uf or '?'}, destino={destino.uf or '?'}). "
            f"Habilite CEP_CONSULTA_EXTERNA ou carregue as faixas de CEP por município.")

    sql = ["""
        SELECT ra.id                AS regiao_atendida_id,
               ra.transportadora_id, t.razao_social,
               ra.praca_id,          p.nome  AS praca,
               ra.modalidade_id,     m.nome  AS modalidade,
               m.fator_cubagem
          FROM regioes_atendidas ra
          JOIN transportadoras t ON t.id = ra.transportadora_id AND t.ativo = 1
          JOIN pracas p          ON p.id = ra.praca_id
          JOIN modalidades m     ON m.id = ra.modalidade_id
         WHERE ra.localidade_origem_id  = %s
           AND ra.localidade_destino_id = %s"""]
    args: list[Any] = [origem.localidade_id, destino.localidade_id]

    if modalidades:
        sql.append(f" AND m.nome IN ({', '.join(['%s'] * len(modalidades))})")
        args += list(modalidades)
    if transportadoras:
        sql.append(f" AND ra.transportadora_id IN ({', '.join(['%s'] * len(transportadoras))})")
        args += list(transportadoras)

    sql.append(" ORDER BY t.razao_social, m.nome")
    cur.execute("".join(sql), args)
    return list(cur.fetchall())


# ─────────────────────────────────────────────────── passo 3
def vigencia_da_rota(cur, regiao_atendida_id: int, quando: date) -> dict:
    """Prazo, calendário e `tar_cid` da rota na data de referência.

    Descarte: ROTA_SEM_VIGENCIA

    Prazo nulo **não** descarta: no dado dela, 28% das 145.798 vigências vieram
    sem prazo. Sem prazo a cotação sai com `previsao_entrega` nula — o preço
    continua válido, e o que falta é informação de entrega, não de tarifa.
    """
    cur.execute("""
        SELECT id, prazo_dias, tipo_prazo, prazo_min, prazo_max, km_da_base,
               atende_seg, atende_ter, atende_qua, atende_qui,
               atende_sex, atende_sab, atende_dom,
               tar_cid, vigencia_inicio, vigencia_fim
          FROM regioes_atendidas_vigencia
         WHERE regiao_atendida_id = %s
           AND %s >= vigencia_inicio
           AND (vigencia_fim IS NULL OR %s <= vigencia_fim)
         ORDER BY vigencia_inicio DESC
         LIMIT 1""", (regiao_atendida_id, quando, quando))
    v = cur.fetchone()
    if not v:
        raise DescarteError(
            MotivoDescarte.ROTA_SEM_VIGENCIA,
            f"a rota {regiao_atendida_id} não tem vigência que cubra {quando:%d/%m/%Y}")
    return v


# ─────────────────────────────────────────────────── passo 4
def tabelas_vigentes(cur, transportadora_id: int, uf_origem: str, modalidade_id: int,
                     tipo_frete: str | None, tipo_nf: str | None,
                     quando: date) -> list[dict]:
    """As tabelas de frete válidas nesta data — salto 1 da cadeia.

    Devolve **lista**, não uma. A UNIQUE que garantiria unicidade foi removida
    porque o cadastro dela não a respeita: 63 das 164 tabelas repetem a
    combinação (transportadora, UF, modalidade, tipo, data de início), e 19
    continuam repetindo mesmo incluindo a data de fim. Quem escolhe é
    `escolher_tabela()`, e a escolha aparece como aviso.
    """
    sql = ["""
        SELECT tf.id, tf.nome_tabela, tf.data_inicio, tf.data_fim, tf.tipo_nf,
               (SELECT COUNT(*) FROM tabelas_frete_peso fp
                 WHERE fp.tabela_frete_id = tf.id) AS qtd_frete_peso
          FROM tabelas_frete_v2 tf
          JOIN ufs u ON u.id = tf.uf_origem_id
         WHERE tf.transportadora_id = %s
           AND u.sigla              = %s
           AND tf.modalidade_id     = %s
           AND tf.ativo = 1
           AND %s >= tf.data_inicio
           AND (tf.data_fim IS NULL OR %s <= tf.data_fim)
           AND (tf.tipo_nf IS NULL OR %s IS NULL OR tf.tipo_nf = %s)"""]
    args: list[Any] = [transportadora_id, uf_origem, modalidade_id,
                       quando, quando, tipo_nf, tipo_nf]

    if tipo_frete:
        sql.append(" AND tf.tipo_frete_id = "
                   "(SELECT id FROM tipos_frete WHERE nome = %s)")
        args.append(tipo_frete)

    sql.append(" ORDER BY tf.data_inicio DESC, tf.id DESC")
    cur.execute("".join(sql), args)
    achadas = list(cur.fetchall())
    if not achadas:
        raise DescarteError(
            MotivoDescarte.SEM_TABELA_VIGENTE,
            f"nenhuma tabela de {uf_origem} vigente em {quando:%d/%m/%Y}")
    return achadas


def escolher_tabela(candidatas: list[dict], ctx: Contexto) -> dict:
    """Qual tabela usar quando o cadastro oferece mais de uma.

    A regra, registrada em `schema_tabelas_frete_duplicadas.sql`:

      1. descarta as que não têm frete-peso — não produzem preço;
      2. se sobrar mais de uma, usa a de `data_inicio` mais recente;
      3. se ainda empatar, usa o maior id (cadastro mais novo) e **avisa**.

    O passo 3 é escolha arbitrária. É por isso que ele avisa: das 63 tabelas
    duplicadas, 7 têm frete-peso, então a ambiguidade chega ao cálculo em 7
    casos — poucos, mas todos em transportadoras que cotam.
    """
    uteis = [c for c in candidatas if (c.get("qtd_frete_peso") or 0) > 0]
    if not uteis:
        raise DescarteError(
            MotivoDescarte.SEM_TABELA_VIGENTE,
            f"as {len(candidatas)} tabela(s) vigente(s) não têm nenhuma linha de "
            f"frete-peso — não há de onde tirar preço")

    if len(uteis) > 1:
        #  Já vêm ordenadas por data_inicio DESC, id DESC.
        mais_nova = uteis[0]["data_inicio"]
        empatadas = [c for c in uteis if c["data_inicio"] == mais_nova]
        if len(empatadas) > 1:
            ctx.avisar(Aviso.TABELA_AMBIGUA)
        return empatadas[0]
    return uteis[0]


# ─────────────────────────────────────────────────── passo 5
def aceita_produtos(cur, tabela_frete_id: int, categorias: list[str] | None) -> None:
    """A tabela aceita estes produtos?

    Semântica de `tabelas_frete_categorias`:

      · nenhuma linha para a tabela  → aceita qualquer produto
      · linhas ACEITA                → whitelist, só as listadas
      · linhas RECUSA                → blacklist, e RECUSA vence ACEITA

    Descarte: PRODUTO_NAO_ACEITO

    Hoje nenhuma tabela tem linha: `tb_embalagens.categoria_produto` veio 0%
    preenchido no dado dela. Então o passo não descarta ninguém — mas a
    semântica fica pronta para quando ela mandar, e é o que o orientador pediu
    na Parte 3 do áudio.
    """
    cur.execute("""
        SELECT cp.nome, tfc.regra
          FROM tabelas_frete_categorias tfc
          JOIN categorias_produto cp ON cp.id = tfc.categoria_produto_id
         WHERE tfc.tabela_frete_id = %s""", (tabela_frete_id,))
    regras = list(cur.fetchall())
    if not regras:
        return

    pedidas = {c.strip().upper() for c in (categorias or []) if c and c.strip()}
    recusadas = {r["nome"].upper() for r in regras if r["regra"] == "RECUSA"}
    aceitas = {r["nome"].upper() for r in regras if r["regra"] == "ACEITA"}

    barradas = pedidas & recusadas
    if barradas:
        raise DescarteError(MotivoDescarte.PRODUTO_NAO_ACEITO,
                            f"a tabela recusa: {', '.join(sorted(barradas))}")
    if aceitas and pedidas and not (pedidas <= aceitas):
        fora = pedidas - aceitas
        raise DescarteError(MotivoDescarte.PRODUTO_NAO_ACEITO,
                            f"fora da lista de produtos aceitos: {', '.join(sorted(fora))}")


# ─────────────────────────────────────────────────── passo 6
def fretes_peso(cur, tabela_frete_id: int, praca_id: int, uf_destino: str) -> list[dict]:
    """Salto 2 — e traz o perfil de generalidades deste destino.

    Atenção ao que isto significa: **o perfil pende daqui, não da tabela.** GRIS,
    pedágio e despacho variam por praça e UF de destino. Foi o que o ER de
    engenharia reversa revelou, e é o erro que teríamos repetido se tivéssemos
    migrado o JSON antigo mantendo a hierarquia da transportadora.

    Devolve lista pelo mesmo motivo do passo 4: a UNIQUE saiu porque o dado dela
    repete a chave — e aqui a repetição é pior, porque em 8 dos 10 casos os dois
    registros cobram **preços diferentes**.

    Descarte: SEM_FRETE_PESO_PARA_PRACA
    """
    cur.execute("""
        SELECT fp.id, fp.perfil_generalidade_id, pg.cubagem, pg.nome AS perfil
          FROM tabelas_frete_peso fp
          JOIN ufs u ON u.id = fp.uf_destino_id
          LEFT JOIN perfis_generalidade pg ON pg.id = fp.perfil_generalidade_id
         WHERE fp.tabela_frete_id = %s
           AND fp.praca_id        = %s
           AND u.sigla            = %s
         ORDER BY fp.id""", (tabela_frete_id, praca_id, uf_destino))
    achados = list(cur.fetchall())
    if not achados:
        raise DescarteError(
            MotivoDescarte.SEM_FRETE_PESO_PARA_PRACA,
            f"a tabela {tabela_frete_id} não tem preço para a praça {praca_id} "
            f"com destino {uf_destino}")
    return achados


# ─────────────────────────────────────────────────── passo 7
def faixas_do_frete_peso(cur, tabela_frete_peso_id: int) -> Faixas:
    """Salto 3 — todas as linhas de faixa, separadas por espécie.

    Não é "achar a faixa do peso": é montar a tarifa. As seis espécies se
    compõem, e três delas (Imposto, AdValore, Frete_Minimo) não têm faixa de
    peso nenhuma — incidem sobre o frete ou sobre a nota.
    """
    cur.execute("""
        SELECT id, peso_inicio, peso_fim, minimo, valor, tipo_calculo
          FROM faixas_valores
         WHERE tabela_frete_peso_id = %s
         ORDER BY tipo_calculo, peso_inicio""", (tabela_frete_peso_id,))
    linhas = list(cur.fetchall())

    bandas = sorted((l for l in linhas if l["tipo_calculo"] == FAIXA),
                    key=lambda l: D(l["peso_inicio"]))
    um = lambda tipo: next((l for l in linhas if l["tipo_calculo"] == tipo), None)

    exc_min = um(EXCEDENTE_MINIMO)
    fmin = um(FRETE_MINIMO)
    imp = um(IMPOSTO)
    adv = um(AD_VALORE)

    return Faixas(
        bandas=tuple(bandas),
        excedente=um(EXCEDENTE),
        #  ExcedenteMinimo traz as duas colunas preenchidas; o piso é o `minimo`.
        excedente_minimo=D(exc_min["minimo"]) if exc_min else None,
        frete_minimo=D(fmin["minimo"]) if fmin else None,
        imposto=D(imp["valor"]) if imp else None,
        ad_valorem=D(adv["valor"]) if adv else None,
    )


# ─────────────────────────────────────────────────── passo 8
def preco_da_faixa(faixas: Faixas, peso: Decimal) -> tuple[Decimal, Optional[int], bool]:
    """Em que faixa o peso taxado cai, e quanto custa.

    Devolve (preço, id da linha usada, se aplicou excedente).

    Duas coisas que o dado ensina e que o nome "faixa de valor" esconde:

    **O preço de uma `Faixa` está em `minimo`, não em `valor`.** As 3.585 linhas
    de faixa têm `valor` vazio. Eu havia lido isso como lacuna de cadastro e
    relatado "59% das faixas sem valor" — estava errado, a coluna usada depende
    da espécie.

    **Acima do teto, soma-se o excedente.** Para 100 kg numa tabela que termina
    em 50–70 kg por R$ 161,88, com excedente de R$ 2,32/kg:

        161,88 + (100 − 70) × 2,32  =  R$ 231,48

    É `pendencias.MODO_EXCEDENTE = ULTIMA_FAIXA_MAIS_EXCESSO`, e é o desenho
    clássico da tabela de frete brasileira — o que dá continuidade de preço no
    ponto de virada. A outra leitura possível (100 × 2,32 = R$ 232,00) dá número
    PRÓXIMO, o que é pior: o erro não salta aos olhos. Pendente de confirmação.

    Descarte: PESO_FORA_DAS_FAIXAS
    """
    peso = D(peso)

    for b in faixas.bandas:
        ini = D(b["peso_inicio"])
        fim = b["peso_fim"]
        if peso >= ini and (fim is None or peso <= D(fim)):
            return D(b["minimo"]), b["id"], False

    exc = faixas.excedente
    if exc is not None:
        limite = faixas.teto
        base = D(faixas.bandas[-1]["minimo"]) if faixas.bandas else Decimal(0)
        inicio_exc = D(exc["peso_inicio"]) if exc.get("peso_inicio") is not None else limite
        corte = limite if limite is not None else inicio_exc
        fim_exc = exc.get("peso_fim")

        if corte is None:
            #  Sem banda de faixa: o excedente é a tarifa inteira, R$/kg.
            preco = peso * D(exc["valor"])
        elif peso > corte and (fim_exc is None or peso <= D(fim_exc)):
            preco = base + (peso - corte) * D(exc["valor"])
        else:
            preco = None

        if preco is not None:
            if faixas.excedente_minimo is not None:
                preco = max(preco, faixas.excedente_minimo)
            return preco, exc["id"], True

    teto = faixas.teto
    raise DescarteError(
        MotivoDescarte.PESO_FORA_DAS_FAIXAS,
        f"{peso} kg está fora das faixas cadastradas"
        + (f" (a tabela vai até {teto} kg e não tem excedente)" if teto else ""))


def escolher_frete_peso(cur, candidatos: list[dict], peso: Decimal,
                        ctx: Contexto) -> tuple[dict, Faixas]:
    """Qual frete-peso usar quando o cadastro oferece mais de um.

    A regra, registrada em `schema_frete_peso_duplicados.sql`:

      1. se as faixas dos candidatos são idênticas, tanto faz — não há
         ambiguidade de fato;
      2. se diferem, cota pela **mais cara** para este peso e **avisa**.

    A escolha pela mais cara é deliberada e não é neutra: numa cotação, errar
    para baixo vira prejuízo na fatura, e errar para cima só perde uma
    comparação. Mas é escolha nossa, não regra dela — daí o aviso.

    O caso real: tabela 209, praça 0, UF destino 11 tem um registro cobrando
    R$ 200 até 10 kg com R$ 7,50/kg de excedente, e outro cobrando R$ 190 com
    R$ 6,00/kg. Mesmo perfil, `obs` nulo nos dois, nada os distingue.
    """
    lidos = [(c, faixas_do_frete_peso(cur, c["id"])) for c in candidatos]
    validos = [(c, f) for c, f in lidos if not f.vazia()]
    if not validos:
        raise DescarteError(
            MotivoDescarte.PESO_FORA_DAS_FAIXAS,
            f"o frete-peso existe mas não tem nenhuma faixa de preço")

    if len(validos) == 1:
        return validos[0]

    if len({f.assinatura() for _, f in validos}) == 1:
        return validos[0]                       # duplicata pura

    ctx.avisar(Aviso.TARIFA_AMBIGUA)
    melhor = None
    for c, f in validos:
        try:
            preco, _, _ = preco_da_faixa(f, peso)
        except DescarteError:
            continue
        if melhor is None or preco > melhor[0]:
            melhor = (preco, c, f)
    if melhor is None:
        #  Nenhum candidato cobre este peso: deixa o primeiro levantar o
        #  descarte com a mensagem certa.
        return validos[0]
    return melhor[1], melhor[2]


# ─────────────────────────────────────────────────── passo 9
def perfil_de_generalidade(cur, perfil_generalidade_id: int | None) -> Optional[ger.Perfil]:
    """As fórmulas de generalidade do perfil, compiladas.

    Cada linha de `perfis_generalidade_itens` traz `coluna_origem` (o nome da
    coluna na planilha dela: gris, pedagio, tde, tst…) e `expressao` (a fórmula).
    Remontamos o dicionário coluna → fórmula e entregamos ao
    `generalidades.Perfil`, que compila e resolve a ordem de avaliação.

    Perfil nulo é caso legítimo: 12 dos 1.018 frete-peso não apontam para perfil
    nenhum. Cotam sem generalidade, com o VN3 valendo zero.
    """
    if not perfil_generalidade_id:
        return None

    cur.execute("""
        SELECT pgi.coluna_origem, pgi.expressao, pg.cubagem, pg.nome
          FROM perfis_generalidade_itens pgi
          JOIN perfis_generalidade pg ON pg.id = pgi.perfil_generalidade_id
          JOIN componentes_tarifa ct  ON ct.id = pgi.componente_tarifa_id
         WHERE pgi.perfil_generalidade_id = %s
           AND ct.ativo = 1
           AND pgi.expressao IS NOT NULL
           AND pgi.coluna_origem IS NOT NULL""", (perfil_generalidade_id,))
    linhas = list(cur.fetchall())
    if not linhas:
        return None

    bruto: dict[str, Any] = {l["coluna_origem"]: l["expressao"] for l in linhas}
    if linhas[0]["cubagem"] is not None:
        bruto[ger.COLUNA_CUBAGEM] = linhas[0]["cubagem"]
    return ger.Perfil.de_linha(perfil_generalidade_id, bruto)


# ─────────────────────────────────────────────────── passo 10
def aliquota_icms(cur, uf_origem: str, uf_destino: str, quando: date) -> Decimal:
    """ICMS interestadual na data — tabelado e público, não depende da parceira.

    Vem da Resolução do Senado Federal nº 22/1989: 7% para destinos no Norte,
    Nordeste, Centro-Oeste e Espírito Santo; 12% para os demais; a alíquota
    interna quando origem e destino são a mesma UF.

    Descarte: SEM_ICMS_PARA_UF

    Isto é o **plano B**. Quando o frete-peso traz uma linha `Imposto`, ela
    manda — ver `icms_da_tarifa()`. Hoje o valor está fixo em 18% no JSON da
    cotação e 12% no agente da Lais; nenhum dos dois com respaldo no dado.
    """
    cur.execute("""
        SELECT i.aliquota
          FROM icms_uf i
          JOIN ufs o ON o.id = i.uf_origem_id
          JOIN ufs d ON d.id = i.uf_destino_id
         WHERE o.sigla = %s AND d.sigla = %s
           AND %s >= i.vigencia_inicio
           AND (i.vigencia_fim IS NULL OR %s <= i.vigencia_fim)
         ORDER BY i.vigencia_inicio DESC
         LIMIT 1""", (uf_origem, uf_destino, quando, quando))
    r = cur.fetchone()
    if not r:
        raise DescarteError(
            MotivoDescarte.SEM_ICMS_PARA_UF,
            f"sem alíquota cadastrada de {uf_origem} para {uf_destino}")
    return D(r["aliquota"])


def icms_da_tarifa(cur, faixas: Faixas, uf_origem: str, uf_destino: str,
                   quando: date, ctx: Contexto) -> Decimal:
    """A alíquota que incide — da tabela dela, ou da nossa tabela de ICMS.

    A linha `Imposto` de `faixas_valores` tem **três valores em 989 linhas**:
    0,12 (826×), 0,04 (118×) e 0,18 (45×). São alíquotas de ICMS, e variam por
    UF de destino — exatamente como o ICMS varia. É a alíquota que a própria
    transportadora aplica naquela rota, e ela ganha da nossa tabela genérica:
    o objetivo do motor é reproduzir a fatura, não a legislação.

    Quando o frete-peso não tem linha `Imposto` (29 dos 1.018), cai para
    `icms_uf`, e a cotação registra de onde veio em `Contexto.icms_de`.
    """
    if faixas.imposto is not None:
        ctx.icms_de = "TABELA"
        return faixas.imposto
    ctx.icms_de = "ICMS_UF"
    ctx.avisar(Aviso.ICMS_ESTIMADO)
    return aliquota_icms(cur, uf_origem, uf_destino, quando)


# ─────────────────────────────────────────────────── passo 11
def codigo_servicos_cliente(cur, transportadora_id: int,
                            cnpj_destino: str | None) -> Optional[str]:
    """O `[tarcli]` das fórmulas — `tarifas_cliente_destino` (tb_tarcli).

    ## Isto era uma pendência e o dado respondeu

    Eu tinha registrado que a semântica de `tarcli` era desconhecida: código de
    tabela alternativa, percentual de desconto ou valor absoluto. **Nenhuma das
    três.** Nas 146.032 linhas há 195 valores distintos, e todos são cadeias
    curtas de LETRAS, no máximo 8 caracteres:

        'D'      136.056 linhas     'A'   17.028     'C'   8.873
        'AD'       5.342            'P'    4.522     'S'   4.891
        'AD1D'     1.838            'V'    3.743     'O'   2.631

    E as letras casam exatamente com o que as fórmulas procuram:

        tde       = se(contem('D',[tarcli]), ...)
        agendar   = se(contem('A',[tarcli]), ...)
        pallet    = se(contem('P',[tarcli]), ...)

    `tarcli` é a **lista de serviços contratados** por aquele cliente naquela
    transportadora, uma letra por serviço. Não é preço: é o que liga e desliga
    cada cobrança.

    ## Por que isto importa muito

    'D' aparece em 136 mil das 146 mil linhas. Sem resolver o `[tarcli]`, o TDE
    sai zero em quase toda cotação — e o frete fica barato em silêncio, que é o
    pior defeito possível. Quando não casa, a cotação leva
    `Aviso.SEM_CODIGO_SERVICOS`.

    ## O casamento do CNPJ

    A coluna veio com largura irregular: 131.598 linhas com 14 dígitos, 11.040
    com 13, 2.126 com 11, e por aí. Os de 13 e 12 são quase certamente CNPJ que
    perdeu zero à esquerda na exportação (a planilha guardou como número).
    Então: tenta igualdade exata primeiro, que usa o índice; se não achar,
    compara numericamente, o que desfaz o zero perdido sem supor largura.
    """
    if not cnpj_destino:
        return None

    so_digitos = "".join(c for c in str(cnpj_destino) if c.isdigit())
    if not so_digitos:
        return None

    cur.execute("""
        SELECT tarcli FROM tarifas_cliente_destino
         WHERE transportadora_id = %s AND cnpj_destino = %s
         LIMIT 1""", (transportadora_id, so_digitos))
    r = cur.fetchone()
    if r:
        return r["tarcli"]

    #  Plano B: compara pelo valor numérico. Resolve o zero perdido na
    #  exportação sem chutar a largura do campo.
    cur.execute("""
        SELECT tarcli FROM tarifas_cliente_destino
         WHERE transportadora_id = %s
           AND CAST(cnpj_destino AS UNSIGNED) = CAST(%s AS UNSIGNED)
         LIMIT 1""", (transportadora_id, so_digitos))
    r = cur.fetchone()
    return r["tarcli"] if r else None


# ════════════════════════════════════════════════════════════════ orquestração
def previsao_de_entrega(emissao: date, prazo_dias: int | None,
                        vigencia: dict | None = None) -> Optional[date]:
    """Data prevista, respeitando o calendário de atendimento quando existe.

    O calendário veio preenchido em 634 das 152.141 vigências (0,4%). Sem ele, a
    conta é dia corrido — e **isso é suposição**, não dado: uma transportadora
    que não entrega no domingo tem o prazo real maior que o calculado. Fica
    registrado aqui em vez de escondido numa soma.
    """
    if prazo_dias is None:
        return None

    dias = ("atende_seg", "atende_ter", "atende_qua", "atende_qui",
            "atende_sex", "atende_sab", "atende_dom")
    cal = [(vigencia or {}).get(d) for d in dias]
    if all(c is None for c in cal):
        return emissao + timedelta(days=int(prazo_dias))

    atende = [bool(c) for c in cal]
    if not any(atende):
        return None                      # cadastro diz que não atende dia nenhum

    data, restantes = emissao, int(prazo_dias)
    while restantes > 0:
        data += timedelta(days=1)
        if atende[data.weekday()]:       # weekday(): 0 = segunda
            restantes -= 1
    return data


def resolver(cur, embarque: Embarque, quando: date,
             transportadoras: list[int] | None = None
             ) -> tuple[list[Contexto], list[dict]]:
    """Os onze passos, de ponta a ponta.

    Devolve `(contextos, descartadas)`. Cada contexto é uma transportadora
    pronta para o `calculo.py`; cada descartada traz o motivo de ter saído.

    Uma transportadora pode aparecer mais de uma vez nos contextos quando atende
    a rota em **mais de uma modalidade** — rodoviário e aéreo são cotações
    distintas, com preço e prazo distintos, e escolher uma por ela seria decidir
    no lugar do usuário.
    """
    contextos: list[Contexto] = []
    descartadas: list[dict] = []

    def descartar(rota: dict, erro: DescarteError) -> None:
        descartadas.append({
            "transportadora_id": rota["transportadora_id"],
            "transportadora": rota["razao_social"],
            "motivo": erro.motivo,
            "detalhe": erro.detalhe,
        })

    # passo 1 · os dois CEPs. Falha aqui derruba o embarque todo, não uma
    # transportadora: sem origem e destino não há o que cotar.
    origem = resolver_local(cur, embarque.origem_cep)
    destino = resolver_local(cur, embarque.destino_cep)

    # passo 2 · quem atende
    rotas = rotas_atendidas(cur, origem, destino,
                            embarque.modalidades, transportadoras)
    if not rotas:
        return [], [{
            "transportadora_id": 0,
            "transportadora": "(nenhuma)",
            "motivo": MotivoDescarte.NAO_ATENDE_ROTA,
            "detalhe": f"nenhuma transportadora ativa atende "
                       f"{origem.localidade}/{origem.uf} → {destino.localidade}/{destino.uf}",
        }]

    for rota in rotas:
        ctx = Contexto(
            transportadora_id=rota["transportadora_id"],
            transportadora=rota["razao_social"],
            regiao_atendida_id=rota["regiao_atendida_id"],
            praca_id=rota["praca_id"],
            praca=rota["praca"],
            modalidade_id=rota["modalidade_id"],
            modalidade=rota["modalidade"],
            fator_cubagem=D(rota["fator_cubagem"] or 300),
            uf_destino=destino.uf,
        )
        try:
            # passo 3 · a rota está vigente hoje?
            vig = vigencia_da_rota(cur, ctx.regiao_atendida_id, quando)
            ctx.prazo_dias = vig["prazo_dias"]
            ctx.tipo_prazo = vig["tipo_prazo"]
            ctx.vigencia_inicio = vig["vigencia_inicio"]
            ctx.vigencia_fim = vig["vigencia_fim"]
            ctx.tar_cid = vig["tar_cid"]        # a variável [tarcid] das fórmulas
            ctx.calendario = vig
            if ctx.prazo_dias is None:
                ctx.avisar(Aviso.SEM_PRAZO)
            elif all(vig.get(d) is None for d in
                     ("atende_seg", "atende_ter", "atende_qua", "atende_qui",
                      "atende_sex", "atende_sab", "atende_dom")):
                #  Sem calendário, o prazo é contado em dia corrido. É suposição,
                #  e fica dita em vez de escondida na soma.
                ctx.avisar(Aviso.CALENDARIO_SUPOSTO)

            # passo 4 · qual tabela vale
            candidatas = tabelas_vigentes(
                cur, ctx.transportadora_id, origem.uf, ctx.modalidade_id,
                embarque.tipo_frete, embarque.tipo_nf, quando)
            tabela = escolher_tabela(candidatas, ctx)
            ctx.tabela_frete_id = tabela["id"]
            ctx.nome_tabela = tabela["nome_tabela"]

            # passo 5 · a tabela aceita o produto
            aceita_produtos(cur, ctx.tabela_frete_id, embarque.categorias_produto)

            # passo 6 · o frete-peso da praça + UF destino
            candidatos = fretes_peso(cur, ctx.tabela_frete_id,
                                     ctx.praca_id, destino.uf)

            # o fator de cubagem do perfil sobrepõe o da modalidade, então o
            # peso taxado só fecha depois de saber qual perfil vale
            perfil_provisorio = perfil_de_generalidade(
                cur, candidatos[0]["perfil_generalidade_id"])
            if perfil_provisorio and perfil_provisorio.fator_cubagem:
                ctx.fator_cubagem = perfil_provisorio.fator_cubagem

            ctx.peso_taxado = peso_taxado(
                embarque.peso_bruto_kg, embarque.volume_m3, ctx.fator_cubagem,
                ModoPeso.MEIO_PARA_CIMA)

            # passos 7 e 8 · a tarifa e o preço da faixa
            escolhido, faixas = escolher_frete_peso(
                cur, candidatos, ctx.peso_taxado, ctx)
            ctx.tabela_frete_peso_id = escolhido["id"]
            ctx.perfil_generalidade_id = escolhido["perfil_generalidade_id"]
            ctx.faixas = faixas

            preco, faixa_id, com_excedente = preco_da_faixa(faixas, ctx.peso_taxado)
            ctx.preco_frete_peso = preco
            ctx.faixa_valor_id = faixa_id
            ctx.aplicou_excedente = com_excedente
            ctx.frete_minimo = faixas.frete_minimo
            ctx.ad_valorem = faixas.ad_valorem

            # passo 9 · as fórmulas de generalidade deste destino
            ctx.perfil = (perfil_provisorio
                          if escolhido is candidatos[0]
                          else perfil_de_generalidade(
                              cur, escolhido["perfil_generalidade_id"]))

            # passo 10 · a alíquota
            ctx.icms = icms_da_tarifa(cur, faixas, origem.uf, destino.uf, quando, ctx)

            # passo 11 · as letras dos serviços contratados
            ctx.tarcli = codigo_servicos_cliente(
                cur, ctx.transportadora_id, embarque.cnpj_destino)
            if ctx.tarcli is None:
                #  Sem as letras, as generalidades condicionais saem zero. Em
                #  136 mil das 146 mil linhas de tb_tarcli existe 'D' (TDE), e
                #  perder o TDE barateia a cotação em silêncio.
                ctx.avisar(Aviso.SEM_CODIGO_SERVICOS)

            contextos.append(ctx)

        except DescarteError as e:
            descartar(rota, e)

    return contextos, descartadas
