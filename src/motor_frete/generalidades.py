# -*- coding: utf-8 -*-
"""Avaliação de um perfil de generalidades — o VN3 do cálculo.

Um perfil é uma linha de `tb_generalidades`: 19 colunas de componente (ctrc,
adv, gris, pedágio, coleta, entrega, pallet, agendar, tde, trt, tas, suframa,
segfluvial, icm_iss, generalidade1, generalidade2, reentrega, devolução,
fluvial), cada uma com uma expressão, mais a coluna `tst` — que é a **soma**, e
que também é expressão.

## A descoberta que dispensou o palpite

`tst` não é "somar tudo". É uma expressão por perfil que **nomeia os seus
próprios componentes**:

    perfil 201  tst = [ctrc]+[adv]+[gris]+…+[icm_iss]+[outros1]+[outros2]
    perfil 314  tst = [ctrc]+[adv]+[gris]+…+[icm_iss]+[outros1]
                      (sem [outros2] — porque o generalidade2 dele lê [tst])

Conferido nos 238 perfis, nas 19 colunas: **toda vez que a fórmula de uma
coluna lê `[tst]`, o `tst` daquele perfil deixa essa coluna de fora.** Zero
ciclos. Não é coincidência de uma planilha bem cuidada — é consistente demais
para isso, o que indica que o `tst` foi gerado do mesmo lugar que as colunas.

A consequência prática é grande: **a ordem de avaliação não é suposta, é lida.**
Cada perfil traz o seu grafo de dependências, e a ordem sai de uma ordenação
topológica. Se um dado novo trouxer ciclo de verdade, isto aqui levanta
`DependenciaCircular` em vez de inventar um número.

## O que entra no frete cotado

    total_cotado = [tst]  +  as colunas COBRADAS que ficam fora do tst daquele perfil

O segundo termo não é sobra: `ctrc = [tst]*0.0174` e `icm_iss = [tst]*0.04`
ficam fora do tst exatamente porque incidem sobre ele — são cobrança por cima,
não substituição. Ignorá-las barateia a cotação em silêncio.

Ficam fora da cotação só as três que **nunca** aparecem em nenhum `tst` dos
238 perfis e que, por definição, dependem de um evento que pode não acontecer:
reentrega, devolução e fluvial. Estão em `pendencias.COMPONENTES_EVENTUAIS`,
são calculadas e devolvidas à parte, para a tela poder mostrar "se houver
reentrega, +R$ X".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping

from . import pendencias
from .formulas import ErroDeFormula, Formula, compilar

__all__ = [
    "COLUNAS_COMPONENTE", "COLUNA_TOTAL", "VARIAVEIS_DO_EMBARQUE",
    "DependenciaCircular", "Perfil", "Resultado", "LinhaMemoria",
    "contexto_do_embarque", "avaliar_perfil",
]

_ZERO = Decimal("0")

#  Ordem da planilha. Não é a ordem de avaliação — é só para relatório sair
#  estável e para o `origem_dado` casar coluna por coluna.
COLUNAS_COMPONENTE: tuple[str, ...] = (
    "ctrc", "adv", "gris", "pedagio", "coleta", "entrega", "pallet", "agendar",
    "tde", "trt", "tas", "suframa", "segfluvial", "icm_iss",
    "generalidade1", "generalidade2", "reentrega", "devolucao", "fluvial",
)

COLUNA_TOTAL = "tst"

#  `cubagem` também é expressão na planilha, mas não é generalidade: é o fator
#  de cubagem (o 300 de "300 kg por m³"), e quem usa é o VN1 em calculo.py.
COLUNA_CUBAGEM = "cubagem"

#  O que o embarque precisa trazer. Nomes exatos como as fórmulas os citam.
VARIAVEIS_DO_EMBARQUE: tuple[str, ...] = (
    "fv",                       # valor da nota — pendencias.VARIAVEL_FV
    "peso_para_calculo",        # o peso taxado do VN1
    "tarcli",                   # código de serviços contratados pelo cliente
    "tarcid",                   # código da cidade/região — pendencias.VARIAVEL_TARCID
    "ufdest",                   # UF de destino
    "pallets",                  # quantidade de pallets
    "cnpj_terceiro_destino",    # usado só em `<>null`: entrega a terceiro?
)


class DependenciaCircular(ErroDeFormula):
    """Duas colunas do mesmo perfil dependem uma da outra.

    Não acontece nos 238 perfis de hoje. Se acontecer num dado novo, é defeito
    de cadastro dela — e tem que aparecer, não ser resolvido por chute.
    """


# ═══════════════════════════════════════════════════════════════════ perfil
@dataclass(frozen=True)
class Perfil:
    """As expressões de uma linha de `tb_generalidades`, já compiladas."""

    id: str
    expressoes: Mapping[str, Formula]          # coluna → fórmula (só as preenchidas)
    fator_cubagem: Decimal | None = None

    @classmethod
    def de_linha(cls, id: Any, linha: Mapping[str, Any]) -> "Perfil":
        """Constrói a partir da linha crua do banco (ou da planilha).

        Coluna vazia é ausência, não zero: `expressoes` só traz o que tem
        fórmula. O zero aparece na avaliação, para o componente entrar na
        memória de cálculo mesmo valendo nada.
        """
        exprs: dict[str, Formula] = {}
        for col in (*COLUNAS_COMPONENTE, COLUNA_TOTAL):
            bruto = linha.get(col)
            if bruto is None or (isinstance(bruto, str) and not bruto.strip()):
                continue
            exprs[col] = compilar(str(bruto).strip())

        cub = linha.get(COLUNA_CUBAGEM)
        fator = None
        if cub is not None and str(cub).strip():
            fator = compilar(str(cub).strip()).numero({})

        return cls(id=str(id), expressoes=exprs, fator_cubagem=fator)

    # ── grafo
    def dependencias(self, coluna: str) -> frozenset[str]:
        """De que outras colunas do perfil esta coluna depende.

        Aplica os apelidos de `pendencias.ALIAS_VARIAVEIS`: `[outros1]` é a
        coluna `generalidade1`.
        """
        f = self.expressoes.get(coluna)
        if f is None:
            return frozenset()
        conhecidas = set(self.expressoes) | {COLUNA_TOTAL}
        saida = set()
        for v in f.variaveis:
            nome = pendencias.ALIAS_VARIAVEIS.get(v, v)
            if nome in conhecidas or nome in COLUNAS_COMPONENTE:
                saida.add(nome)
        return frozenset(saida)

    def componentes_do_total(self) -> frozenset[str]:
        """As colunas que o `tst` DESTE perfil soma — resolvendo os apelidos."""
        return self.dependencias(COLUNA_TOTAL)

    def ordem_de_avaliacao(self) -> tuple[str, ...]:
        """Ordenação topológica: cada coluna depois das que ela lê.

        Desempate pela ordem da planilha, para a memória de cálculo sair igual
        em duas execuções — e para o diff contra o CT-e ser legível.
        """
        alvos = [c for c in COLUNAS_COMPONENTE if c in self.expressoes]
        if COLUNA_TOTAL in self.expressoes:
            alvos.append(COLUNA_TOTAL)

        pendente = {c: set(self.dependencias(c)) & set(alvos) for c in alvos}
        prontos: list[str] = []
        vistos: set[str] = set()

        while pendente:
            livres = [c for c in alvos
                      if c in pendente and not (pendente[c] - vistos)]
            if not livres:
                travadas = sorted(pendente)
                raise DependenciaCircular(
                    f"perfil {self.id}: dependência circular entre "
                    f"{', '.join(travadas)} — nenhuma delas pode ser avaliada "
                    f"primeiro")
            for c in livres:
                prontos.append(c)
                vistos.add(c)
                del pendente[c]

        return tuple(prontos)


# ═══════════════════════════════════════════════════════════════════ saída
@dataclass(frozen=True)
class LinhaMemoria:
    """Uma linha do "como cheguei nesse número"."""

    coluna: str
    expressao: str
    valor: Decimal
    entra_no_total: bool
    eventual: bool


@dataclass
class Resultado:
    perfil: str
    valores: dict[str, Decimal] = field(default_factory=dict)
    tst: Decimal = _ZERO
    #  O que soma no frete: o tst mais as cobranças que ficam fora dele.
    total_cotado: Decimal = _ZERO
    #  Reentrega, devolução, fluvial: calculadas, não somadas.
    eventuais: dict[str, Decimal] = field(default_factory=dict)
    ordem: tuple[str, ...] = ()
    memoria: list[LinhaMemoria] = field(default_factory=list)

    def componentes(self) -> list[LinhaMemoria]:
        """Só o que entrou no total — é isto que casa com `tb_cte_tarifas`."""
        return [m for m in self.memoria if m.entra_no_total]


# ═══════════════════════════════════════════════════════════════ contexto
def contexto_do_embarque(
    *,
    valor_nf: Decimal | None = None,
    peso_para_calculo: Decimal | None = None,
    tarcli: str | None = None,
    tarcid: str | None = None,
    uf_destino: str | None = None,
    pallets: int | Decimal | None = None,
    cnpj_terceiro_destino: str | None = None,
) -> dict[str, Any]:
    """Traduz o embarque para os nomes que as fórmulas usam.

    Toda variável do embarque entra SEMPRE, mesmo nula — assim
    `VariavelIndefinida` fica reservada para o que realmente é erro nosso:
    ordem de avaliação furada.
    """
    return {
        "fv": valor_nf,
        "peso_para_calculo": peso_para_calculo,
        "tarcli": tarcli,
        "tarcid": tarcid,
        "ufdest": uf_destino,
        "pallets": pallets,
        "cnpj_terceiro_destino": cnpj_terceiro_destino,
    }


# ═══════════════════════════════════════════════════════════════ avaliação
def avaliar_perfil(
    perfil: Perfil,
    contexto: Mapping[str, Any],
    *,
    eventuais: Iterable[str] | None = None,
) -> Resultado:
    """Avalia o perfil inteiro na ordem que as dependências ditam.

    `contexto` são as variáveis do embarque (use `contexto_do_embarque`).
    `eventuais` sobrescreve `pendencias.COMPONENTES_EVENTUAIS` — é o gancho
    para, quando a resposta dela chegar, mudar a lista num lugar só.
    """
    fora = frozenset(pendencias.COMPONENTES_EVENTUAIS if eventuais is None
                     else eventuais)

    ctx: dict[str, Any] = dict(contexto)
    for v in VARIAVEIS_DO_EMBARQUE:
        ctx.setdefault(v, None)

    #  Coluna SEM fórmula entra como nula desde o começo: o `tst` soma
    #  `[coleta]` mesmo quando aquele perfil não cobra coleta, e nulo vale 0.
    #  Coluna COM fórmula fica de fora do contexto até ser avaliada — é isso que
    #  faz `VariavelIndefinida` denunciar ordem de avaliação errada em vez de
    #  cobrar zero em silêncio.
    for col in (*COLUNAS_COMPONENTE, COLUNA_TOTAL):
        if col not in perfil.expressoes:
            ctx.setdefault(col, None)
    for apelido, destino in pendencias.ALIAS_VARIAVEIS.items():
        if destino not in perfil.expressoes:
            ctx.setdefault(apelido, None)

    ordem = perfil.ordem_de_avaliacao()
    dentro_do_tst = perfil.componentes_do_total()
    res = Resultado(perfil=perfil.id, ordem=ordem)

    for coluna in ordem:
        f = perfil.expressoes[coluna]
        valor = f.numero(ctx)

        ctx[coluna] = valor
        #  O apelido tem que existir no contexto pelos dois nomes: o `tst` cita
        #  [outros1], as demais colunas citam [generalidade1].
        for apelido, destino in pendencias.ALIAS_VARIAVEIS.items():
            if destino == coluna:
                ctx[apelido] = valor

        if coluna == COLUNA_TOTAL:
            res.tst = valor
            continue

        res.valores[coluna] = valor
        eh_eventual = coluna in fora
        #  Entra no total se o tst já a soma, ou se é cobrança que ficou fora
        #  do tst (ctrc = [tst]*0.0174, icm_iss = [tst]*0.04) — nunca se é
        #  eventual.
        entra = not eh_eventual
        if eh_eventual:
            res.eventuais[coluna] = valor

        res.memoria.append(LinhaMemoria(
            coluna=coluna, expressao=f.fonte, valor=valor,
            entra_no_total=entra, eventual=eh_eventual))

    #  O total: o tst (que já embute as colunas que ele nomeia) mais as
    #  cobranças de fora dele.
    por_cima = sum(
        (v for c, v in res.valores.items()
         if c not in dentro_do_tst and c not in fora),
        _ZERO,
    )
    res.total_cotado = res.tst + por_cima

    res.memoria.append(LinhaMemoria(
        coluna=COLUNA_TOTAL,
        expressao=perfil.expressoes[COLUNA_TOTAL].fonte
        if COLUNA_TOTAL in perfil.expressoes else "(perfil sem tst)",
        valor=res.tst, entra_no_total=True, eventual=False))

    return res
