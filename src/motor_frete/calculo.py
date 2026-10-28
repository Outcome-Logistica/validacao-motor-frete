# -*- coding: utf-8 -*-
"""O cálculo de frete — VN1 a VN6.

    VN1  peso taxado      max(peso_bruto, m³ × fator_cubagem)
    VN2  frete-peso       max(fp_minimo, round(VN1, fp_arred) × fp_valor)
    VN3  generalidades    Σ por componente, cada um com base, mínimo e máximo
    VN4  sobre-frete      (VN2 + VN3) × sobre_frete
    VN5  ICMS             (VN2 + VN3 + VN4) × icms
    VN6  total            max(minimo_geral, VN2 + VN3 + VN4 + VN5)

    frete_liquido = VN2 + VN3 + VN4      (antes do ICMS)
    frete_total   = VN6

Referência: `freight_calculation.md`, do repositório do agente de recomendação —
a única especificação escrita destas fórmulas no projeto. As duas cópias dela
(crew e langgraph) são idênticas, conferido.

────────────────────────────────────────────────────────────────────────────────
DUAS DIVERGÊNCIAS ENTRE OS MOTORES EXISTENTES, E O QUE FOI FEITO COM ELAS

Ao comparar as duas implementações, apareceram dois pontos onde elas discordam.
Nenhum é erro de digitação: são leituras diferentes da mesma especificação.

1 · ONDE ARREDONDA
   `quote_lookup.py` (Lais) arredonda para 2 casas a cada nível: VN2, VN3, VN4,
   frete_liquido, VN5 e VN6. E calcula o VN5 sobre o frete_liquido JÁ arredondado.
   `cotacao_router.py` (Pedro) só arredonda na saída — os níveis intermediários
   carregam todas as casas decimais.

   Consequência: os dois devolvem números diferentes para a mesma carga, por
   acúmulo de arredondamento. Nada grande, centavos — mas suficiente para a
   auditoria acusar divergência num CT-e correto.

   Resolvido com POLÍTICA EXPLÍCITA, não escolhendo um lado em silêncio:
     · POR_NIVEL  — arredonda a cada nível. É o padrão, porque é o que casa
                    com o CT-e: lá cada componente é um valor já arredondado,
                    e o total é a soma dos arredondados.
     · SO_NO_FIM  — arredonda só na saída. Reproduz o motor atual, e é o modo
                    usado pelo teste de regressão.

2 · O ARREDONDAMENTO POR LINHA DE GENERALIDADE
   O motor do Pedro arredonda a BASE antes de multiplicar:
   `round(valor_base, gen.arred) * gen.valor`. O campo `arred` vive dentro de
   cada generalidade, junto do min e do max — então é mesmo um parâmetro por
   linha. A implementação da Lais não tem esse campo e ignora o passo.

   Adotado o comportamento do Pedro: o campo existe na estrutura da parceira, e
   descartá-lo seria perder informação de tarifa.

3 · MEIO PARA CIMA, NÃO BANKER'S
   O `round()` do Python arredonda 0,5 para o par mais próximo: round(78.5) = 78.
   Em dinheiro e em peso taxado, a convenção é meio para cima — 78,5 vira 79.
   Com `fp_arred = 0`, isso muda o peso em 1 kg, que × tarifa dá reais, não
   centavos. Por isso Decimal com ROUND_HALF_UP, e não float com round().

   PENDENTE DE CONFIRMAÇÃO com a parceira: o arredondamento do peso taxado é
   meio para cima, sempre para cima, ou truncado? Transportadora que arredonda
   sempre para cima é comum. Ver `ModoPeso` abaixo.
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Optional

from .esquemas import BaseIncidencia, Componente

DOIS = Decimal("0.01")


class Politica(str, Enum):
    """Quando arredondar para 2 casas."""
    POR_NIVEL = "POR_NIVEL"   # a cada nível — casa com o CT-e (padrão)
    SO_NO_FIM = "SO_NO_FIM"   # só na saída — reproduz o cotacao_router


class ModoPeso(str, Enum):
    """Como arredondar o peso taxado. A confirmar com a parceira."""
    MEIO_PARA_CIMA = "MEIO_PARA_CIMA"   # 78,5 → 79   (convenção, padrão)
    SEMPRE_PARA_CIMA = "SEMPRE_PARA_CIMA"  # 78,1 → 79   (comum em transportadora)
    TRUNCA = "TRUNCA"                    # 78,9 → 78


def D(v) -> Decimal:
    """Converte para Decimal sem passar por float — float perde centavo."""
    if isinstance(v, Decimal):
        return v
    if v is None:
        return Decimal(0)
    try:
        return Decimal(str(v))
    except InvalidOperation as e:
        raise ValueError(f"valor não numérico no cálculo: {v!r}") from e


def arredondar(valor, casas: int = 2, modo=ROUND_HALF_UP) -> Decimal:
    """Arredondamento meio para cima — a convenção para dinheiro no Brasil."""
    q = Decimal(1).scaleb(-casas)
    return D(valor).quantize(q, rounding=modo)


# ════════════════════════════════════════════════════════ parâmetros de entrada
@dataclass(frozen=True)
class ItemGeneralidade:
    """Uma linha de generalidade da tarifa."""
    codigo: str
    nome: str
    base: BaseIncidencia
    valor: Decimal
    minimo: Optional[Decimal] = None
    maximo: Optional[Decimal] = None
    arredondamento: int = 2   # casas para arredondar a BASE antes de multiplicar


@dataclass(frozen=True)
class Tarifa:
    """Tudo que o cálculo precisa, já normalizado.

    Serve tanto ao JSON antigo (pelo adaptador abaixo) quanto às tabelas novas,
    e é de propósito: o núcleo do cálculo não sabe de onde o dado veio.
    """
    fp_minimo: Decimal = Decimal(0)
    fp_valor: Decimal = Decimal(0)
    fp_arred: int = 0
    generalidades: tuple[ItemGeneralidade, ...] = ()
    sobre_frete: Decimal = Decimal(0)
    icms: Decimal = Decimal(0)
    minimo_geral: Decimal = Decimal(0)
    fator_cubagem: Decimal = Decimal(300)
    tipo_calculo: str = "POR_KG"   # POR_KG | FIXO | PERCENTUAL_VALOR_NF


@dataclass
class Carga:
    peso_bruto: Decimal
    volume_m3: Decimal = Decimal(0)
    valor_nf: Decimal = Decimal(0)
    valor_mercadoria: Optional[Decimal] = None
    quantidade_volumes: int = 1


@dataclass
class Niveis:
    """O resultado, nível a nível, com a memória de como cada um foi obtido."""
    peso_taxado: Decimal          # VN1
    frete_peso: Decimal           # VN2
    generalidades: Decimal        # VN3
    sobre_frete: Decimal          # VN4
    icms: Decimal                 # VN5
    frete_liquido: Decimal
    frete_total: Decimal          # VN6
    aplicou_minimo_geral: bool = False
    aplicou_minimo_frete_peso: bool = False
    componentes: list[Componente] = field(default_factory=list)
    memoria: dict = field(default_factory=dict)


# ════════════════════════════════════════════════════════ os níveis
def peso_taxado(peso_bruto, volume_m3, fator_cubagem,
                modo: ModoPeso = ModoPeso.MEIO_PARA_CIMA, casas: int = 2) -> Decimal:
    """VN1 — o maior entre o peso real e o peso cubado.

    O fator de cubagem vem do perfil de generalidades ou, na falta, da
    modalidade. Era constante 300 no cotacao_router, o que só vale para
    rodoviário: aéreo usa ~167 e cabotagem ~500.
    """
    cubado = D(volume_m3) * D(fator_cubagem)
    bruto = max(D(peso_bruto), cubado)
    if modo is ModoPeso.SEMPRE_PARA_CIMA:
        return arredondar(bruto, casas, ROUND_CEILING)
    if modo is ModoPeso.TRUNCA:
        return arredondar(bruto, casas, ROUND_DOWN)
    return arredondar(bruto, casas)


def frete_peso(vn1: Decimal, t: Tarifa, valor_nf: Decimal = Decimal(0)) -> tuple[Decimal, bool]:
    """VN2 — aplica a tarifa de peso, respeitando o mínimo.

    Devolve (valor, aplicou_minimo). Saber se o mínimo pegou importa: é a peça
    central da queixa da parceira sobre fracionamento — cada CT-e a mais faz o
    mínimo incidir de novo.
    """
    if t.tipo_calculo == "FIXO":
        bruto = D(t.fp_valor)
    elif t.tipo_calculo == "PERCENTUAL_VALOR_NF":
        bruto = D(valor_nf) * D(t.fp_valor)
    else:  # POR_KG
        peso_tarifado = arredondar(vn1, t.fp_arred)
        bruto = peso_tarifado * D(t.fp_valor)

    minimo = D(t.fp_minimo)
    if minimo > bruto:
        return minimo, True
    return bruto, False


def generalidades(vn1: Decimal, carga: Carga, itens, arredondar_linha: bool
                  ) -> tuple[Decimal, list[Componente]]:
    """VN3 — uma linha por componente, somadas.

    Devolver a lista aberta é o que permite a auditoria comparar contra
    tb_cte_tarifas componente a componente, em vez de só o total.
    """
    total = Decimal(0)
    saida: list[Componente] = []

    for g in itens:
        if g.base is BaseIncidencia.PESO:
            base = vn1
        elif g.base is BaseIncidencia.VALOR_NF:
            base = D(carga.valor_nf)
        elif g.base is BaseIncidencia.VALOR_MERCADORIA:
            base = D(carga.valor_mercadoria if carga.valor_mercadoria is not None
                     else carga.valor_nf)
        elif g.base is BaseIncidencia.VOLUME:
            base = D(carga.quantidade_volumes)
        elif g.base is BaseIncidencia.FIXO:
            base = Decimal(1)
        else:  # FRETE_PESO é resolvido fora — depende do VN2
            continue

        # o `arred` da tarifa se aplica à BASE, antes de multiplicar
        base_arred = arredondar(base, g.arredondamento)
        calc = base_arred * D(g.valor)

        pegou_min = pegou_max = False
        if g.minimo is not None and D(g.minimo) > calc:
            calc, pegou_min = D(g.minimo), True
        if g.maximo is not None and D(g.maximo) < calc:
            calc, pegou_max = D(g.maximo), True

        if arredondar_linha:
            calc = arredondar(calc)

        total += calc
        saida.append(Componente(
            codigo=g.codigo, nome=g.nome, base_incidencia=g.base,
            base_calculo=arredondar(base_arred, 4), valor=arredondar(calc),
            aplicou_minimo=pegou_min, aplicou_maximo=pegou_max))

    return total, saida


def previsao_entrega(emissao: date, dias_coleta: int, prazo: Optional[int]) -> Optional[date]:
    """emissao + dias_coleta + prazo, em dias corridos.

    PENDENTE: o calendário de atendimento (atende_seg…atende_dom) veio 0%
    preenchido no dado da parceira. Quando vier, prazo em dias ÚTEIS precisa
    pular os dias não atendidos.
    """
    if prazo is None:
        return None
    return emissao + timedelta(days=int(dias_coleta or 0) + int(prazo))


# ════════════════════════════════════════════════════════ o cálculo completo
def calcular_niveis(carga: Carga, t: Tarifa,
                    politica: Politica = Politica.POR_NIVEL,
                    modo_peso: ModoPeso = ModoPeso.MEIO_PARA_CIMA) -> Niveis:
    """Roda VN1 a VN6 e devolve tudo, com a memória do caminho."""
    por_nivel = politica is Politica.POR_NIVEL

    def talvez(v: Decimal) -> Decimal:
        return arredondar(v) if por_nivel else D(v)

    vn1 = peso_taxado(carga.peso_bruto, carga.volume_m3, t.fator_cubagem, modo_peso)

    vn2_bruto, pegou_min_fp = frete_peso(vn1, t, carga.valor_nf)
    vn2 = talvez(vn2_bruto)

    vn3_bruto, componentes = generalidades(vn1, carga, t.generalidades, por_nivel)
    vn3 = talvez(vn3_bruto)

    vn4 = talvez((vn2 + vn3) * D(t.sobre_frete))
    liquido = talvez(vn2 + vn3 + vn4)

    # POR_NIVEL calcula o ICMS sobre o líquido já arredondado, como a
    # implementação da Lais; SO_NO_FIM usa a soma crua, como a do cotacao_router.
    base_icms = liquido if por_nivel else (vn2 + vn3 + vn4)
    vn5 = talvez(base_icms * D(t.icms))

    soma = liquido + vn5 if por_nivel else (vn2 + vn3 + vn4 + vn5)
    minimo_geral = D(t.minimo_geral)
    pegou_min_geral = minimo_geral > soma
    vn6 = talvez(minimo_geral if pegou_min_geral else soma)

    return Niveis(
        peso_taxado=arredondar(vn1),
        frete_peso=arredondar(vn2),
        generalidades=arredondar(vn3),
        sobre_frete=arredondar(vn4),
        icms=arredondar(vn5),
        frete_liquido=arredondar(liquido),
        frete_total=arredondar(vn6),
        aplicou_minimo_geral=pegou_min_geral,
        aplicou_minimo_frete_peso=pegou_min_fp,
        componentes=componentes,
        memoria={
            "politica_arredondamento": politica.value,
            "modo_peso_taxado": modo_peso.value,
            "VN1_peso_taxado": str(arredondar(vn1)),
            "VN1_detalhe": (f"max(peso_bruto {carga.peso_bruto}, "
                            f"m3 {carga.volume_m3} × cubagem {t.fator_cubagem})"),
            "VN2_frete_peso": str(arredondar(vn2)),
            "VN2_detalhe": (f"{t.tipo_calculo}: round(VN1, {t.fp_arred}) × {t.fp_valor}"
                            + (f" — mínimo {t.fp_minimo} aplicado" if pegou_min_fp else "")),
            "VN3_generalidades": str(arredondar(vn3)),
            "VN3_detalhe": f"{len(componentes)} componente(s)",
            "VN4_sobre_frete": f"(VN2+VN3) × {t.sobre_frete} = {arredondar(vn4)}",
            "VN5_icms": f"{'liquido' if por_nivel else 'VN2+VN3+VN4'} × {t.icms} = {arredondar(vn5)}",
            "VN6_total": str(arredondar(vn6)),
            "VN6_detalhe": (f"mínimo geral {t.minimo_geral} aplicado"
                            if pegou_min_geral else "soma dos níveis"),
        })


# ════════════════════════════════════════════════════════ adaptador do JSON antigo
def tarifa_do_json(regras: dict, fator_cubagem=None) -> Tarifa:
    """Traduz o `regras_calculo` do modelo antigo para a Tarifa normalizada.

    Só serve ao teste de regressão: em produção a Tarifa vem das tabelas
    relacionais. Mantido aqui, e não nos testes, porque a tradução é
    conhecimento do domínio — não detalhe de teste.
    """
    fp = regras.get("frete_peso") or {}
    itens = []
    for i, g in enumerate(regras.get("generalidades") or []):
        base_txt = (g.get("base") or "").upper()
        if base_txt == "VNF":
            base, codigo, nome = BaseIncidencia.VALOR_NF, "FRETE_VALOR", "Frete-valor (VNF)"
        elif base_txt == "PPC":
            base, codigo, nome = BaseIncidencia.PESO, "GENERALIDADE_PPC", "Generalidade por peso (PPC)"
        else:
            continue
        itens.append(ItemGeneralidade(
            codigo=f"{codigo}_{i}" if len(regras.get("generalidades") or []) > 2 else codigo,
            nome=nome, base=base, valor=D(g.get("valor")),
            minimo=D(g["min"]) if g.get("min") is not None else None,
            maximo=D(g["max"]) if g.get("max") else None,
            arredondamento=int(g.get("arred", 2) or 0)))

    return Tarifa(
        fp_minimo=D(fp.get("minimo")), fp_valor=D(fp.get("valor")),
        fp_arred=int(fp.get("arred", 0) or 0),
        generalidades=tuple(itens),
        sobre_frete=D(regras.get("sobre_frete")),
        icms=D(regras.get("icms")),
        minimo_geral=D(regras.get("minimo_geral")),
        fator_cubagem=D(fator_cubagem if fator_cubagem is not None else 300))


def calcular_de_tarifa_bruta(peso_bruto, volume_m3, valor_nf, fator_cubagem, tarifas,
                             politica: Politica = Politica.SO_NO_FIM) -> dict:
    """Entrada do teste de regressão: recebe o JSON antigo, devolve os níveis.

    O padrão aqui é SO_NO_FIM de propósito — é o modo que reproduz o motor
    atual, que é justamente o que o teste de regressão precisa provar.
    """
    t = tarifa_do_json(tarifas, fator_cubagem)
    carga = Carga(peso_bruto=D(peso_bruto), volume_m3=D(volume_m3), valor_nf=D(valor_nf))
    n = calcular_niveis(carga, t, politica=politica)
    return {
        "peso_taxado": n.peso_taxado, "frete_peso": n.frete_peso,
        "generalidades": n.generalidades, "sobre_frete": n.sobre_frete,
        "icms": n.icms, "frete_liquido": n.frete_liquido, "frete_total": n.frete_total,
        "aplicou_minimo_geral": n.aplicou_minimo_geral,
        "componentes": n.componentes, "memoria": n.memoria,
    }


# ════════════════════════════════════════════════════════ a rota de produção
#
#  Daqui para baixo é a costura entre as duas metades do motor: a resolução
#  (quais números entram) e o cálculo (a aritmética). Os três pedaços que se
#  juntam aqui pela primeira vez são o interpretador de fórmulas, as faixas
#  reais da parceira e os avisos de ambiguidade.


def tarifa_do_contexto(ctx) -> "Tarifa":
    """Traduz um `resolucao.Contexto` para a `Tarifa` que o VN2 consome.

    Três coisas valem explicação, porque não são óbvias:

    **O VN2 já vem resolvido.** A resolução achou a faixa e calculou o preço
    (incluindo o excedente, quando o peso passou do teto). Então a tarifa entra
    como `FIXO` com esse valor, em vez de `POR_KG` com R$/kg: multiplicar de
    novo pelo peso cobraria duas vezes.

    **`Frete_Minimo` virou o piso do frete-peso**, não o mínimo geral. É
    suposição — está em `pendencias.MODO_FRETE_MINIMO`.

    **`sobre_frete` é zero.** O VN4 existe no contrato porque o JSON antigo o
    tinha e o agente da Lais o consome. Mas nas 6.430 faixas da parceira não há
    nenhuma espécie que corresponda a sobre-frete: as seis são Faixa,
    Excedente, Imposto, AdValore, Frete_Minimo e ExcedenteMinimo. Zero aqui é
    ausência de dado, não decisão de cálculo — e é melhor que inventar um
    percentual.
    """
    return Tarifa(
        fp_minimo=D(ctx.frete_minimo) if ctx.frete_minimo is not None else Decimal(0),
        fp_valor=D(ctx.preco_frete_peso),
        fp_arred=0,
        generalidades=(),          # o VN3 vem das fórmulas, não daqui
        sobre_frete=Decimal(0),
        icms=D(ctx.icms),
        minimo_geral=Decimal(0),
        fator_cubagem=D(ctx.fator_cubagem),
        tipo_calculo="FIXO",
    )


def generalidades_do_perfil(ctx, carga: Carga, vn1: Decimal, por_nivel: bool,
                            pallets: Decimal | None = None
                            ) -> tuple[Decimal, list[Componente], object]:
    """VN3 pela via nova: avalia as fórmulas do perfil.

    Substitui a soma de `ItemGeneralidade` quando existe perfil. A diferença
    não é de formato: `se(contem([ufdest],'DF, RJ'), ...)` não cabe em
    (valor, mínimo, máximo), e era isso que o modelo antigo tentava fazer.

    O `AdValore` das faixas entra aqui também, e **nunca junto** com a fórmula
    `adv` do perfil: nos 638 frete-peso que têm a linha, o perfil não tem a
    fórmula, e nos 341 em que o perfil tem, a linha não existe. Somar os dois
    cobraria ad valorem em dobro.
    """
    from . import generalidades as ger

    total = Decimal(0)
    saida: list[Componente] = []
    r = None

    if ctx.perfil is not None:
        ctxf = ger.contexto_do_embarque(
            valor_nf=D(carga.valor_nf),
            peso_para_calculo=vn1,
            tarcli=ctx.tarcli,
            tarcid=ctx.tar_cid,
            uf_destino=ctx.uf_destino,
            pallets=(pallets if pallets is not None
                     else Decimal(carga.quantidade_volumes)),
            #  `[cnpj_terceiro_destino]` aparece em 2 expressões, só num
            #  `<>null` ("entrega a terceiro?"). Não existe campo para isto no
            #  Embarque, e inventar um seria inventar cobrança: fica nulo, e a
            #  condição dá falso.
            cnpj_terceiro_destino=None,
        )
        r = ger.avaliar_perfil(ctx.perfil, ctxf)

        for linha in r.memoria:
            if not linha.entra_no_total or linha.coluna == ger.COLUNA_TOTAL:
                continue
            valor = arredondar(linha.valor) if por_nivel else linha.valor
            saida.append(Componente(
                codigo=linha.coluna.upper(), nome=linha.coluna,
                base_incidencia=BaseIncidencia.FIXO,
                base_calculo=Decimal(0),
                valor=arredondar(valor)))
        total = r.total_cotado

    if ctx.ad_valorem is not None:
        base = D(carga.valor_nf)
        valor = base * D(ctx.ad_valorem)
        if por_nivel:
            valor = arredondar(valor)
        total += valor
        saida.append(Componente(
            codigo="ADVALORE", nome="Ad valorem (linha da tarifa)",
            base_incidencia=BaseIncidencia.VALOR_NF,
            base_calculo=arredondar(base, 4), valor=arredondar(valor)))

    return total, saida, r


def calcular(embarque, ctx, quando: date, incluir_memoria: bool = True):
    """Monta a `Cotacao` de UMA transportadora, a partir do contexto resolvido.

    Devolve `esquemas.Cotacao`. O que ela traz de diferente do motor atual:
    componentes item a item, o rastreio das linhas do banco que produziram o
    número, e os avisos do que o motor teve que decidir no lugar do dado.
    """
    from .esquemas import Aviso, Cotacao, Rastreio
    from .resolucao import previsao_de_entrega

    carga = Carga(
        peso_bruto=D(embarque.peso_bruto_kg),
        volume_m3=D(embarque.volume_m3),
        valor_nf=D(embarque.valor_nf),
        valor_mercadoria=(D(embarque.valor_mercadoria)
                          if embarque.valor_mercadoria is not None else None),
        quantidade_volumes=int(embarque.quantidade_volumes or 1),
    )
    t = tarifa_do_contexto(ctx)

    #  POR_NIVEL é o padrão de produção: arredonda a cada nível, que é o que
    #  casa com o CT-e. SO_NO_FIM existe para reproduzir o motor antigo no
    #  teste de regressão, e não deve ser o padrão aqui.
    por_nivel = True

    vn1 = D(ctx.peso_taxado) or peso_taxado(
        carga.peso_bruto, carga.volume_m3, t.fator_cubagem)

    vn2_bruto, pegou_min_fp = frete_peso(vn1, t, carga.valor_nf)
    vn2 = arredondar(vn2_bruto)

    #  `[pallets]` é campo próprio do embarque. Na falta dele, o motor usa a
    #  quantidade de volumes — que é um chute, porque 40 caixas podem ir em 2
    #  pallets — e avisa, mas só quando o perfil de fato cobra por pallet.
    pallets = (Decimal(embarque.quantidade_pallets)
               if embarque.quantidade_pallets is not None else None)
    if pallets is None and ctx.perfil is not None:
        usa_pallets = any("pallets" in f.variaveis
                          for f in ctx.perfil.expressoes.values())
        if usa_pallets:
            ctx.avisar(Aviso.PALLETS_ESTIMADOS)

    vn3_bruto, componentes, avaliacao = generalidades_do_perfil(
        ctx, carga, vn1, por_nivel, pallets)
    vn3 = arredondar(vn3_bruto)

    vn4 = arredondar((vn2 + vn3) * D(t.sobre_frete))
    liquido = arredondar(vn2 + vn3 + vn4)
    vn5 = arredondar(liquido * D(t.icms))

    soma = arredondar(liquido + vn5)
    minimo_geral = D(t.minimo_geral)
    pegou_min_geral = minimo_geral > soma
    vn6 = arredondar(minimo_geral if pegou_min_geral else soma)

    #  O frete-peso também é componente: é o que torna a conferência da
    #  auditoria um JOIN contra tb_cte_tarifas, em vez de comparar totais.
    componentes.insert(0, Componente(
        codigo="FRETE_PESO", nome="Frete-peso",
        base_incidencia=BaseIncidencia.PESO,
        base_calculo=arredondar(vn1), valor=vn2,
        aplicou_minimo=pegou_min_fp))
    if vn5:
        componentes.append(Componente(
            codigo="ICMS", nome="ICMS",
            base_incidencia=BaseIncidencia.FRETE_PESO,
            base_calculo=liquido, valor=vn5))

    memoria = None
    if incluir_memoria:
        memoria = {
            "VN1_peso_taxado": str(vn1),
            "VN2_frete_peso": str(vn2),
            "VN3_generalidades": str(vn3),
            "VN4_sobre_frete": str(vn4),
            "VN5_icms": str(vn5),
            "VN6_total": str(vn6),
            "politica_arredondamento": Politica.POR_NIVEL.value,
            "fator_cubagem": str(t.fator_cubagem),
            "aliquota_icms": str(t.icms),
            "icms_de": ctx.icms_de,
            "aplicou_excedente": ctx.aplicou_excedente,
            "aplicou_minimo_frete_peso": pegou_min_fp,
            "tarcli": ctx.tarcli,
            "tarcid": ctx.tar_cid,
            #  O "como cheguei nesse numero" das generalidades: a formula
            #  escrita ao lado do valor que ela produziu. E o que permite
            #  defender um valor contestado sem abrir o banco.
            "formulas": ([{"coluna": m.coluna, "expressao": m.expressao,
                           "valor": str(m.valor), "eventual": m.eventual}
                          for m in avaliacao.memoria]
                         if avaliacao is not None else None),
            "eventuais": ({k: str(v) for k, v in avaliacao.eventuais.items()}
                          if avaliacao is not None else None),
        }

    return Cotacao(
        transportadora_id=ctx.transportadora_id,
        transportadora=ctx.transportadora,
        peso_taxado_kg=vn1,
        frete_peso=vn2,
        generalidades=vn3,
        sobre_frete=vn4,
        icms=vn5,
        frete_liquido=liquido,
        frete_total=vn6,
        aplicou_minimo_geral=pegou_min_geral,
        prazo_dias=ctx.prazo_dias,
        previsao_entrega=previsao_de_entrega(quando, ctx.prazo_dias, ctx.calendario),
        componentes=componentes,
        avisos=list(ctx.avisos),
        rastreio=Rastreio(
            tabela_frete_id=ctx.tabela_frete_id,
            nome_tabela=ctx.nome_tabela,
            tabela_frete_peso_id=ctx.tabela_frete_peso_id,
            faixa_valor_id=ctx.faixa_valor_id,
            aplicou_excedente=ctx.aplicou_excedente,
            icms_de=ctx.icms_de,
            regiao_atendida_id=ctx.regiao_atendida_id,
            praca_id=ctx.praca_id,
            praca=ctx.praca,
            modalidade_id=ctx.modalidade_id,
            modalidade=ctx.modalidade,
            perfil_generalidade_id=ctx.perfil_generalidade_id,
            vigencia_inicio=ctx.vigencia_inicio,
            vigencia_fim=ctx.vigencia_fim,
        ),
        memoria_calculo=memoria,
    )
