# -*- coding: utf-8 -*-
"""Contratos da API do motor-frete.

Este módulo é a parte mais importante do esqueleto: define a forma da entrada e
da saída do cálculo de frete. A cotação e a auditoria vão falar esta língua, e
mudar isso depois custa alterar os dois chamadores.

Três decisões que estão embutidas aqui:

1. Toda cotação carrega o RASTREIO — quais linhas do banco produziram o número
   (tabela, praça, faixa). Sem isso não dá para contestar a transportadora nem
   para a auditoria reconferir.

2. Os COMPONENTES vêm item a item, na mesma granularidade de tb_cte_tarifas.
   É o que torna a conferência da auditoria um JOIN em vez de comparar totais.

3. As transportadoras DESCARTADAS voltam com o motivo. Hoje, quando uma some da
   cotação, ninguém sabe por quê.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ════════════════════════════════════════════════════════════════ enums
class MotivoDescarte(str, Enum):
    """Por que uma transportadora não entrou na cotação.

    Cada um corresponde a um passo do algoritmo de resolução que não
    retornou linha. A ordem é a ordem em que são avaliados.
    """
    CEP_NAO_MAPEADO           = "CEP_NAO_MAPEADO"
    NAO_ATENDE_ROTA           = "NAO_ATENDE_ROTA"
    ROTA_SEM_VIGENCIA         = "ROTA_SEM_VIGENCIA"
    SEM_TABELA_VIGENTE        = "SEM_TABELA_VIGENTE"
    PRODUTO_NAO_ACEITO        = "PRODUTO_NAO_ACEITO"
    SEM_FRETE_PESO_PARA_PRACA = "SEM_FRETE_PESO_PARA_PRACA"
    PESO_FORA_DAS_FAIXAS      = "PESO_FORA_DAS_FAIXAS"
    SEM_ICMS_PARA_UF          = "SEM_ICMS_PARA_UF"
    TRANSPORTADORA_INATIVA    = "TRANSPORTADORA_INATIVA"


class Aviso(str, Enum):
    """A cotação saiu, mas com uma ressalva que o usuário precisa ver.

    Diferente do MotivoDescarte: ali a transportadora não entrou; aqui ela
    entrou, e algo na escolha merece ser dito. São todos casos em que o dado da
    parceira é ambíguo ou incompleto e o motor **teve que escolher**. Deixar a
    escolha implícita seria repetir o defeito que este projeto existe para tirar:
    hoje ninguém sabe por que a cotação mostrou um número e não outro.
    """
    TABELA_AMBIGUA = "TABELA_AMBIGUA"
    """Mais de uma tabela de frete serve a rota, e nada no cadastro as distingue.
    Usamos a de data de início mais recente, maior id. São 7 tabelas com
    frete-peso nessa situação."""

    TARIFA_AMBIGUA = "TARIFA_AMBIGUA"
    """Mais de um frete-peso serve a mesma praça e UF de destino, com **preços
    diferentes**. Cotamos pelo mais caro: errar para baixo vira prejuízo na
    fatura, errar para cima só perde uma comparação."""

    ICMS_ESTIMADO = "ICMS_ESTIMADO"
    """A tabela dela não trouxe a alíquota (sem linha `Imposto`), então usamos a
    Resolução do Senado 22/1989. Pode não ser a alíquota que ela fatura."""

    SEM_PRAZO = "SEM_PRAZO"
    """A vigência da rota veio sem prazo — 28% delas. O preço vale; a previsão
    de entrega, não."""

    SEM_CODIGO_SERVICOS = "SEM_CODIGO_SERVICOS"
    """O CNPJ de destino não casou com `tarifas_cliente_destino`, então o
    `[tarcli]` das fórmulas ficou nulo. As generalidades condicionais — TDE,
    agendamento, pallet — saem ZERO, e o frete sai **mais barato do que é**.
    A letra 'D' (TDE) aparece em 136 mil das 146 mil linhas, então este aviso
    quase sempre significa dinheiro faltando."""

    PALLETS_ESTIMADOS = "PALLETS_ESTIMADOS"
    """O perfil cobra por pallet, mas o chamador não informou quantos. O motor
    usou a quantidade de VOLUMES no lugar — e as duas coisas não são iguais:
    40 caixas podem ir em 2 pallets. Mande `quantidade_pallets` para o valor
    sair certo."""

    CALENDARIO_SUPOSTO = "CALENDARIO_SUPOSTO"
    """Sem calendário de atendimento cadastrado (99,6% das vigências), o prazo
    foi contado em dias corridos. Se ela não entrega no fim de semana, o prazo
    real é maior."""


class BaseIncidencia(str, Enum):
    """Sobre o que um componente tarifário incide."""
    PESO             = "PESO"
    VALOR_NF         = "VALOR_NF"
    VALOR_MERCADORIA = "VALOR_MERCADORIA"
    VOLUME           = "VOLUME"
    FIXO             = "FIXO"
    FRETE_PESO       = "FRETE_PESO"


# ════════════════════════════════════════════════════════════════ entrada
class Embarque(BaseModel):
    """A carga a ser cotada.

    Aceita pedido isolado ou embarque consolidado: quem monta a requisição
    decide o que é uma unidade de cotação. O motor não sabe a diferença — e é
    de propósito, porque a entidade embarque ainda não existe no Outcome.
    """
    model_config = ConfigDict(extra="forbid")

    referencia: str = Field(
        ..., min_length=1, max_length=60,
        description="Identificador de quem chamou (número do pedido, id do embarque, chave do CT-e). Volta na resposta para o chamador parear.")

    origem_cep: str = Field(..., pattern=r"^\d{8}$", description="CEP de origem, só dígitos")
    destino_cep: str = Field(..., pattern=r"^\d{8}$", description="CEP de destino, só dígitos")

    peso_bruto_kg: Decimal = Field(..., ge=0, description="Peso real somado dos volumes")
    volume_m3: Decimal = Field(default=Decimal("0"), ge=0, description="Cubagem em m³")
    valor_nf: Decimal = Field(..., ge=0, description="Valor total da nota fiscal")
    valor_mercadoria: Optional[Decimal] = Field(default=None, ge=0)
    quantidade_volumes: int = Field(default=1, ge=1)
    quantidade_pallets: Optional[int] = Field(
        default=None, ge=0,
        description="Pallets do embarque — é o `[pallets]` das fórmulas, usado em "
                    "`se(contem('P',[tarcli]),[pallets]*29.68,0)`. Volume e pallet "
                    "são coisas diferentes: 40 caixas podem ir em 2 pallets. "
                    "Nulo = o motor usa a quantidade de volumes e avisa.")

    tipo_nf: Optional[str] = Field(default=None, description="VENDA | TRANSFERENCIA | DEVOLUCAO. Nulo = qualquer.")
    tipo_frete: Optional[str] = Field(default=None, description="FRACIONADO, CARRO DEDICADO, REFRIGERADO… Nulo = qualquer.")
    modalidades: Optional[list[str]] = Field(
        default=None, description="Restringe a estas modalidades. Nulo = todas as que atendem a rota.")

    cnpj_destino: Optional[str] = Field(
        default=None, description="Para aplicar tarifa negociada por cliente destino (tarifas_cliente_destino).")
    categorias_produto: Optional[list[str]] = Field(
        default=None, description="Categorias dos itens. Filtra tabelas que recusam esses produtos.")


class RequisicaoCalculo(BaseModel):
    """Cotação de um embarque."""
    model_config = ConfigDict(extra="forbid")

    embarque: Embarque
    data_referencia: Optional[date] = Field(
        default=None,
        description="Data usada para resolver vigência de tabela e de rota. "
                    "Nulo = hoje. A auditoria manda a data de saída do embarque — "
                    "é o que torna o recálculo reprodutível.")
    transportadoras: Optional[list[int]] = Field(
        default=None, description="Restringe a estes ids. Nulo = todas as ativas.")
    incluir_descartadas: bool = Field(
        default=True, description="Devolve também as transportadoras que não entraram, com o motivo.")
    incluir_memoria: bool = Field(
        default=True, description="Devolve o passo a passo do cálculo. Desligue em lote grande.")


class RequisicaoLote(BaseModel):
    """Cotação de N embarques numa chamada.

    É a interface principal, não uma conveniência: a auditoria recalcula milhares
    de CT-e por fechamento. Uma chamada por CT-e transformaria cada auditoria em
    milhares de round-trips.
    """
    model_config = ConfigDict(extra="forbid")

    embarques: list[Embarque] = Field(..., min_length=1, max_length=5000)
    data_referencia: Optional[date] = None
    transportadoras: Optional[list[int]] = None
    incluir_descartadas: bool = False
    incluir_memoria: bool = Field(
        default=False, description="Em lote vem desligado: a memória de N embarques × M transportadoras fica grande.")
    parar_no_erro: bool = Field(
        default=False, description="Falso: um embarque com erro não derruba o lote, volta com erro no próprio item.")


# ════════════════════════════════════════════════════════════════ saída
class Local(BaseModel):
    """Um CEP resolvido em localidade."""
    cep: str
    localidade_id: Optional[int] = None
    localidade: Optional[str] = None
    uf: Optional[str] = None


class Componente(BaseModel):
    """Uma linha da composição do frete.

    Mesma granularidade de tb_cte_tarifas: é assim que a auditoria compara o que
    cotamos com o que a transportadora cobrou, componente a componente.
    """
    codigo: str = Field(..., description="GRIS, ADV, PEDAGIO, DESPACHO, TAS…")
    nome: str
    base_incidencia: BaseIncidencia
    base_calculo: Decimal = Field(..., description="Valor sobre o qual incidiu")
    valor: Decimal
    aplicou_minimo: bool = False
    aplicou_maximo: bool = False


class Rastreio(BaseModel):
    """Quais linhas do banco produziram este número.

    Sem isso, um valor contestado não tem como ser defendido — e a auditoria não
    consegue reconferir o que a cotação decidiu.
    """
    tabela_frete_id: Optional[int] = None
    nome_tabela: Optional[str] = None
    tabela_frete_peso_id: Optional[int] = None
    faixa_valor_id: Optional[int] = None
    aplicou_excedente: bool = Field(
        default=False, description="O peso passou do teto da tabela e o excedente entrou")
    icms_de: Optional[str] = Field(
        default=None, description="TABELA (linha Imposto da tarifa dela) | ICMS_UF (Resolução 22/1989)")
    regiao_atendida_id: Optional[int] = None
    praca_id: Optional[int] = None
    praca: Optional[str] = None
    modalidade_id: Optional[int] = None
    modalidade: Optional[str] = None
    perfil_generalidade_id: Optional[int] = None
    vigencia_inicio: Optional[date] = None
    vigencia_fim: Optional[date] = None


class Cotacao(BaseModel):
    """Uma transportadora cotada, com o cálculo aberto em VN1–VN6."""
    transportadora_id: int
    transportadora: str

    peso_taxado_kg: Decimal = Field(..., description="VN1 — max(peso real, m³ × fator de cubagem)")
    frete_peso: Decimal     = Field(..., description="VN2")
    generalidades: Decimal  = Field(..., description="VN3 — soma dos componentes")
    sobre_frete: Decimal    = Field(..., description="VN4")
    icms: Decimal           = Field(..., description="VN5")
    frete_liquido: Decimal  = Field(..., description="VN2 + VN3 + VN4")
    frete_total: Decimal    = Field(..., description="VN6 — respeitando o mínimo geral")
    aplicou_minimo_geral: bool = False

    prazo_dias: Optional[int] = None
    previsao_entrega: Optional[date] = None

    componentes: list[Componente] = Field(default_factory=list)
    avisos: list[Aviso] = Field(
        default_factory=list,
        description="Ressalvas da cotação: ambiguidade no cadastro, ICMS estimado, "
                    "prazo ausente. Cotação com aviso é válida, mas o usuário "
                    "precisa saber o que o motor teve que decidir.")
    rastreio: Rastreio = Field(default_factory=Rastreio)
    memoria_calculo: Optional[dict] = Field(
        default=None, description="Passo a passo auditável. Presente quando incluir_memoria.")


class Descartada(BaseModel):
    """Uma transportadora que não entrou na cotação, e por quê."""
    transportadora_id: int
    transportadora: str
    motivo: MotivoDescarte
    detalhe: Optional[str] = None


class ResumoCarga(BaseModel):
    origem: Local
    destino: Local
    peso_bruto_kg: Decimal
    volume_m3: Decimal
    valor_nf: Decimal


class RespostaCalculo(BaseModel):
    """Resultado da cotação de um embarque."""
    referencia: str
    data_referencia: date
    resumo_carga: ResumoCarga
    cotacoes: list[Cotacao] = Field(
        default_factory=list, description="Ordenadas da mais barata para a mais cara")
    descartadas: list[Descartada] = Field(default_factory=list)
    erro: Optional[str] = Field(
        default=None, description="Preenchido quando o embarque inteiro falhou (em lote)")


class ResumoLote(BaseModel):
    embarques: int
    com_cotacao: int
    sem_cotacao: int
    com_erro: int
    duracao_ms: int


class RespostaLote(BaseModel):
    data_referencia: date
    resumo: ResumoLote
    resultados: list[RespostaCalculo]


# ════════════════════════════════════════════════════════════════ saúde
class Saude(BaseModel):
    status: str = Field(..., description="ok | degradado")
    servico: str
    versao: str
    banco: str = Field(..., description="ok | indisponivel")
    schema_cotacao: str = Field(..., description="ok | incompleto — se as 21 tabelas existem")
    detalhe: Optional[str] = None
