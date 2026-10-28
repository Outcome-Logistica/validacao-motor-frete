# -*- coding: utf-8 -*-
"""As seis suposições que dependem de resposta da empresa parceira.

Todas juntas aqui, e não espalhadas pelo código, por um motivo prático: quando
as respostas chegarem, é um arquivo só para abrir.

Cada uma foi escolhida como a leitura mais provável do dado, está travada por um
teste em `tests/test_pendencias.py`, e traz o que muda se a resposta contrariar.

Perguntas enviadas em 26/09/2026 · resposta esperada em ~5 dias.

RESOLVIDA PELO DADO (01/10): a semântica de `tb_tarcli.tarcli`. Era listada
como desconhecida — código de tabela, percentual ou valor. É nenhuma das três:
são cadeias de LETRAS (195 valores distintos, no máximo 8 caracteres) e cada
letra liga uma cobrança. 'D' em 136 mil das 146 mil linhas é o TDE, que as
fórmulas leem como `contem('D',[tarcli])`; 'A' é agendamento, 'P' pallet,
'C' a generalidade2. Ver `resolucao.codigo_servicos_cliente()`.
Texto completo em `outcome_api/docs/GRAMATICA_GENERALIDADES.html`, seção 5.

⚠ NÃO rodar a validação contra os 2.560 CT-e reais antes das respostas. Se der
   diferença, não haverá como saber se é bug nosso ou suposição errada.
"""

from __future__ import annotations

from enum import Enum


# ═══════════════════════════════════════════════════════════ pergunta 1
class OrdemAvaliacao(str, Enum):
    ORDEM_DAS_DEPENDENCIAS = "ORDEM_DAS_DEPENDENCIAS"
    ORDEM_DA_TABELA = "ORDEM_DA_TABELA"


ORDEM_AVALIACAO = OrdemAvaliacao.ORDEM_DAS_DEPENDENCIAS
"""Em que ordem as generalidades são avaliadas.

SUPOSIÇÃO: cada coluna é avaliada depois das colunas que a fórmula dela lê —
ordenação topológica, uma por perfil.

POR QUÊ: **isto quase deixou de ser suposição.** A ordem não precisa ser
adivinhada porque o próprio dado a declara: o `tst` é uma expressão por perfil
que NOMEIA os seus componentes, e nas 238 linhas × 19 colunas, toda vez que a
fórmula de uma coluna lê `[tst]`, o `tst` daquele perfil deixa essa coluna de
fora. Zero ciclos, sem exceção. Consistência dessa ordem não sai de planilha
editada à mão — o `tst` foi gerado do mesmo lugar que as colunas.

    perfil 201  tst = …+[icm_iss]+[outros1]+[outros2]
    perfil 314  tst = …+[icm_iss]+[outros1]        ← generalidade2 lê [tst]
    perfil 234  ctrc = [tst]*0.0174                ← e o tst dele omite [ctrc]

O que sobra de suposição é só isto: que o sistema dela respeita esse grafo em
vez de varrer coluna por coluna na ordem da tabela.

SE A RESPOSTA CONTRARIAR: se ela varre na ordem da tabela, as colunas que leem
`[tst]` veem soma PARCIAL, não total. Muda
`generalidades.Perfil.ordem_de_avaliacao()` — um método. Vale dizer que seria
estranho: explicaria por quê o `tst` se dá o trabalho de nomear componente por
componente.

CORREÇÃO (26/09): a versão anterior desta nota dizia "três passadas fixas". O
corpus mostrou que passada fixa não serve — o grafo muda de perfil para perfil.
"""


# ═══════════════════════════════════════════════════════════ pergunta 2
class ModoExcedente(str, Enum):
    ULTIMA_FAIXA_MAIS_EXCESSO = "ULTIMA_FAIXA_MAIS_EXCESSO"
    PESO_TOTAL_VEZES_TARIFA = "PESO_TOTAL_VEZES_TARIFA"


MODO_EXCEDENTE = ModoExcedente.ULTIMA_FAIXA_MAIS_EXCESSO
"""Como o excedente se soma acima do teto da tabela de faixas.

SUPOSIÇÃO: preço da última faixa + (peso − teto) × tarifa do excedente.
Para 100 kg numa tabela que termina em 50–70 kg por R$ 161,88 com excedente de
R$ 2,32/kg: 161,88 + 30 × 2,32 = R$ 231,48.

POR QUÊ: é o desenho clássico da tabela de frete brasileira, e é o que dá
continuidade — sem ele o preço daria um salto para baixo logo acima do teto.

SE A RESPOSTA CONTRARIAR: a outra leitura (100 × 2,32 = R$ 232,00) dá número
PRÓXIMO, o que é pior: o erro não salta aos olhos. Muda `calculo.frete_peso()`.
Afeta todo frete acima do teto.
"""


# ═══════════════════════════════════════════════════════════ pergunta 3
VARIAVEL_FV = "valor_nf"
"""A que campo do embarque `[fv]` corresponde.

SUPOSIÇÃO: valor da nota fiscal.

POR QUÊ: é a base do GRIS (`[fv]*0.001`) e do ad valorem — ambos incidem sobre
o valor da mercadoria transportada, por definição.

SE A RESPOSTA CONTRARIAR: 567 usos apontam para outro campo. É uma linha aqui,
mas erra quase toda generalidade percentual se estiver errado.
"""


# ═══════════════════════════════════════════════════════════ pergunta 4
VARIAVEL_TARCID = "regioes_atendidas_vigencia.tar_cid"
"""De onde vem `[tarcid]`.

SUPOSIÇÃO: o campo `tar_cid` de `tb_regioes_atendidas_h`, que veio 19%
preenchido e aparece como número (ex.: 234).

POR QUÊ: é o único campo com esse nome em todo o banco dela.

SE A RESPOSTA CONTRARIAR: muda de onde o valor é lido. E o significado de cada
dígito continua desconhecido — `contem('1',[tarcid])` dispara o TRT, mas não
sabemos o que "1" representa.
"""


# ═══════════════════════════════════════════════════════════ pergunta 5
ALIAS_VARIAVEIS = {
    "outros1": "generalidade1",
    "outros2": "generalidade2",
}
"""Variáveis que as fórmulas citam mas não existem como coluna.

SUPOSIÇÃO: `[outros1]` e `[outros2]` são as colunas `generalidade1` e
`generalidade2`.

POR QUÊ: são as únicas duas colunas sem correspondência, e as únicas duas
variáveis sem coluna. A correspondência é 1 para 1.

SE A RESPOSTA CONTRARIAR: são 467 usos, dentro do [tst] de quase todo perfil.
Vale notar que `generalidades_nome1` e `generalidades_nome2` — que diriam o que
essas duas são — vieram 0% preenchidas. Ninguém sabe o que elas cobram.
"""


# ═══════════════════════════════════════════════════════════ pergunta 6
COMPONENTES_EVENTUAIS = frozenset({"reentrega", "devolucao", "fluvial"})
"""Generalidades que existem mas não entram no frete cotado.

SUPOSIÇÃO: são serviços eventuais — cobrados à parte, se e quando acontecerem.

POR QUÊ: são as três — e só elas — que **nunca** aparecem em nenhum `tst` dos
238 perfis. As outras 16 aparecem em 141 perfis ou mais. E reentrega, por
definição, só existe se a primeira entrega falhar.

SE A RESPOSTA CONTRARIAR: se entrarem na cotação, todo frete fica mais caro —
`reentrega` é tipicamente `maximo(73.95, [tst]*0.5)`, meio frete a mais. É um
`frozenset`: incluir ou excluir é editar esta linha.

`fluvial` é o caso menos óbvio dos três: para destino no Pará e no Amapá pode
ser trecho obrigatório do transporte, não serviço opcional. Aparece em 70
perfis.

CORREÇÃO (26/09): a versão anterior listava também `generalidade1` e
`generalidade2`, com a justificativa de que "não entram em nenhum tst". **Estava
errado, e por causa da própria pendência 5:** o `tst` cita `[outros1]` e
`[outros2]`, que são justamente essas duas colunas. Elas entram — em 142 e 83
perfis, respectivamente.

E o que entra no frete NÃO é uma lista global: é o `tst` de cada perfil que
declara os seus componentes, mais as cobranças que ficam fora dele de propósito
porque incidem sobre ele (`ctrc = [tst]*0.0174`, `icm_iss = [tst]*0.04`). Quem
resolve isso é `generalidades.avaliar_perfil()`, lendo o dado. Esta constante só
responde por "o que é eventual".
"""


# ═══════════════════════════════════════════════════════════ pergunta 7
class ModoFreteMinimo(str, Enum):
    PISO_DO_FRETE_PESO = "PISO_DO_FRETE_PESO"
    PISO_DO_TOTAL = "PISO_DO_TOTAL"


MODO_FRETE_MINIMO = ModoFreteMinimo.PISO_DO_FRETE_PESO
"""Sobre o que a linha `Frete_Minimo` das faixas incide.

SUPOSIÇÃO: é piso do frete-peso (VN2). Generalidades, sobre-frete e ICMS
somam por cima dele.

POR QUÊ: é o sentido clássico de "frete mínimo" na tabela brasileira — o piso
da tarifa de peso, não do documento. E a faixa 0–10 kg já cobre o piso do
embarque pequeno, então um piso do TOTAL seria uma segunda camada estranha.
São 200 linhas em 1.018 frete-peso.

SE A RESPOSTA CONTRARIAR: se for piso do total, o valor passa de `fp_minimo`
para `minimo_geral` em `calculo.tarifa_do_contexto()` — uma linha. O efeito é
só nos embarques baratos, exatamente onde o mínimo pega; nos caros, nada muda.

NOTA: esta pendência nasceu da carga, não da planilha de generalidades. Não
está entre as seis que foram enviadas em 26/09.
"""


# ═══════════════════════════════════════════════════════════ resumo
def resumo() -> list[dict]:
    """O que está suposto, para o /health e para os relatórios mostrarem."""
    return [
        {"n": 1, "assunto": "ordem de avaliação", "suposto": ORDEM_AVALIACAO.value,
         "impacto": "generalidades que dependem de [tst] — grafo lido do dado, "
                    "não suposto"},
        {"n": 2, "assunto": "excedente acima do teto", "suposto": MODO_EXCEDENTE.value,
         "impacto": "todo frete acima da última faixa"},
        {"n": 3, "assunto": "[fv]", "suposto": VARIAVEL_FV,
         "impacto": "567 usos — GRIS, ad valorem e afins"},
        {"n": 4, "assunto": "[tarcid]", "suposto": VARIAVEL_TARCID,
         "impacto": "TRT"},
        {"n": 5, "assunto": "[outros1] / [outros2]", "suposto": str(ALIAS_VARIAVEIS),
         "impacto": "467 usos, dentro do [tst]"},
        {"n": 6, "assunto": "componentes eventuais (fora da cotação)",
         "suposto": ", ".join(sorted(COMPONENTES_EVENTUAIS)),
         "impacto": "valor final da cotação"},
        {"n": 7, "assunto": "sobre o que o Frete_Minimo incide",
         "suposto": MODO_FRETE_MINIMO.value,
         "impacto": "200 frete-peso, só nos embarques baratos"},
    ]
