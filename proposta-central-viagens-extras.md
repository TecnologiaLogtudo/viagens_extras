# Proposta de Projeto — Central de Solicitação de Viagens Extras
**Logtudo · Ecossistema de Gestão Logística · Projeto 6**
Versão 1.0 · Maio 2026

---

## 1. Visão Geral do Projeto

### 1.1 Contexto e Problema

Atualmente, empresas parceiras solicitam carros extras, dedicados e segundas viagens exclusivamente via grupos de WhatsApp organizados por base. Nesses grupos circulam, no mesmo canal e sem separação formal, comunicações de rota, mensagens operacionais, solicitações de viagens extras, dedicados, dúvidas de rota e solicitações de segunda viagem.

Esse modelo apresenta os seguintes problemas críticos:

- **Ausência de registro formal e rastreável** das solicitações e das respostas da operação
- **Impossibilidade de comprovar autoria e aceite** para fins de cobrança futura
- **Sem padronização de informações**: cada solicitação chega com campos distintos e incompletos
- **Sem SLA formal**: sem tempo máximo de resposta nem alerta de vencimento
- **Sem visibilidade gerencial**: impossível extrair métricas de volume, taxa de atendimento e pico de demanda
- **Risco operacional**: mensagens podem ser ignoradas, perdidas ou respondidas fora de ordem na fila do grupo

### 1.2 Objetivo do Projeto

Desenvolver a **Central de Solicitação de Viagens Extras**, uma plataforma web que:

1. Substitui o WhatsApp como canal oficial de pedidos de viagens extras
2. Estrutura o fluxo de solicitação, triagem, confirmação e aceite em etapas formais
3. Coleta **assinatura eletrônica do solicitante** após a confirmação da operação, gerando evidência válida para cobrança
4. Gera **comprovantes imutáveis em PDF** com trilha de auditoria completa
5. Integra ao ecossistema Logtudo, funcionando como o hub de demanda extraordinária dos demais sistemas

### 1.3 Posicionamento no Ecossistema

Este é o **Projeto 6** do portfólio Logtudo e o **primeiro a entrar em produção**, por seu impacto imediato na operação e no faturamento. Apesar de ser criado antes dos demais, já nasce integrado ao ecossistema:

| Projeto | Integração com a Central |
|---------|--------------------------|
| 4 · Portal do Cliente | Parceiro abre pedidos, acompanha status, assina e baixa comprovantes |
| Sistema Operacional de Rotas | Consome confirmação para alocação e despacho de motoristas |
| Módulo de Comprovantes/Tracking | Vincula pedido extra à viagem executada |
| Faturamento | Exporta apenas pedidos com aceite válido e tarifa registrada |
| BI / Analytics | Métricas de volume, SLA, pico e taxa de atendimento por base |

O WhatsApp **não é eliminado** — continua sendo usado para avisos rápidos e comunicação de rota. O que muda é que toda solicitação oficial passa a nascer no sistema, com número de protocolo e rastreabilidade.

---

## 2. Perfis de Usuário

### 2.1 Solicitante — Empresa Parceira

**Quem é:** Colaborador da empresa parceira autorizado a solicitar viagens extras para uma ou mais bases.

**Necessidades:**
- Abrir pedidos com campos estruturados, de qualquer dispositivo
- Acompanhar o status do pedido em tempo real
- Receber notificações quando a operação responder
- Assinar o aceite de forma simples, sem burocracia
- Baixar comprovantes para apresentar ao próprio financeiro

**Restrições de acesso:**
- Visualiza apenas os pedidos da sua empresa
- Solicita apenas para as bases autorizadas no contrato
- Não visualiza dados internos da operação (tarifa interna, observações da base)

**Perfis dentro do grupo "Parceiro":**

| Perfil | Permissão |
|--------|-----------|
| Solicitante | Abre pedidos, assina, acompanha, baixa comprovantes |
| Aprovador | Tudo do Solicitante + aprova pedidos abertos por outros da equipe |
| Gestor Parceiro | Tudo do Aprovador + visão de todos os pedidos da empresa + relatórios |

### 2.2 Supervisor de Base — Operação

**Quem é:** Responsável operacional de uma base. Recebe, avalia e responde pedidos de viagens extras.

**Necessidades:**
- Visualizar fila de pedidos da sua base com SLA visível
- Confirmar, ajustar ou recusar pedidos com registro de motivo
- Alocar motorista e veículo à viagem confirmada
- Registrar execução e encerramento da viagem
- Receber alertas quando o SLA estiver próximo do vencimento

**Restrições de acesso:**
- Visualiza e age apenas sobre pedidos da(s) sua(s) base(s)

### 2.3 Gerente de Logística

**Quem é:** Responsável pela operação global. Acompanha todas as bases.

**Necessidades:**
- Visão consolidada de todos os pedidos em andamento por base
- Indicadores: volume, taxa de atendimento, SLA médio, recusas, pico por horário
- Capacidade de intervir em pedidos escalados
- Configuração de regras por base (SLA, antecedência mínima, limite por empresa)
- Relatórios exportáveis para reuniões e faturamento

### 2.4 Financeiro

**Quem é:** Responsável pela emissão de cobranças às empresas parceiras.

**Necessidades:**
- Consultar lista de viagens faturáveis (concluídas + aceite válido)
- Filtrar por empresa, base e período
- Exportar relatório com dados de tarifa, contrato e comprovante
- Acessar documento de aceite em caso de contestação

**Restrições:** Não interage com o fluxo operacional. Acesso somente leitura.

### 2.5 Motorista *(integração futura, fase 3)*

**Quem é:** Motorista alocado à viagem extra.

**Necessidades:**
- Receber a tarefa de viagem extra (app mobile ou notificação)
- Confirmar recebimento
- Registrar conclusão da viagem

---

## 3. Telas do Sistema

### 3.1 Telas do Portal do Parceiro

#### T-P01 · Login
- Formulário de e-mail e senha
- "Esqueci minha senha" com envio por e-mail
- Identificação da empresa após login (nome e logo)

#### T-P02 · Dashboard do Parceiro
- Contadores: pedidos abertos, aguardando aceite, em execução, concluídos no mês
- Lista dos últimos 10 pedidos com status e ação rápida
- Botão primário "Novo Pedido"
- Alerta em destaque quando há pedidos aguardando a assinatura do aceite

#### T-P03 · Novo Pedido
**Campos obrigatórios:**
- Tipo de solicitação: extra, dedicado, 2ª viagem, reforço de rota, substituição
- Base de atendimento (dropdown com bases autorizadas pelo contrato)
- Data e horário desejado
- Origem e destino (texto livre + mapa opcional)
- Quantidade de carros
- Tipo de veículo (sedan, SUV, van, micro-ônibus — conforme base)
- Centro de custo / contrato
- Motivo (dropdown com opções + campo livre)
- Observações e anexos (opcionais)

**Validações em tempo real:**
- Antecedência mínima (alerta se abaixo do configurado pela base)
- Base disponível para o contrato selecionado

#### T-P04 · Detalhe do Pedido
- Timeline visual do ciclo de vida do pedido com timestamps
- Dados da confirmação operacional (quando disponível)
- Status atual em destaque
- Seção de aceite (quando pedido confirmado): resumo da confirmação + botão "Assinar e confirmar"
- Histórico de mensagens internas visíveis ao parceiro
- Botão de download do comprovante (quando disponível)

#### T-P05 · Tela de Aceite e Assinatura
- Resumo congelado da confirmação: protocolo, empresa, base, data/hora confirmada, quantidade, tipo de veículo, tarifa e condições
- Declaração de aceite em texto claro
- Campo para inserção do código OTP (enviado por WhatsApp ou e-mail)
- Botão "Confirmar aceite"
- Mensagem de sucesso + PDF gerado automaticamente

#### T-P06 · Lista de Pedidos
- Filtros: status, base, tipo, período, empresa (para gestor parceiro)
- Exportação CSV
- Ação rápida por linha: ver detalhe, assinar (quando pendente), baixar PDF

#### T-P07 · Comprovantes
- Lista de comprovantes gerados com filtro por período
- Download individual ou em lote (ZIP)
- Visualização online do PDF antes do download

---

### 3.2 Telas do Painel da Operação

#### T-O01 · Fila de Pedidos da Base
- Cards de pedidos ordenados por SLA (urgente primeiro)
- Filtros: status, tipo, empresa, data
- SLA visível em cada card com indicador de cor (verde / amarelo / vermelho)
- Ação rápida: abrir triagem sem sair da fila

#### T-O02 · Triagem do Pedido
- Todos os dados do pedido do parceiro
- Seletor de resposta: confirmar, confirmar parcialmente, propor alternativa, recusar
- Campos de confirmação: qtd aprovada, horário confirmado, tipo de veículo, tarifa, observações
- Registro de motivo em caso de recusa ou ajuste
- Botão "Salvar e notificar parceiro"

#### T-O03 · Despacho
- Pedidos com aceite assinado aguardando despacho
- Alocação de motorista (autocomplete do cadastro) e veículo (placa)
- Horário de saída previsto
- Botão "Despachar"

#### T-O04 · Execução em Andamento
- Pedidos em execução com tempo decorrido
- Registro de ocorrências durante a viagem
- Botão "Concluir viagem" (gera comprovante automaticamente)

#### T-O05 · Painel de Indicadores da Base
- Volume de pedidos por dia / semana / mês
- Taxa de atendimento (confirmados / total)
- SLA médio de resposta
- Top empresas solicitantes
- Pedidos recusados e motivos mais frequentes

---

### 3.3 Telas do Painel Gerencial

#### T-G01 · Visão Consolidada (todas as bases)
- KPIs: pedidos abertos, em SLA crítico, aceites pendentes, viagens em execução
- Mapa ou lista de bases com status operacional
- Alertas de SLA vencido em destaque

#### T-G02 · Relatório de Faturamento
- Filtro por empresa, base, período e status
- Tabela com: protocolo, empresa, base, data, tipo, qtd, tarifa, status do aceite
- Exportação CSV e PDF
- Acesso ao comprovante de cada pedido

#### T-G03 · Configurações do Sistema
- SLA por base (tempo máximo para resposta da operação)
- Antecedência mínima de solicitação por base
- Cadastro de bases e empresas parceiras
- Configuração de contratos: quais bases cada empresa pode solicitar
- Gestão de usuários e perfis

---

## 4. Regras de Negócio

### 4.1 Regras de Solicitação

| ID | Regra |
|----|-------|
| RN-01 | Um solicitante só pode abrir pedidos para bases autorizadas pelo contrato da sua empresa |
| RN-02 | A antecedência mínima de solicitação é configurável por base (padrão: 2 horas) |
| RN-03 | Pedidos com antecedência abaixo do mínimo são bloqueados pelo sistema (ou marcados como "urgente" se a regra permitir) |
| RN-04 | Campos obrigatórios: tipo, base, data/hora, origem, destino, qtd, tipo de veículo, centro de custo |
| RN-05 | Centros de custo inativos bloqueiam a submissão do pedido |
| RN-06 | Número de protocolo é gerado imediatamente na submissão, antes da validação da operação |

### 4.2 Regras de SLA

| ID | Regra |
|----|-------|
| RN-07 | Cada base tem um SLA máximo de resposta configurado pelo gerente (padrão: 30 min) |
| RN-08 | Pedidos que ultrapassam 70% do SLA recebem alerta amarelo na fila da operação |
| RN-09 | Pedidos que ultrapassam 100% do SLA recebem alerta vermelho e notificação automática ao gerente |
| RN-10 | O SLA é contado apenas em horário operacional da base |

### 4.3 Regras de Confirmação

| ID | Regra |
|----|-------|
| RN-11 | A confirmação registrada pela operação congela os dados: qtd, horário, tipo de veículo e tarifa — esses dados não podem ser alterados após o aceite |
| RN-12 | O sistema não permite despacho sem aceite eletrônico registrado |
| RN-13 | Em caso de confirmação parcial (qtd menor que o solicitado), o sistema registra a diferença e notifica o solicitante |
| RN-14 | A operação pode propor alternativa (horário diferente, outro veículo); o solicitante pode aceitar ou reabrir novo pedido |

### 4.4 Regras de Aceite e Assinatura

| ID | Regra |
|----|-------|
| RN-15 | O aceite somente pode ocorrer após a confirmação operacional ser registrada |
| RN-16 | O aceite é realizado pelo solicitante autenticado + código OTP válido por 10 minutos |
| RN-17 | O sistema registra no aceite: ID do usuário, e-mail, IP, user-agent, timestamp UTC e hash SHA-256 do documento de confirmação |
| RN-18 | O documento de aceite é imutável após geração — nenhuma edição pode ser feita |
| RN-19 | Se o parceiro não assinar dentro de X horas (configurável), o sistema envia lembrete automático; após Y horas (configurável), alerta o gerente |
| RN-20 | Um pedido sem aceite não aparece na lista de faturamento |

### 4.5 Regras de Comprovante

| ID | Regra |
|----|-------|
| RN-21 | O comprovante PDF é gerado automaticamente após o encerramento da viagem |
| RN-22 | O PDF contém: número de protocolo, empresa, base, dados da confirmação, dados do aceite com hash, motorista/veículo alocado, horários reais e histórico completo de eventos |
| RN-23 | O comprovante inclui QR Code que permite verificar autenticidade acessando o sistema |
| RN-24 | Comprovantes são armazenados com hash SHA-256 e imutabilidade verificável |

### 4.6 Regras de Cancelamento e Recusa

| ID | Regra |
|----|-------|
| RN-25 | O parceiro pode cancelar pedidos nos status: enviado, em triagem ou confirmado (antes do aceite) |
| RN-26 | Após o aceite assinado, o cancelamento exige aprovação do supervisor da base |
| RN-27 | A operação pode recusar pedidos nos status: em triagem ou pendente de confirmação |
| RN-28 | Todo cancelamento ou recusa deve registrar motivo obrigatório |
| RN-29 | Pedidos cancelados e recusados são mantidos no histórico para fins de auditoria |

### 4.7 Regras de Faturamento

| ID | Regra |
|----|-------|
| RN-30 | Somente aparecem no relatório de faturamento pedidos com: aceite válido + status "Concluído" |
| RN-31 | Pedidos contestados são bloqueados para faturamento até resolução registrada |
| RN-32 | O financeiro acessa apenas leitura — não pode alterar status nem dados operacionais |

---

## 5. Backlog do MVP

O MVP foca exclusivamente nos fluxos essenciais: **solicitar, confirmar, assinar e comprovar**. Integrações avançadas são deixadas para fases posteriores.

### 5.1 Critérios de Prioridade

- **P0 — Bloqueante:** Sem isso o MVP não funciona
- **P1 — Essencial:** Funcionalidade central do produto, deve estar no MVP
- **P2 — Importante:** Agrega valor significativo, entra na segunda semana de sprint
- **P3 — Desejável:** Não bloqueia o lançamento, entra nos ciclos seguintes

---

### 5.2 Épico 1 — Autenticação e Gestão de Usuários

| ID | História | Prioridade | Critério de Aceite |
|----|----------|------------|--------------------|
| US-001 | Como solicitante, quero fazer login com e-mail e senha para acessar o portal | P0 | Login funcional, sessão expirada em 8h, bloqueio após 5 tentativas |
| US-002 | Como gerente, quero cadastrar empresas parceiras e suas bases autorizadas | P0 | CRUD completo de empresas e bases |
| US-003 | Como gerente, quero cadastrar usuários vinculados a uma empresa com perfil definido | P0 | Convite por e-mail, ativação de conta, perfis: Solicitante, Aprovador, Gestor Parceiro |
| US-004 | Como usuário, quero redefinir minha senha via e-mail | P1 | Token de redefinição expira em 1h |
| US-005 | Como gerente, quero desativar um usuário sem excluir seu histórico | P1 | Usuário desativado não faz login, pedidos anteriores preservados |

### 5.3 Épico 2 — Solicitação de Viagem Extra

| ID | História | Prioridade | Critério de Aceite |
|----|----------|------------|--------------------|
| US-010 | Como solicitante, quero abrir um novo pedido com todos os campos obrigatórios | P0 | Formulário validado, protocolo gerado, pedido na fila da operação |
| US-011 | Como solicitante, quero selecionar apenas as bases autorizadas pelo meu contrato | P0 | Dropdown filtrado por contrato da empresa |
| US-012 | Como sistema, quero validar antecedência mínima ao submeter um pedido | P1 | Bloqueio ou aviso configurável por base |
| US-013 | Como solicitante, quero salvar um pedido como rascunho e enviá-lo depois | P2 | Rascunho salvo, não aparece na fila da operação |
| US-014 | Como solicitante, quero visualizar o histórico de todos os meus pedidos com filtros | P1 | Filtros: status, base, tipo, período. Paginação. |
| US-015 | Como solicitante, quero receber uma notificação por WhatsApp quando o pedido for confirmado | P1 | Mensagem via API do WhatsApp com link direto para aceite |

### 5.4 Épico 3 — Triagem e Confirmação Operacional

| ID | História | Prioridade | Critério de Aceite |
|----|----------|------------|--------------------|
| US-020 | Como supervisor, quero ver a fila de pedidos da minha base ordenada por SLA | P0 | Fila com indicador de SLA em tempo real (verde/amarelo/vermelho) |
| US-021 | Como supervisor, quero confirmar um pedido registrando qtd, horário, veículo e tarifa | P0 | Dados congelados após confirmação; parceiro notificado |
| US-022 | Como supervisor, quero recusar um pedido com motivo obrigatório | P1 | Status atualizado, motivo registrado, parceiro notificado |
| US-023 | Como supervisor, quero propor alternativa ao parceiro quando não tiver disponibilidade plena | P2 | Proposta enviada ao parceiro, que pode aceitar ou recusar |
| US-024 | Como gerente, quero receber alerta quando um pedido ultrapassar o SLA configurado | P1 | Notificação por e-mail + destaque vermelho no painel gerencial |
| US-025 | Como supervisor, quero registrar observações internas que não são visíveis ao parceiro | P2 | Campo de observação com flag "interno" |

### 5.5 Épico 4 — Aceite Eletrônico e Assinatura

| ID | História | Prioridade | Critério de Aceite |
|----|----------|------------|--------------------|
| US-030 | Como solicitante, quero visualizar o resumo congelado da confirmação antes de assinar | P0 | Tela com todos os dados confirmados pela operação, somente leitura |
| US-031 | Como solicitante, quero receber um código OTP por WhatsApp ou e-mail para confirmar o aceite | P0 | OTP gerado, enviado e validado; expira em 10 min |
| US-032 | Como sistema, quero registrar IP, user-agent, timestamp e hash do documento no aceite | P0 | Todos os campos de evidência armazenados imutavelmente |
| US-033 | Como solicitante, quero receber cópia do aceite assinado por e-mail imediatamente | P1 | E-mail com PDF anexado enviado ao assinante após confirmação |
| US-034 | Como sistema, quero enviar lembrete automático ao parceiro quando o aceite estiver pendente por mais de X horas | P1 | Configuração do tempo de lembrete por empresa ou globalmente |
| US-035 | Como gerente, quero ser alertado quando um aceite ultrapassar o prazo máximo sem resposta | P1 | Alerta por e-mail com link para o pedido |

### 5.6 Épico 5 — Despacho e Execução

| ID | História | Prioridade | Critério de Aceite |
|----|----------|------------|--------------------|
| US-040 | Como supervisor, quero alocar motorista e veículo a um pedido com aceite assinado | P1 | Campo de motorista e placa; pedido muda para "Em execução" |
| US-041 | Como supervisor, quero registrar o horário real de saída e chegada da viagem | P1 | Campos de horário real; diferença calculada automaticamente |
| US-042 | Como supervisor, quero encerrar uma viagem, gerando o comprovante automaticamente | P0 | Botão "Concluir viagem"; comprovante gerado em PDF |
| US-043 | Como supervisor, quero registrar ocorrências durante a execução da viagem | P2 | Campo de ocorrência vinculado ao protocolo, com timestamp |

### 5.7 Épico 6 — Comprovante e Auditoria

| ID | História | Prioridade | Critério de Aceite |
|----|----------|------------|--------------------|
| US-050 | Como sistema, quero gerar PDF imutável com histórico completo, dados de aceite e hash | P0 | PDF gerado automaticamente; conteúdo não editável após geração |
| US-051 | Como solicitante, quero baixar o comprovante PDF da viagem pelo portal | P0 | Botão de download disponível após conclusão da viagem |
| US-052 | Como financeiro, quero acessar a lista de viagens faturáveis com filtros | P1 | Lista filtrada por empresa, base, período; apenas pedidos com aceite + concluídos |
| US-053 | Como financeiro, quero exportar o relatório de faturamento em CSV | P1 | CSV com todos os campos relevantes para cobrança |
| US-054 | Como gerente, quero visualizar a trilha de auditoria completa de qualquer pedido | P2 | Timeline de todos os eventos com usuário e timestamp |
| US-055 | Como sistema, quero disponibilizar QR Code no comprovante para verificação de autenticidade | P2 | QR Code aponta para URL de verificação do documento no sistema |

### 5.8 Épico 7 — Painel Gerencial e Indicadores

| ID | História | Prioridade | Critério de Aceite |
|----|----------|------------|--------------------|
| US-060 | Como gerente, quero ver KPIs consolidados de todas as bases em uma única tela | P1 | Pedidos abertos, em SLA crítico, aceites pendentes, em execução |
| US-061 | Como gerente, quero ver o volume de pedidos por base, empresa e período | P2 | Gráfico de barras + tabela exportável |
| US-062 | Como supervisor, quero ver indicadores da minha base (taxa de atendimento, SLA médio) | P2 | Painel da base com os últimos 30 dias |
| US-063 | Como gerente, quero configurar SLA, antecedência mínima e contratos por base | P1 | Tela de configurações com CRUD de regras por base |

---

## 6. Arquitetura do Sistema

### 6.1 Componentes Principais

| Componente | Responsabilidade |
|------------|-----------------|
| **Front-end Web (Parceiro)** | Portal de solicitação, aceite, acompanhamento e comprovantes — responsivo, mobile-first |
| **Front-end Web (Operação)** | Fila de triagem, confirmação, despacho e encerramento de viagens |
| **API Principal** | Regras de negócio, workflow de pedidos, controle de permissões e SLA |
| **Serviço de Autenticação** | Login, OTP, gestão de sessões e perfis |
| **Serviço de Notificações** | Envio por WhatsApp (API oficial) e e-mail — templates por evento |
| **Serviço de Documentos** | Geração de PDF, hash SHA-256, armazenamento e QR Code de verificação |
| **Banco Relacional** | Dados transacionais: pedidos, confirmações, aceites, usuários, contratos |
| **Repositório de Arquivos** | Armazenamento de PDFs, comprovantes e anexos |
| **Event Log (Auditoria)** | Registro append-only de todos os eventos do sistema — imutável por design |

### 6.2 Entidades de Dados

| Entidade | Campos principais |
|----------|------------------|
| Empresa Parceira | ID, nome, CNPJ, bases autorizadas, contratos, usuários |
| Base | ID, nome, localidade, SLA configurado, antecedência mínima, supervisores |
| Usuário | ID, nome, e-mail, empresa/base, perfil, status |
| Pedido | Protocolo, tipo, empresa, base, data/hora solicitada, campos operacionais, status, timestamps |
| Confirmação Operacional | Pedido ID, qtd aprovada, horário confirmado, tipo veículo, tarifa, observações, supervisor, timestamp |
| Aceite | Pedido ID, usuário ID, IP, user-agent, OTP validado, timestamp UTC, hash do documento |
| Alocação | Pedido ID, motorista, veículo, horário despacho, horário saída real, horário chegada real |
| Comprovante | Pedido ID, PDF hash, URL, data geração, QR Code |
| Event Log | ID, pedido ID, tipo evento, usuário, timestamp, payload |

### 6.3 Integrações Fase 1 (MVP)

- **API do WhatsApp Business** (Cloud API oficial): notificações de status e envio de OTP
- **Serviço de e-mail transacional** (SendGrid, SES ou similar): notificações e cópias de aceite

### 6.4 Integrações Fases Seguintes

- **Sistema de Despacho Logtudo** (fase 3): consome confirmação para alocação automática de motoristas
- **App de Motoristas** (fase 3): recebimento de tarefa e confirmação de execução
- **Portal do Cliente Logtudo** (projeto 4): acesso unificado do parceiro a pedidos e comprovantes
- **Sistema de Faturamento** (fase 4): exportação automática de pedidos faturáveis
- **BI / Analytics Logtudo** (fase 4): indicadores consolidados multi-base

---

## 7. Plano de Implementação

### Fase 1 — MVP Operacional *(prioridade máxima)*

**Objetivo:** Colocar em produção o fluxo essencial — pedido formal, confirmação formal, assinatura e comprovante.

**Funcionalidades incluídas:** Épicos 1 a 6 com histórias P0 e P1

**Resultado esperado:** Substituição imediata do WhatsApp como canal oficial de pedidos extras para as primeiras empresas parceiras piloto.

---

### Fase 2 — Comunicação e Notificações Aprimoradas

- Notificações por WhatsApp com links de ação direta (aceitar, ver pedido)
- Lembretes automáticos configuráveis por evento
- Alertas de SLA para gerentes

---

### Fase 3 — Integração Operacional

- Vínculo com sistema de despacho e cadastro de motoristas
- App ou notificação para motoristas receberem a tarefa
- Registro de execução em campo
- Tracking da viagem vinculado ao protocolo

---

### Fase 4 — Integração Corporativa

- Exportação automática para faturamento
- Dashboard de BI com indicadores multi-base
- Workflow de contestação com prazo e resolução registrados
- Portal do Cliente unificado (projeto 4) com acesso a pedidos e comprovantes

---

## 8. Considerações Técnicas e de Segurança

- **Autenticação:** JWT com refresh token + OTP para operações críticas (aceite)
- **Auditoria:** Event log append-only — nenhum evento pode ser alterado ou excluído
- **Imutabilidade de documentos:** Hash SHA-256 calculado no momento da geração; qualquer alteração invalida o hash
- **Proteção de dados:** Dados do parceiro isolados por tenant; a operação de uma base não acessa dados de outra
- **LGPD:** Dados pessoais (usuários, solicitantes) com policy de retenção definida; consentimento registrado no cadastro
- **Disponibilidade:** SLA de 99,5% em horário operacional; alertas de downtime para o gerente

---

## 9. Critérios de Sucesso do MVP

| Métrica | Meta |
|---------|------|
| Tempo médio para abertura de pedido | < 3 minutos pelo solicitante |
| Tempo médio de triagem pela operação | Dentro do SLA configurado |
| Taxa de adoção nas primeiras bases piloto | ≥ 80% dos pedidos extras via sistema (não WhatsApp) |
| Taxa de aceites coletados | 100% dos pedidos confirmados devem ter aceite antes de executar |
| Comprovantes gerados automaticamente | 100% das viagens concluídas |
| Zero pedidos faturados sem aceite registrado | Regra de negócio bloqueante (RN-20) |

---

*Documento preparado por: Equipe Logtudo · Projeto 6 — Central de Solicitação de Viagens Extras · Versão 1.0 · Maio 2026*
