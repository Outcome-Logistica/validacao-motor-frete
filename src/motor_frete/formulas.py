# -*- coding: utf-8 -*-
"""Interpretador da linguagem de fórmulas da parceira.

As 21 colunas de valor de `tb_generalidades` não guardam número: guardam
expressão. `gris` vale `maximo(5,[fv]*0.002)`, `pedagio` vale
`se(contem([ufdest],'DF, RJ, SP'),arredondar_para_cima0([peso_para_calculo]/100)*7,0)`.
São 2.770 expressões em 238 perfis, e sem avaliá-las não há como cotar.

A gramática foi levantada do corpus inteiro, não adivinhada — está em
`outcome_api/docs/GRAMATICA_GENERALIDADES.html` e no fixture
`tests/fixtures/formulas_generalidades.json`. Seis funções, dois comparadores,
quatro operadores. Nada mais aparece nas 679 expressões distintas.

Duas decisões de projeto que valem ler antes de mexer:

**Erro nunca é zero.** Função desconhecida, variável não fornecida, sintaxe
inválida — tudo levanta. Um interpretador que devolve 0 no que não entende
produz cotação barata e silenciosa, que é o pior defeito possível aqui: ninguém
descobre pelo número, só pela fatura.

**Dinheiro é Decimal.** Nenhum `float` entra ou sai. `/` usa a precisão do
contexto decimal (28 dígitos), e o arredondamento para centavo é do chamador —
`calculo.py` decide onde arredonda, não aqui.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "ErroDeFormula", "ErroDeSintaxe", "VariavelIndefinida", "FuncaoDesconhecida",
    "Formula", "compilar", "avaliar", "avaliar_numero", "variaveis_de",
    "FUNCOES",
]


# ═══════════════════════════════════════════════════════════════════ erros
class ErroDeFormula(ValueError):
    """Qualquer falha ao compilar ou avaliar uma expressão."""


class ErroDeSintaxe(ErroDeFormula):
    """A expressão não é da linguagem."""


class VariavelIndefinida(ErroDeFormula):
    """A expressão cita `[nome]` e o contexto não trouxe esse nome.

    Não é o mesmo que a variável valer nulo: nulo é valor, e vale 0 na soma.
    Isto aqui é a variável não existir — quase sempre ordem de avaliação errada
    (uma generalidade lendo `[tst]` antes de o `[tst]` ter sido calculado).
    """


class FuncaoDesconhecida(ErroDeFormula):
    """A expressão chama função fora das seis da linguagem."""


# ═══════════════════════════════════════════════════════════════ tokenizador
#  Ordem importa: `<>` antes de `<`, TEXTO antes de qualquer coisa (aspas podem
#  conter `>` e `,` — ex.: 'PFP>000001'), VAR antes de IDENT.
_TOKENS = re.compile(
    r"""
      (?P<ESPACO>   \s+                            )
    | (?P<TEXTO>    '(?:[^']|'')*'  |  "(?:[^"]|"")*"  )
    | (?P<NUMERO>   \d+(?:\.\d+)?   |  \.\d+        )
    | (?P<VAR>      \[ [^\]]* \]                    )
    | (?P<NE>       <>                              )
    | (?P<GT>       >                               )
    | (?P<LT>       <                               )
    | (?P<IDENT>    [A-Za-z_][A-Za-z_0-9]*          )
    | (?P<ABRE>     \(                              )
    | (?P<FECHA>    \)                              )
    | (?P<VIRGULA>  ,                               )
    | (?P<MAIS>     \+                              )
    | (?P<MENOS>    -                               )
    | (?P<VEZES>    \*                              )
    | (?P<DIVIDE>   /                               )
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Token:
    tipo: str
    texto: str
    pos: int


def _tokenizar(fonte: str) -> list[_Token]:
    saida: list[_Token] = []
    i = 0
    while i < len(fonte):
        m = _TOKENS.match(fonte, i)
        if m is None:
            raise ErroDeSintaxe(
                f"caractere inesperado {fonte[i]!r} na posição {i} de {fonte!r}")
        i = m.end()
        if m.lastgroup == "ESPACO":
            continue
        saida.append(_Token(m.lastgroup, m.group(), m.start()))
    return saida


# ═══════════════════════════════════════════════════════════════════ nós
#  AST como tuplas pequenas: ("num", Decimal) / ("txt", str) / ("nulo",)
#  ("var", nome) / ("op", simbolo, esq, dir) / ("neg", no) / ("cham", nome, args)


def _desescapa(literal: str) -> str:
    aspa = literal[0]
    return literal[1:-1].replace(aspa * 2, aspa)


class _Parser:
    """Descida recursiva. Precedência, do mais fraco ao mais forte:

        comparacao  >  <>  <
        soma        +  -
        produto     *  /
        unario      -
        primario    numero, texto, null, [var], f(...), ( ... )
    """

    def __init__(self, fonte: str):
        self.fonte = fonte
        self.tokens = _tokenizar(fonte)
        self.i = 0

    # ── mecânica
    def _olha(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _come(self, tipo: str) -> _Token:
        t = self._olha()
        if t is None or t.tipo != tipo:
            visto = "fim da expressão" if t is None else f"{t.texto!r}"
            raise ErroDeSintaxe(f"esperava {tipo}, veio {visto} em {self.fonte!r}")
        self.i += 1
        return t

    def _aceita(self, *tipos: str) -> _Token | None:
        t = self._olha()
        if t is not None and t.tipo in tipos:
            self.i += 1
            return t
        return None

    # ── gramática
    def analisar(self):
        if not self.tokens:
            raise ErroDeSintaxe("expressão vazia")
        no = self._comparacao()
        sobra = self._olha()
        if sobra is not None:
            raise ErroDeSintaxe(
                f"sobrou {sobra.texto!r} na posição {sobra.pos} de {self.fonte!r}")
        return no

    def _comparacao(self):
        esq = self._soma()
        while (t := self._aceita("GT", "LT", "NE")) is not None:
            esq = ("op", t.texto, esq, self._soma())
        return esq

    def _soma(self):
        esq = self._produto()
        while (t := self._aceita("MAIS", "MENOS")) is not None:
            esq = ("op", t.texto, esq, self._produto())
        return esq

    def _produto(self):
        esq = self._unario()
        while (t := self._aceita("VEZES", "DIVIDE")) is not None:
            esq = ("op", t.texto, esq, self._unario())
        return esq

    def _unario(self):
        if self._aceita("MENOS") is not None:
            return ("neg", self._unario())
        return self._primario()

    def _primario(self):
        t = self._olha()
        if t is None:
            raise ErroDeSintaxe(f"expressão termina incompleta: {self.fonte!r}")

        if t.tipo == "NUMERO":
            self.i += 1
            return ("num", Decimal(t.texto))

        if t.tipo == "TEXTO":
            self.i += 1
            return ("txt", _desescapa(t.texto))

        if t.tipo == "VAR":
            self.i += 1
            nome = t.texto[1:-1].strip().lower()
            if not nome:
                raise ErroDeSintaxe(f"variável sem nome em {self.fonte!r}")
            return ("var", nome)

        if t.tipo == "ABRE":
            self.i += 1
            dentro = self._comparacao()
            self._come("FECHA")
            return dentro

        if t.tipo == "IDENT":
            self.i += 1
            nome = t.texto.lower()
            if nome == "null":
                return ("nulo",)
            if self._olha() is None or self._olha().tipo != "ABRE":
                # Identificador solto não existe na linguagem. Quase sempre é
                # variável escrita sem os colchetes.
                raise ErroDeSintaxe(
                    f"{t.texto!r} solto em {self.fonte!r} — faltou [colchete] "
                    f"ou parêntese de chamada?")
            self._come("ABRE")
            args = []
            if self._olha() is not None and self._olha().tipo != "FECHA":
                args.append(self._comparacao())
                while self._aceita("VIRGULA") is not None:
                    args.append(self._comparacao())
            self._come("FECHA")
            return ("cham", nome, tuple(args))

        raise ErroDeSintaxe(
            f"não esperava {t.texto!r} na posição {t.pos} de {self.fonte!r}")


# ═══════════════════════════════════════════════════════════════ conversões
_ZERO = Decimal("0")


def _numero(v: Any, onde: str) -> Decimal:
    """Para aritmética. Nulo é 0 — é o que faz `[ctrc]+[adv]+[gris]` fechar
    quando metade das colunas do perfil veio vazia."""
    if v is None:
        return _ZERO
    if isinstance(v, bool):
        return Decimal(1) if v else _ZERO
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, str):
        try:
            return Decimal(v.strip().replace(",", "."))
        except Exception:
            raise ErroDeFormula(f"{onde}: {v!r} não é número")
    if isinstance(v, float):
        raise ErroDeFormula(
            f"{onde}: float não entra no motor — converta para Decimal antes "
            f"(recebido {v!r})")
    raise ErroDeFormula(f"{onde}: não sei usar {type(v).__name__} como número")


def _texto(v: Any) -> str:
    """Para `contem`. Compara sem caixa; nulo é string vazia."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def _verdade(v: Any, onde: str) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (Decimal, int)):
        return v != 0
    raise ErroDeFormula(f"{onde}: {v!r} não é condição")


# ═══════════════════════════════════════════════════════════════════ funções
def _f_se(args: Sequence[Any]) -> Any:
    if len(args) != 3:
        raise ErroDeFormula(f"se() quer 3 argumentos, veio {len(args)}")
    return args[1] if _verdade(args[0], "se()") else args[2]


def _f_contem(args: Sequence[Any]) -> bool:
    """`contem(agulha, palheiro)` — a agulha vem primeiro.

    Confirmado nas duas formas de uso do corpus: `contem('P',[tarcli])` procura
    a letra P no código do cliente, e `contem([ufdest],'DF, RJ, SP')` procura a
    UF do destino na lista de UFs atendidas. Nos dois casos o primeiro
    argumento é o que se procura.

    Substring, sem caixa. É a leitura honesta de `'DF, RJ, SP'` — uma lista
    escrita como texto, sem separador confiável (há itens com espaço à frente e
    vírgula sobrando no fim).
    """
    if len(args) != 2:
        raise ErroDeFormula(f"contem() quer 2 argumentos, veio {len(args)}")
    agulha, palheiro = _texto(args[0]).strip(), _texto(args[1])
    if agulha == "":
        return False
    return agulha.casefold() in palheiro.casefold()


def _f_maximo(args: Sequence[Any]) -> Decimal:
    if not args:
        raise ErroDeFormula("maximo() sem argumentos")
    return max(_numero(a, "maximo()") for a in args)


def _f_minimo(args: Sequence[Any]) -> Decimal:
    if not args:
        raise ErroDeFormula("minimo() sem argumentos")
    return min(_numero(a, "minimo()") for a in args)


def _f_ou(args: Sequence[Any]) -> bool:
    if not args:
        raise ErroDeFormula("ou() sem argumentos")
    return any(_verdade(a, "ou()") for a in args)


def _f_arredondar_para_cima0(args: Sequence[Any]) -> Decimal:
    """Teto inteiro. É como o pedágio conta fração de 100 kg: 101 kg paga 2."""
    if len(args) != 1:
        raise ErroDeFormula(
            f"arredondar_para_cima0() quer 1 argumento, veio {len(args)}")
    return _numero(args[0], "arredondar_para_cima0()").quantize(
        Decimal(1), rounding=ROUND_CEILING)


FUNCOES: Mapping[str, Callable[[Sequence[Any]], Any]] = {
    "se": _f_se,
    "contem": _f_contem,
    "maximo": _f_maximo,
    "minimo": _f_minimo,
    "ou": _f_ou,
    "arredondar_para_cima0": _f_arredondar_para_cima0,
}


# ═══════════════════════════════════════════════════════════════════ avaliação
def _avaliar_no(no: tuple, ctx: Mapping[str, Any], fonte: str) -> Any:
    marca = no[0]

    if marca == "num":
        return no[1]
    if marca == "txt":
        return no[1]
    if marca == "nulo":
        return None

    if marca == "var":
        nome = no[1]
        if nome not in ctx:
            raise VariavelIndefinida(
                f"[{nome}] não veio no contexto de {fonte!r} — "
                f"disponíveis: {', '.join(sorted(ctx)) or '(nenhuma)'}")
        return ctx[nome]

    if marca == "neg":
        return -_numero(_avaliar_no(no[1], ctx, fonte), "negativo")

    if marca == "op":
        simbolo, esq, dir_ = no[1], no[2], no[3]
        a = _avaliar_no(esq, ctx, fonte)
        b = _avaliar_no(dir_, ctx, fonte)

        if simbolo == "<>":
            # Único comparador que encara nulo: `[cnpj_terceiro_destino]<>null`
            # pergunta se o campo está preenchido.
            if a is None or b is None:
                return (a is None) != (b is None)
            if isinstance(a, str) or isinstance(b, str):
                return _texto(a).casefold() != _texto(b).casefold()
            return _numero(a, "<>") != _numero(b, "<>")

        if simbolo == ">":
            return _numero(a, ">") > _numero(b, ">")
        if simbolo == "<":
            return _numero(a, "<") < _numero(b, "<")

        x, y = _numero(a, simbolo), _numero(b, simbolo)
        if simbolo == "+":
            return x + y
        if simbolo == "-":
            return x - y
        if simbolo == "*":
            return x * y
        if simbolo == "/":
            if y == 0:
                raise ErroDeFormula(f"divisão por zero em {fonte!r}")
            return x / y
        raise ErroDeSintaxe(f"operador {simbolo!r} não existe na linguagem")

    if marca == "cham":
        nome, args = no[1], no[2]
        fn = FUNCOES.get(nome)
        if fn is None:
            raise FuncaoDesconhecida(
                f"{nome}() não existe na linguagem (em {fonte!r}). "
                f"Conhecidas: {', '.join(sorted(FUNCOES))}")
        if nome == "se":
            # Curto-circuito: `se(contem(...),[tst]*0.5,0)` não deve exigir
            # [tst] quando a condição é falsa — e o ramo morto pode citar
            # variável que aquele perfil não tem.
            if len(args) != 3:
                raise ErroDeFormula(f"se() quer 3 argumentos, veio {len(args)}")
            cond = _verdade(_avaliar_no(args[0], ctx, fonte), "se()")
            return _avaliar_no(args[1] if cond else args[2], ctx, fonte)
        if nome == "ou":
            for a in args:
                if _verdade(_avaliar_no(a, ctx, fonte), "ou()"):
                    return True
            if not args:
                raise ErroDeFormula("ou() sem argumentos")
            return False
        return fn([_avaliar_no(a, ctx, fonte) for a in args])

    raise ErroDeSintaxe(f"nó desconhecido: {no!r}")


def _colher_variaveis(no: tuple, saida: set[str]) -> None:
    if no[0] == "var":
        saida.add(no[1])
    elif no[0] == "neg":
        _colher_variaveis(no[1], saida)
    elif no[0] == "op":
        _colher_variaveis(no[2], saida)
        _colher_variaveis(no[3], saida)
    elif no[0] == "cham":
        for a in no[2]:
            _colher_variaveis(a, saida)


# ═══════════════════════════════════════════════════════════════════ fachada
@dataclass(frozen=True)
class Formula:
    """Uma expressão compilada. Imutável, reaproveitável, sem estado."""

    fonte: str
    _no: tuple
    variaveis: frozenset[str]

    def avaliar(self, contexto: Mapping[str, Any]) -> Any:
        """Devolve Decimal, str, bool ou None — conforme a expressão."""
        return _avaliar_no(self._no, contexto, self.fonte)

    def numero(self, contexto: Mapping[str, Any]) -> Decimal:
        """O caso comum: a expressão vale dinheiro, então vale Decimal."""
        v = self.avaliar(contexto)
        if isinstance(v, bool):
            raise ErroDeFormula(
                f"{self.fonte!r} resultou em condição, não em valor")
        return _numero(v, self.fonte)

    def __str__(self) -> str:
        return self.fonte


_cache: dict[str, Formula] = {}


def compilar(fonte: str) -> Formula:
    """Compila (e memoriza) uma expressão.

    São 2.770 expressões em 238 perfis, com 679 distintas — e uma cotação em
    lote avalia as mesmas repetidamente. Compilar uma vez por texto é o que
    torna o lote viável.
    """
    if not isinstance(fonte, str):
        raise ErroDeSintaxe(f"expressão tem que ser texto, veio {type(fonte).__name__}")
    achada = _cache.get(fonte)
    if achada is not None:
        return achada
    no = _Parser(fonte).analisar()
    vs: set[str] = set()
    _colher_variaveis(no, vs)
    f = Formula(fonte=fonte, _no=no, variaveis=frozenset(vs))
    _cache[fonte] = f
    return f


def avaliar(fonte: str, contexto: Mapping[str, Any] | None = None) -> Any:
    """Atalho: compila e avalia."""
    return compilar(fonte).avaliar(contexto or {})


def avaliar_numero(fonte: str, contexto: Mapping[str, Any] | None = None) -> Decimal:
    """Atalho para o caso de dinheiro."""
    return compilar(fonte).numero(contexto or {})


def variaveis_de(fonte: str) -> frozenset[str]:
    """De que variáveis a expressão depende — é o que ordena as passadas."""
    return compilar(fonte).variaveis
