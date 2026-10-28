# -*- coding: utf-8 -*-
"""Configuração por variável de ambiente.

Mesmas chaves MYSQL_* que o outcome_api usa, de propósito: no compose os dois
leem o mesmo banco, e um .env divergente seria uma fonte silenciosa de bug.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    SERVICO_NOME: str = "motor-frete"
    SERVICO_VERSAO: str = "0.1.0"
    PORTA: int = 8003

    MYSQL_HOST: str = "db"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "outcome_db"

    # Usado quando a modalidade não define o seu. 300 kg/m³ é o padrão rodoviário
    # — era constante no cotacao_router e aqui vira configuração.
    FATOR_CUBAGEM_PADRAO: float = 300.0

    # Dias entre a emissão e a coleta, quando a rota não informa.
    DIAS_COLETA_PADRAO: int = 1

    # ── resolução de CEP ──────────────────────────────────────────────────
    # Quando o CEP não cai em nenhuma faixa conhecida, o motor pergunta o
    # código IBGE a um serviço público e guarda o resultado para sempre.
    # Desligue se a rede do container não tiver saída — a cascata continua
    # funcionando pela faixa de UF, só perde a precisão de município.
    CEP_CONSULTA_EXTERNA: bool = True
    CEP_TIMEOUT_S: float = 4.0
    CEP_MAX_TENTATIVAS: int = 3

    LOG_NIVEL: str = "INFO"


settings = Settings()
