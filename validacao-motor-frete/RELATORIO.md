# Validação do motor de frete

## 1. Como o frete é calculado

```
                    ┌─────────────────────────────────┐
  peso taxado ──►   │  Camada 1 · de quanto é a tarifa │
                    │  faixa / excedente / recusa      │
                    └──────────────┬──────────────────┘
                                   │ preço em R$
                                   ▼
  carga + tarifa ─► ┌─────────────────────────────────┐
                    │  Camada 2 · VN1 … VN6            │
                    │  peso → frete-peso → taxas →     │
                    │  sobre-frete → ICMS → total      │
                    └──────────────┬──────────────────┘
                                   │
                                   ▼
                              frete_total
```


| Nível | O que é | Conta |
|---|---|---|
| VN1 | peso taxado | `max(peso real, m³ × fator de cubagem)` |
| VN2 | frete-peso | preço da faixa (já em R$), respeitando o piso |
| VN3 | generalidades | soma das fórmulas (GRIS, pedágio, despacho…) |
| VN4 | sobre-frete | `(VN2 + VN3) × percentual`, zero no cadastro dela |
| líquido | antes do imposto | `VN2 + VN3 + VN4` |
| VN5 | ICMS | `líquido × alíquota` |
| VN6 | total | `líquido + ICMS` (respeitando mínimo geral, se houver) |


---

## 2. Como os testes funcionam

```python
# modo faixa
preco, _, com_exc = preco_da_faixa(faixas, peso)

# modo cálculo
cotacao = calcular(embarque, contexto, data_referencia)

# comparação
assert abs(esperado - obtido) <= Decimal("0.01")
```

Os casos estão em [`casos.json`](casos.json).

O notebook [`conferir.ipynb`](conferir.ipynb)
carrega o JSON, chama a função certa e imprime uma linha por caso.

---

## 3. Os casos

### Camada 1: de quanto é a tarifa

Os casos **1 a 4** não calculam o frete total. Conferem só a peça
`preco_da_faixa()`: o preço da banda, o excedente ou a recusa.

#### Caso 1: Peso dentro da faixa (7 kg) (só o preço da faixa, não o frete total)

**Justificativa.** Nas linhas `Faixa` o preço está em `minimo` e `valor` vem
vazio. Se o motor ler a coluna errada, cobra zero.

| Entrada | Valor |
|---|---|
| banda | 0–10 kg |
| preço (`minimo`) | R$ 200,00 |
| peso | 7 kg |

**Conta.** 7 kg cai na banda 0–10. Lê `minimo`.

**Esperado.** R$ 200,00

#### Caso 2: Peso no limite da banda (70 kg) (só o preço da faixa, não o frete total)

**Justificativa.** O limite de cima conta na banda de baixo. Se não for assim,
70 kg ia pro excedente sem precisar.

| Entrada | Valor |
|---|---|
| bandas | 0–10 / 10–30 / 30–70 |
| preço da última | R$ 161,88 |
| excedente | R$ 2,32/kg acima de 70 |
| peso | 70 kg |

**Conta.** 70 kg está no fim da banda 30–70. Excedente ainda não entra.

**Esperado.** R$ 161,88

#### Caso 3: Peso acima do teto, com excedente (só o preço da faixa, não o frete total)

**Justificativa.** Acima do teto a conta certa é
`última faixa + (peso − teto) × R$/kg`. Se fizer `peso × R$/kg` erra uns
centavos (231,48 vs 232,00) e no total quase não dá pra ver.

| Entrada | Valor |
|---|---|
| última faixa | até 70 kg por R$ 161,88 |
| excedente | R$ 2,32/kg |
| peso | 100 kg |

**Conta.**

```
161,88 + (100 − 70) × 2,32
161,88 + 69,60
= R$ 231,48
```

**Esperado.** R$ 231,48 (com excedente aplicado)

#### Caso 4: Peso acima do teto, sem excedente (só a recusa, não o frete total)

**Justificativa.** Sem excedente cadastrado, acima do teto não tem preço.
Melhor recusar do que inventar valor.

| Entrada | Valor |
|---|---|
| banda | 0–10 kg por R$ 200 |
| excedente | nenhum |
| peso | 500 kg |

**Conta.** Fora das bandas, sem excedente → recusa.

**Esperado.** `PESO_FORA_DAS_FAIXAS`

---

### Camada 2: como a tarifa vira frete

Os casos **5 a 21** montam a cotação inteira com `calcular()` e batem o
**frete total** (e os níveis VN1–VN6).

#### Caso 5: Só o frete-peso

**Justificativa.** Depois que a faixa já veio em reais, não pode multiplicar de
novo pelo peso. Senão vira R$ 25.000 no lugar de R$ 250.

| Entrada | Valor |
|---|---|
| peso taxado | 100 kg |
| preço da faixa | R$ 250 (fechado) |
| ICMS | 0 |
| fórmulas | nenhuma |

**Conta.** VN2 = 250. Sem taxas, sem imposto.

**Esperado.** Total R$ 250,00

#### Caso 6: Mínimo do frete-peso pega

**Justificativa.** Se a faixa fica abaixo do piso, usa o piso. Se ignorar o
piso, a cotação fica mais barata e passa.

| Entrada | Valor |
|---|---|
| preço da faixa | R$ 40 |
| piso (`Frete_Minimo`) | R$ 77,40 |

**Conta.** `max(40, 77,40) = 77,40`. A linha marca que o mínimo foi aplicado.

**Esperado.** Total R$ 77,40

#### Caso 7: Mínimo do frete-peso não pega

**Justificativa.** Outro lado do caso 6. Sem esse, um piso que sempre aplica
também passaria.

| Entrada | Valor |
|---|---|
| preço da faixa | R$ 250 |
| piso | R$ 77,40 |

**Conta.** 250 > 77,40. Mínimo não aplicado.

**Esperado.** Total R$ 250,00

#### Caso 8: GRIS, percentual ganha do piso

**Justificativa.** A fórmula `maximo(5, nota × 0,002)` escolhe o maior. Nota
grande usa o percentual.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| nota | R$ 10.000 |
| fórmula | `maximo(5, [fv]*0.002)` |

**Conta.** `10.000 × 0,002 = 20`. `max(5, 20) = 20`. Total = 250 + 20.

**Esperado.** Total R$ 270,00

#### Caso 9: GRIS, piso ganha do percentual

**Justificativa.** Outro lado do caso 8. Nota pequena cai no piso de R$ 5.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| nota | R$ 1.000 |
| fórmula | a mesma do caso 8 |

**Conta.** `1.000 × 0,002 = 2`. `max(5, 2) = 5`. Total = 250 + 5.

**Esperado.** Total R$ 255,00

#### Caso 10: GRIS e despacho em linhas separadas

**Justificativa.** A auditoria olha linha por linha. Se só tiver o total, não
dá pra ver quanto foi GRIS e quanto foi despacho.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| GRIS | nota 10.000 × 0,2% → R$ 20 |
| despacho (CTRC) | R$ 15 fixo |

**Conta.** Generalidades = 20 + 15 = 35. Linhas `GRIS` e `CTRC` separadas.

**Esperado.** Total R$ 285,00

#### Caso 11: tarcli liga a cobrança de pallet

**Justificativa.** `tarcli` é a lista de serviços contratados (uma letra por
serviço). A letra P liga a fórmula do pallet.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| tarcli | `ADP` |
| pallets | 2 |
| fórmula | `se(contem('P',[tarcli]), [pallets]*29.68, 0)` |

**Conta.** Contém P → 2 × 29,68 = 59,36. Total = 250 + 59,36.

**Esperado.** Total R$ 309,36

#### Caso 12: tarcli desliga a cobrança de pallet

**Justificativa.** Outro lado do caso 11. Sem a letra P, a mesma fórmula e os
mesmos pallets cobram zero. Errar o `tarcli` deixa a cotação mais barata.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| tarcli | `AD` (sem P) |
| pallets | 2 |
| fórmula | a mesma do caso 11 |

**Conta.** Condição falsa → pallet = 0.

**Esperado.** Total R$ 250,00

#### Caso 13: Pallet com a quantidade informada

**Justificativa.** Volume e pallet não são a mesma coisa: 40 caixas podem ir
em 2 pallets. A fórmula usa o campo de pallets, não o de volumes.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| volumes | 40 |
| pallets | 2 |
| tarcli | `P` |

**Conta.** 2 × 29,68 = 59,36 (não 40 × 29,68). Sem aviso.

**Esperado.** Total R$ 309,36

#### Caso 14: Pallet estimado pelos volumes

**Justificativa.** Outro lado do caso 13. Sem quantidade de pallets, o motor
usa os volumes e tem que avisar. O aviso mostra que foi estimativa.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| volumes | 4 |
| pallets | em branco |
| tarcli | `ADP` |

**Conta.** 4 × 29,68 = 118,72. Aviso `PALLETS_ESTIMADOS`.

**Esperado.** Total R$ 368,72 · aviso `PALLETS_ESTIMADOS`

#### Caso 15: Pedágio no Rio de Janeiro

**Justificativa.** Pedágio só cobra se a UF de destino está na lista. RJ está.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| destino | RJ |
| peso | 100 kg |
| fórmula | `arredondar_para_cima0(peso/100) × 7` se UF na lista |

**Conta.** `arredondar_para_cima0(100/100) = 1`. 1 × 7 = 7.

**Esperado.** Total R$ 257,00

#### Caso 16: Pedágio fora da lista (Bahia)

**Justificativa.** Outro lado do caso 15. BA não está na lista → pedágio zero.
Zero também precisa aparecer.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| destino | BA |
| peso | 100 kg |
| fórmula | a mesma do caso 15 |

**Conta.** Condição falsa → pedágio = 0.

**Esperado.** Total R$ 250,00

#### Caso 17: Pedágio com fração incompleta (101 kg)

**Justificativa.** Fecha os casos 15 e 16: 101 kg no RJ viram 2 frações, não 1.
Sem isso não testa o arredondamento pra cima.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| destino | RJ |
| peso | 101 kg |
| fórmula | a mesma do caso 15 |

**Conta.** `arredondar_para_cima0(101/100) = 2`. 2 × 7 = 14.

**Esperado.** Total R$ 264,00

#### Caso 18: Peso cubado

**Justificativa.** Cubagem muda o peso taxado. O preço da faixa já está em
reais, então o peso maior não multiplica o preço. Mandei `peso_taxado = 0`
pra forçar o motor a calcular a cubagem.

| Entrada | Valor |
|---|---|
| peso real | 100 kg |
| volume | 1 m³ |
| fator | 300 |
| preço da faixa | R$ 250 (fechado) |

**Conta.** `max(100, 1 × 300) = 300` kg taxados. Frete continua 250.

**Esperado.** Peso taxado 300,00 kg · total R$ 250,00

#### Caso 19: ICMS sobre o líquido

**Justificativa.** O ICMS entra em cima de frete-peso + generalidades, não só
do frete-peso. Se errar a base sai R$ 30 no lugar de R$ 36.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| GRIS | R$ 50 fixo |
| ICMS | 12% |

**Conta.** Líquido = 250 + 50 = 300. ICMS = 300 × 0,12 = 36. Total = 336.

**Esperado.** Total R$ 336,00

#### Caso 20: Ad valorem da faixa

**Justificativa.** 0,32% da nota vem da linha `AdValore` da tarifa, não da
fórmula `adv` do perfil. Nos dados reais as duas não aparecem juntas. Aqui
testei o lado da faixa.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| ad valorem | 0,32% da nota |
| nota | R$ 10.000 |

**Conta.** `10.000 × 0,0032 = 32`. Total = 250 + 32.

**Esperado.** Total R$ 282,00

#### Caso 21: Reentrega fora do total

**Justificativa.** Reentrega, devolução e fluvial são eventuais: o motor
calcula pra mostrar na tela, mas não soma no total. Se somasse, a cotação
ficaria mais cara por um serviço que pode nem acontecer.

| Entrada | Valor |
|---|---|
| frete-peso | R$ 250 |
| GRIS | R$ 100 |
| reentrega | `tst × 0,5` |

**Conta.** Generalidades = 100. Reentrega = 50 (à parte). Total = 350, não 400.

**Esperado.** Total R$ 350,00 · eventual reentrega R$ 50,00

---

## 4. Execução

Rodado a partir da raiz do repositório, sem banco:

```
ok  caso  1  faixa_dentro_7kg             → 200.00
ok  caso  2  faixa_limite_70kg            → 161.88
ok  caso  3  excedente_acima_do_teto      → 231.48
ok  caso  4  peso_fora_sem_excedente      → PESO_FORA_DAS_FAIXAS
ok  caso  5  so_frete_peso                → 250.00
ok  caso  6  minimo_do_frete_pega         → 77.40
ok  caso  7  minimo_do_frete_nao_pega     → 250.00
ok  caso  8  gris_percentual              → 270.00
ok  caso  9  gris_piso                    → 255.00
ok  caso 10  gris_e_despacho              → 285.00
ok  caso 11  tarcli_liga                  → 309.36
ok  caso 12  tarcli_desliga               → 250.00
ok  caso 13  pallet_informado             → 309.36
ok  caso 14  pallet_estimado              → 368.72
ok  caso 15  pedagio_rj                   → 257.00
ok  caso 16  pedagio_ba                   → 250.00
ok  caso 17  pedagio_rj_101kg             → 264.00
ok  caso 18  peso_cubado                  → 250.00
ok  caso 19  icms_12                      → 336.00
ok  caso 20  ad_valorem                   → 282.00
ok  caso 21  reentrega_fora_do_total      → 350.00

21 de 21 passaram · 0 falharam
```

Para repetir:

```bash
uv run jupyter nbconvert --to notebook --execute --inplace \
  validacao-motor-frete/conferir.ipynb
```

Ou abrir
[`conferir.ipynb`](conferir.ipynb)
e rodar todas as células.

---

## 5. Resultado

**21 de 21 passaram.** Em todos esses comportamentos, o motor devolveu o mesmo
número da conta que eu fiz na mão.

---

## Arquivos

| Arquivo | O que é |
|---|---|
| `../README.md` | Relatório na raiz (igual a este) |
| `RELATORIO.md` | Este relatório |
| `casos.json` | Entradas, conta manual e valores esperados |
| `conferir.ipynb` | Notebook que roda a comparação |
| `saida/conferencia.json` | Evidência bruta da última execução |
| `../src/motor_frete/` | Cópia do motor (pro notebook rodar sozinho) |
