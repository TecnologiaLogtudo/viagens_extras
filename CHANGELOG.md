# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2026-06-15

### Changed
- **Restrição de Cancelamento:**
  - A ação de cancelamento de solicitações de viagem agora é exclusiva dos usuários da Logtudo (Supervisores e Gerentes), sendo completamente desabilitada para Parceiros.
  - Criado o endpoint POST `/empresa/requests/{request_id}/cancel` para execução do cancelamento pela equipe Logtudo, e adicionado o botão "Cancelar" na tabela de solicitações operacionais (`_operations_requests.html`).
- **Fluxo Dedicado de Cotação de Preço:**
  - Implementado fluxo específico para o tipo de viagem `"Cotação de preço"`.
  - Na Triagem: a tarifa passa a ser obrigatória (maior que R$ 0,00). O status da viagem é atualizado para `CONFIRMED` ("Aprovação pendente de aceite") e a emissão do comprovante PDF é postergada.
  - No Portal do Parceiro: se uma cotação estiver pendente de aceite (status `CONFIRMED`), são disponibilizados os botões "Aceitar" e "Declinar".
  - Ao clicar em "Aceitar", o status evolui para `COMPLETED`, gerando e liberando o comprovante PDF para download. Ao clicar em "Declinar", o status evolui para `REFUSED`.
  - Para outros tipos de viagem, o campo de tarifa não é exigido na triagem, avançando o pedido diretamente para `COMPLETED` com geração imediata do comprovante PDF.
- **Antecedência Mínima Configurável por Parceiro:**
  - Remoção completa de todas as restrições e travas de horário globais baseadas nas bases operacionais (`base.min_advance_minutes`). O parceiro agora pode solicitar viagens para qualquer momento do futuro, e o supervisor pode responder às solicitações a qualquer momento.
  - Implementada a configuração de antecedência mínima especificamente no cadastro individual de cada Parceiro (campo `min_advance_minutes` no modelo `User`).
  - Adicionado campo "Antecedência Mínima (minutos)" na interface do Gerente para as ações de cadastro e edição de Parceiros.
- **Lista Suspensa para Tipo de Viagem:**
  - O campo de texto livre para Tipo de Viagem no formulário do Parceiro (no portal do parceiro) foi substituído por uma lista suspensa (select) com as opções: "Viagem extra NILO", "Carro dedicado", "Carro extra rota D2D" e "Cotação de preço".
- **Ajustes de Rótulos e Mapeamentos no Portal do Parceiro:**
  - O rótulo "Data/Hora" foi alterado para "Data/Hora do carregamento".
  - O rótulo e campo "Motivo" (que enviava `reason`) foram substituídos por "Centro de Custo" (enviando `cost_center`).
  - O rótulo "Notas" foi alterado para "Observações do carregamento".
- **Simplificação do Fluxo de Viagens Extras:**
  - Remoção completa da etapa de validação via código OTP pelo Parceiro.
  - Remoção completa da etapa de Despacho (dispatch) e Conclusão (complete) manual do Supervisor.
  - Triagem pelo Supervisor agora é a última etapa activa do fluxo operacional. Ao confirmar uma viagem na triagem, ela é movida diretamente para o status final `COMPLETED`.
  - Edição de solicitações confirmadas (triadas) agora é permitida diretamente no status `COMPLETED`.
- **Disponibilização do Comprovante (PDF):**
  - O comprovante PDF da viagem (voucher) agora é gerado automaticamente e imediatamente após a confirmação/triagem do Supervisor (com exceção de cotações, onde aguarda o aceite do parceiro).
  - O comprovante PDF agora exibe o método de aprovação como "Confirmação do Supervisor", tendo a assinatura preenchida com o nome do Supervisor responsável e a data em que ocorreu.
  - O botão de download do "Comprovante" foi disponibilizado em todos os perfis (Parceiro, Supervisor, Gerente, Financeiro) assim que a viagem é confirmada (com exceção de cotações pendentes de aceite).
  - Adicionada a coluna de "Comprovante" na tabela do módulo **Financeiro** (`/empresa/financeiro`) do Gerente e Financeiro para acesso direto ao PDF da viagem.

### Removed
- Formulário e botões de envio/validação de código OTP no portal do Parceiro.
- Formulários e botões de "Despachar" e "Finalizar" no portal do Supervisor e nas rotas da API.
- Requisito de registro na tabela `Acceptance` (gerado na assinatura via OTP) para qualificar uma viagem no módulo financeiro.
- Templates HTML obsoletos/legados não referenciados na aplicação (`dashboard.html` e `finance.html`).

## [1.1.0] - 2026-06-15

### Added
- **Banco de Dados PostgreSQL via Docker:**
  - Criação do arquivo `docker-compose.yml` para rodar o PostgreSQL 15-alpine em container.
  - Adicionada a dependência `psycopg2-binary` para conexão nativa ao PostgreSQL.
  - Adicionado suporte a `DATABASE_URL` no arquivo `.env` para conexão com o banco de dados PostgreSQL.
- **Processo de Carga Manual de Dados (Sementes):**
  - Criação do script utilitário `seed_all.py` para executar de forma independente a criação física das tabelas e a importação manual das sementes (motoristas, bases e usuários padrão).

### Changed
- **Configuração do Banco de Dados:**
  - Ajuste na inicialização do FastAPI no `app/main.py` para apenas garantir a estrutura física do banco (`init_db()`), removendo a carga automática de seeds na inicialização do servidor.
  - Ajuste em `app/db.py` para que a engine use PostgreSQL se `DATABASE_URL` estiver presente no ambiente, e SQLite (`mvp.db`) como fallback se ausente.
  - Isoladas as consultas `PRAGMA` específicas de migração SQLite para executar apenas quando o dialeto do banco for SQLite.
  - Modificado o arquivo `app/seeds/seed_tracker.py` para ser agnóstico quanto à sintaxe SQL (SERIAL, ON CONFLICT) entre SQLite e PostgreSQL.

