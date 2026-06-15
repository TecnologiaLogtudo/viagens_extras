# Sistema de Seeds - Persistência de Dados

## 📋 Visão Geral

O sistema de seeds garante que dados críticos (como a importação de motoristas) persistam através de:
- ✅ Alterações no schema do banco de dados
- ✅ Migrations de banco de dados
- ✅ Recriações de tabelas
- ✅ Execuções repetidas da aplicação

## 🌱 Como Funciona

### Rastreamento de Seeds
Cada seed é rastreado na tabela `seed_version` com:
- Nome único do seed
- Versão
- Data e hora de aplicação
- Checksum (para detectar mudanças)

Isso garante que um seed **nunca será reimportado** mais de uma vez.

### Fluxo de Inicialização

```
app/main.py (startup)
    ↓
init_db_with_seeds()
    ↓
init_db() - Cria schema e altera tabelas se necessário
    ↓
apply_all_seeds() - Aplica seeds idempotentemente
    ↓
seed_fleet_import() - Importa motoristas do Excel (se não aplicado)
```

## 📁 Arquivos do Sistema

```
app/seeds/
├── __init__.py              # Orquestrador de seeds
├── seed_tracker.py          # Rastreador de versão de seeds
└── seed_fleet.py            # Seed de importação de motoristas
```

## 🚀 Scripts Disponíveis

### Importar Motoristas
```bash
python migrate_motoristas.py
```
- Executa `init_db_with_seeds()`
- Cria o banco se não existir
- Importa motoristas do `cadastro_veiculos_tratado.xlsx` (idempotente)

### Ver Histórico de Seeds
```bash
python manage_seeds.py show
```
Exemplo de saída:
```
📋 Histórico de Seeds Aplicados:
─────────────────────────────────
  • 001_import_fleet (v1)
    Aplicado em: 2026-06-01T10:30:45.123456
─────────────────────────────────
Total: 1 seeds
```

### Resetar um Seed Específico
```bash
python manage_seeds.py reset 001_import_fleet
```
- Remove o seed do rastreamento
- Na próxima execução, será re-aplicado

### Resetar Todos os Seeds
```bash
python manage_seeds.py reset-all
```
- Remove todos os seeds do rastreamento
- Todos serão re-aplicados na próxima execução

## 🔄 Integrações Automáticas

### FastAPI App (main.py)
Na inicialização, a aplicação chama automaticamente:
```python
@app.on_event("startup")
def on_startup():
    init_db_with_seeds()  # Garante dados persistidos
    # ... resto do setup
```

### Testes
Para testes, use `init_db_with_seeds()` antes de executar:
```python
from app.db import init_db_with_seeds

def test_something():
    init_db_with_seeds()
    # seu teste aqui
```

## ➕ Criar Novo Seed

1. Crie um arquivo em `app/seeds/seed_xxxx.py`:
```python
from sqlmodel import Session
from .seed_tracker import is_seed_applied, mark_seed_applied

SEED_NAME = "002_seu_seed"
SEED_VERSION = 1

def seed_seu_seed(session: Session, connection) -> bool:
    if is_seed_applied(connection, SEED_NAME, SEED_VERSION):
        print(f"⏭️  Seed {SEED_NAME} já foi aplicado, pulando...")
        return False
    
    print(f"🌱 Aplicando seed {SEED_NAME}...")
    
    # Seu código aqui
    
    mark_seed_applied(connection, SEED_NAME, SEED_VERSION)
    print(f"✅ Seed {SEED_NAME} aplicado!")
    return True
```

2. Registre no `app/seeds/__init__.py`:
```python
from .seed_seu_seed import seed_seu_seed

def apply_all_seeds(session: Session, connection: Connection) -> None:
    print("\n🌾 Iniciando processo de seeds...")
    init_seed_tracker(connection)
    
    seed_seu_seed(session, connection)  # ← Adicione aqui
    
    print("🌾 Processo de seeds finalizado!\n")
```

## ✅ Status Atual

- ✅ `001_import_fleet` - Importação de motoristas e veículos do Excel
- ⏳ Próximos: Adicione seeds conforme necessário

## 📊 Verificar Dados Importados

```python
from sqlmodel import Session, select
from app.db import engine
from app.models import Driver, Vehicle

with Session(engine) as session:
    drivers = session.exec(select(Driver)).all()
    vehicles = session.exec(select(Vehicle)).all()
    print(f"Motoristas: {len(drivers)}")
    print(f"Veículos: {len(vehicles)}")
```

## ⚠️ Notas Importantes

1. **Idempotência**: Sempre implemente seeds que possam rodar múltiplas vezes sem danificar dados
2. **Versioning**: Incremente a versão ao mudar um seed
3. **Rastreamento**: O sistema rastreia por `(seed_name, version)`, não apenas pelo nome
4. **Performance**: Seeds idempotentes não causam overhead na inicialização
5. **Backup**: Mesmo com seeds, considere fazer backup antes de grandes mudanças
