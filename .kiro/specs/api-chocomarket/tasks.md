# Implementation Plan: API ChocoMarket

## Overview

Este plano converte `requirements.md`, `design.md`, `openapi.yaml` e a implementação FastAPI existente em uma sequência incremental de tarefas de código. A implementação será feita em Python sobre FastAPI, SQLAlchemy, SQLite e Pydantic, preservando o cadastro atual de clientes em `POST /v1/users`: normalização de e-mail, hash Argon2, criação como `customer` ativo, rejeição de campos extras, conflito de e-mail duplicado e resposta sem credenciais.

As tarefas alteram somente os módulos necessários da aplicação e seus testes. Cada tarefa de teste marcada com `*` é opcional para um MVP, mas cobre comportamento definido nos requisitos e deve ser executada para concluir a implementação integral. O código não deve introduzir endpoints fora de `openapi.yaml`.

## Tasks

- [x] 1. Preparar fundação persistente e infraestrutura transversal
  - [x] 1.1 Consolidar configuração de banco, sessões e metadados SQLAlchemy
    - Atualizar `app/database.py` para habilitar foreign keys no SQLite, isolamento transacional apropriado e sessões por requisição/teste.
    - Preservar a compatibilidade do `Base`, `engine`, `SessionLocal` e `get_db` usados pelo cadastro atual.
    - Definir a separação entre banco de desenvolvimento e banco temporário dos testes sem depender de `chocomarket.db` compartilhado.
    - **Escopo:** `app/database.py`, fixtures comuns de teste quando necessário.
    - **Dependências:** nenhuma.
    - _Requisitos: 4.6, 4.7, 6.1, 6.2, 8.6_

  - [x] 1.2 Criar modelos comuns, timestamps, enums e convenções de UUID
    - Implementar tipos/modelos comuns para IDs UUID, datas UTC, status e serialização consistente.
    - Garantir que os modelos persistam UUIDs compatíveis com SQLite e os schemas exponham UUIDs conforme o contrato.
    - **Escopo:** `app/common/models.py`, `app/common/types.py` e módulos de modelos relacionados.
    - **Dependências:** 1.1.
    - _Requisitos: 8.1, 8.2, 8.3, 8.4_

  - [x] 1.3 Implementar modelos SQLAlchemy de produto, pedido, item e idempotência
    - Criar `ProductModel`, `OrderModel`, `OrderItemModel` e `IdempotencyRecordModel` com foreign keys, índices, enums, limites e restrições únicas do design.
    - Preservar `UserModel` existente e preparar relacionamentos sem quebrar a tabela atual.
    - **Escopo:** `app/products/model.py`, `app/orders/model.py`, `app/core/idempotency.py` ou módulo de modelo equivalente, ajustes compatíveis em `app/users/model.py`.
    - **Dependências:** 1.1, 1.2.
    - _Requisitos: 2.8, 3.5, 3.6, 4.3, 4.7, 4.8, 4.9, 6.1, 6.2_

  - [x] 1.4 Configurar migrations/schema inicial sem remover dados válidos existentes
    - Criar a estrutura Alembic e migrations para as tabelas, índices e restrições novas.
    - Permitir a evolução da tabela de usuários existente e manter o cadastro atual funcionando em banco vazio e banco já criado.
    - **Escopo:** `alembic/`, `alembic.ini` e configuração de migration; não alterar `openapi.yaml`.
    - **Dependências:** 1.3.
    - _Requisitos: 2.8, 4.3, 4.8, 8.6_

  - [x]* 1.5 Testar isolamento de banco, foreign keys, índices e restrições únicas
    - Criar fixtures para banco temporário e verificar as restrições `users.email`, `idempotency_records(scope, key)` e `order_items(order_id, product_id)`.
    - Confirmar que uma sessão de teste não vaza dados para outra e que o cadastro existente continua usando persistência.
    - **Escopo:** `tests/conftest.py`, `tests/integration/test_database.py` ou módulos equivalentes.
    - **Dependências:** 1.4.
    - _Requisitos: 2.8, 4.4, 4.8, 4.9, 8.6_

- [x] 2. Implementar erros, request ID, autenticação e autorização
  - [x] 2.1 Criar tipos de erro de domínio e envelopes `Problem_Response`
    - Implementar exceções para `401`, `403`, `404`, `409`, `422` e `500`, incluindo `code`, `detail`, `request_id` e erros por campo quando aplicável.
    - **Escopo:** `app/core/errors.py`, `app/common/responses.py` e schemas comuns.
    - **Dependências:** 1.2.
    - _Requisitos: 1.1, 1.2, 1.3, 2.8, 3.12, 4.4, 4.5, 4.9, 4.10, 5.7, 5.9, 6.4, 6.5, 6.6, 7.4, 8.6, 8.7, 8.9_

  - [x] 2.2 Adicionar middleware de request ID e handlers globais de erro
    - Gerar ou propagar com segurança um `request_id`, registrar falhas sem token, senha, hash ou SQL e converter erros FastAPI/Pydantic/SQLAlchemy para `application/problem+json`.
    - Garantir rollback e resposta genérica em falhas inesperadas.
    - **Escopo:** `app/main.py`, `app/core/errors.py`, middleware e logging.
    - **Dependências:** 2.1.
    - _Requisitos: 8.6, 8.7_

  - [x] 2.3 Completar configuração e validação de JWT Bearer
    - Implementar validação de assinatura, algoritmo permitido, emissor, audiência, expiração e claim `sub` usando configuração de ambiente.
    - Manter Argon2 em `app/core/security.py` e não armazenar segredos no código ou no banco.
    - **Escopo:** `app/core/security.py`, `app/core/auth.py`, configuração de ambiente e dependências.
    - **Dependências:** 1.2, 2.1.
    - _Requisitos: 1.1, 1.3, 8.5_

  - [x] 2.4 Criar dependências de usuário ativo, administrador e autenticação opcional
    - Implementar `get_current_user`, `require_active_user`, `require_admin` e a dependência opcional para consultas públicas de produtos.
    - Consultar sempre o papel/status atual no banco; usuário inativo deve receber `401`, papel insuficiente `403`.
    - **Escopo:** `app/core/auth.py`, integração com `app/users/repository.py`.
    - **Dependências:** 1.3, 2.2, 2.3.
    - _Requisitos: 1.1, 1.2, 1.3, 1.4_

  - [x]* 2.5 Testar autenticação, autorização, configuração segura e envelopes de erro
    - Cobrir token ausente, malformado, expirado, alterado, algoritmo não permitido, emissor/audiência inválidos, usuário inativo, customer sem permissão e admin autorizado.
    - Verificar `401/403`, `WWW-Authenticate` quando aplicável, media type, campos obrigatórios e ausência de dados sensíveis.
    - **Escopo:** `tests/unit/test_auth.py`, `tests/integration/test_errors_auth.py`.
    - **Dependências:** 2.4.
    - _Requisitos: 1.1, 1.2, 1.3, 1.4, 8.5, 8.7_

  - [x]* 2.6 Property 1: credenciais inválidas interrompem rotas protegidas
    - Criar o teste Hypothesis com no mínimo 100 exemplos para confirmar `401`, `Problem_Response` e ausência de execução do handler.
    - **Escopo:** `tests/property/test_auth_properties.py`.
    - **Dependências:** 2.4.
    - **Propriedade:** Property 1 do design.
    - _Requisitos: 1.1_

  - [x]* 2.7 Property 2: usuário inativo não acessa recursos protegidos
    - Criar o teste Hypothesis com no mínimo 100 exemplos para confirmar rejeição antes de qualquer mutação.
    - **Escopo:** `tests/property/test_auth_properties.py`.
    - **Dependências:** 2.4.
    - **Propriedade:** Property 2 do design.
    - _Requisitos: 1.3_

  - [x]* 2.8 Property 3: papel insuficiente não produz efeito
    - Criar o teste Hypothesis com no mínimo 100 exemplos para confirmar `403` e preservação do estado persistido.
    - **Escopo:** `tests/property/test_auth_properties.py`.
    - **Dependências:** 2.4.
    - **Propriedade:** Property 3 do design.
    - _Requisitos: 1.2_

- [x] 3. Concluir módulo de usuários sem quebrar o cadastro existente
  - [x] 3.1 Criar schemas, repository e projeções seguras de usuários
    - Implementar schemas de entrada/saída com `extra="forbid"`, validações do OpenAPI, patch com distinção entre ausente e presente e resposta sem senha/hash.
    - Substituir o uso funcional de `users_by_email` por repository persistido e restrição única, mantendo compatibilidade temporária apenas onde os testes existentes exigirem.
    - **Escopo:** `app/users/schemas.py`, `app/users/repository.py`, `app/users/model.py`, `app/users/service.py`.
    - **Dependências:** 1.2, 1.3, 2.1.
    - _Requisitos: 2.3, 2.5, 2.6, 2.7, 2.8, 8.1, 8.2, 8.3, 8.5, 8.6_

  - [x] 3.2 Preservar e adaptar POST /v1/users com idempotência opcional
    - Manter `201`, role `customer`, status `active`, normalização de e-mail, hash Argon2, rejeição de `role/status/password_hash` e conflito de e-mail.
    - Integrar `Idempotency-Service` quando houver `Idempotency-Key`, incluindo replay igual e `409` para fingerprint diferente.
    - Emitir `Location` conforme o contrato e não persistir a senha em texto puro.
    - **Escopo:** `app/users/service.py`, `app/users/router.py`, `app/core/idempotency.py`.
    - **Dependências:** 1.3, 2.1, 3.1.
    - _Requisitos: 8.8, 8.9 e comportamento existente coberto por `tests/test_users.py`_

  - [x] 3.3 Implementar listagem administrativa de usuários com filtros e cursor
    - Adicionar `GET /v1/users`, default `status=active`, filtros combináveis por nome, e-mail, papel e status, e paginação por cursor.
    - **Escopo:** `app/users/repository.py`, `app/users/service.py`, `app/users/router.py`.
    - **Dependências:** 2.4, 3.1, 7.1.
    - _Requisitos: 2.1, 2.2, 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 3.4 Implementar consulta, patch, desativação e troca de senha de usuários
    - Adicionar `GET/PATCH/DELETE /v1/users/{userId}` e `PATCH /v1/users/{userId}/password` com autorização contextual, conflitos de e-mail, atualização parcial, exclusão lógica idempotente e verificação da senha atual.
    - Retornar `404` para alvo inexistente/invisível, `403` quando o alvo estiver disponível mas a operação não for permitida, `401` para senha atual incorreta e `204` sem corpo nos sucessos previstos.
    - **Escopo:** `app/users/service.py`, `app/users/router.py`, `app/users/repository.py`, `app/core/security.py`.
    - **Dependências:** 2.4, 3.1, 3.3.
    - _Requisitos: 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14_

  - [x]* 3.5 Testar integração HTTP completa do módulo de usuários
    - Atualizar `tests/test_users.py` para usar banco de teste e cobrir cadastro existente, `Location`, idempotência, listagem, filtros, visibilidade customer/admin, patch, conflito, exclusão lógica, senha e códigos de erro.
    - Remover a dependência dos testes em `users_by_email` como fonte de verdade.
    - **Escopo:** `tests/test_users.py`, `tests/integration/test_users.py`, fixtures.
    - **Dependências:** 3.2, 3.3, 3.4.
    - _Requisitos: 2.1–2.14, 8.8, 8.9_

  - [x]* 3.6 Property 4: listagem de usuários respeita visão e filtros
    - Criar teste Hypothesis com no mínimo 100 exemplos comparando resultado da API/serviço com um resultado de referência e metadados válidos.
    - **Escopo:** `tests/property/test_user_properties.py`.
    - **Dependências:** 3.3.
    - **Propriedade:** Property 4 do design.
    - _Requisitos: 2.1, 2.2_

  - [x]* 3.7 Property 5: perfil respeita proprietário, status e projeção
    - Criar teste Hypothesis com no mínimo 100 exemplos para visibilidade customer/admin, `200/404` e ausência de credenciais.
    - **Escopo:** `tests/property/test_user_properties.py`.
    - **Dependências:** 3.4.
    - **Propriedade:** Property 5 do design.
    - _Requisitos: 2.3, 2.4, 2.5, 8.5_

  - [x]* 3.8 Property 6: patch de usuário altera somente campos permitidos e presentes
    - Criar teste Hypothesis com no mínimo 100 exemplos para campos presentes, ausentes, role/status e projeção segura.
    - **Escopo:** `tests/property/test_user_properties.py`.
    - **Dependências:** 3.4.
    - **Propriedade:** Property 6 do design.
    - _Requisitos: 2.6, 2.7_

  - [x]* 3.9 Property 7: e-mail normalizado permanece único
    - Criar teste Hypothesis com no mínimo 100 exemplos para colisões case-insensitive, `409` e preservação integral do registro.
    - **Escopo:** `tests/property/test_user_properties.py`.
    - **Dependências:** 3.4.
    - **Propriedade:** Property 7 do design.
    - _Requisitos: 2.8_

  - [x]* 3.10 Property 8: exclusão lógica é idempotente
    - Criar teste Hypothesis com no mínimo 100 exemplos para usuários e produtos, repetição da operação, status `inactive`, preservação dos demais campos e resposta sem corpo.
    - **Escopo:** `tests/property/test_lifecycle_properties.py`.
    - **Dependências:** 3.4, 4.4.
    - **Propriedade:** Property 8 do design.
    - _Requisitos: 2.9, 2.10, 3.10, 3.11_

- [x] 4. Implementar catálogo, estoque e estados de produtos
  - [x] 4.1 Criar schemas, repository e regras de transição de produto
    - Implementar schemas com limites do OpenAPI, patch não vazio e função pura de precedência entre `inactive`, `out_of_stock` e `active`.
    - Aplicar status derivado na criação e atualização, inclusive reativação por estoque positivo e desativação explícita.
    - **Escopo:** `app/products/schemas.py`, `app/products/repository.py`, `app/products/service.py`, `app/products/model.py`.
    - **Dependências:** 1.2, 1.3, 2.1.
    - _Requisitos: 3.5, 3.6, 3.7, 3.8, 3.9, 3.12_

  - [x] 4.2 Implementar listagem e consulta pública/admin de produtos
    - Adicionar `GET /v1/products` e `GET /v1/products/{productId}` com autenticação opcional, visibilidade pública de ativos, filtros combináveis e consulta administrativa de qualquer status.
    - **Escopo:** `app/products/router.py`, `app/products/service.py`, `app/products/repository.py`.
    - **Dependências:** 2.4, 4.1, 7.1.
    - _Requisitos: 1.4, 3.1, 3.2, 3.3, 3.4, 7.1–7.5_

  - [x] 4.3 Implementar criação, patch e exclusão lógica de produtos
    - Adicionar `POST /v1/products`, `PATCH /v1/products/{productId}` e `DELETE /v1/products/{productId}` somente para admin.
    - Integrar `Idempotency-Key` opcional na criação, retornar `Location`, preservar campos ausentes e tratar `404`, `422` e exclusão repetida.
    - **Escopo:** `app/products/router.py`, `app/products/service.py`, `app/core/idempotency.py`.
    - **Dependências:** 2.4, 4.1.
    - _Requisitos: 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13_

  - [x]* 4.4 Testar catálogo, filtros, estoque e autorização
    - Cobrir consulta sem token, customer/admin, estados `active/inactive/out_of_stock`, limites, criação, patch, reativação, exclusão lógica, idempotência e produtos inexistentes.
    - **Escopo:** `tests/integration/test_products.py`, `tests/unit/test_product_status.py`.
    - **Dependências:** 4.2, 4.3.
    - _Requisitos: 1.4, 3.1–3.13_

  - [x]* 4.5 Property 9: catálogo respeita visibilidade e filtros
    - Criar teste Hypothesis com no mínimo 100 exemplos para comparar catálogo público/customer/admin com a interseção dos filtros permitidos.
    - **Escopo:** `tests/property/test_product_properties.py`.
    - **Dependências:** 4.2.
    - **Propriedade:** Property 9 do design.
    - _Requisitos: 3.1, 3.2, 3.3, 3.4_

  - [x]* 4.6 Property 10: status do produto acompanha o estoque
    - Criar teste Hypothesis com no mínimo 100 exemplos para criação, atualização, estoque zero, estoque positivo e desativação explícita.
    - **Escopo:** `tests/property/test_product_properties.py`.
    - **Dependências:** 4.1, 4.3.
    - **Propriedade:** Property 10 do design.
    - _Requisitos: 3.5, 3.6, 3.8, 3.9_

  - [x]* 4.7 Property 11: patch de produto preserva campos ausentes
    - Criar teste Hypothesis com no mínimo 100 exemplos para confirmar merge patch e exceção do status derivado pelo estoque.
    - **Escopo:** `tests/property/test_product_properties.py`.
    - **Dependências:** 4.3.
    - **Propriedade:** Property 11 do design.
    - _Requisitos: 3.7_

- [x] 5. Implementar pedidos, idempotência e transações de estoque
  - [x] 5.1 Implementar canonicalização, fingerprint e armazenamento idempotente
    - Criar serviço com escopo por usuário/método/rota, chave de 16 a 128 caracteres, JSON canônico, replay e conflito por conteúdo diferente.
    - Persistir a chave e o resultado na mesma transação do recurso e tratar corridas sem registrar efeitos parciais.
    - **Escopo:** `app/core/idempotency.py`, `app/orders/service.py`, `app/users/service.py`, `app/products/service.py`.
    - **Dependências:** 1.3, 2.1, 3.2.
    - _Requisitos: 4.8, 4.9, 4.10, 8.8, 8.9_

  - [x] 5.2 Criar schemas, repository e cálculo monetário de pedidos
    - Implementar schemas de pedidos/itens, rejeição de itens duplicados, validação de quantidade, snapshots de preço e cálculo inteiro de subtotal/total.
    - **Escopo:** `app/orders/schemas.py`, `app/orders/repository.py`, `app/orders/service.py`, `app/orders/model.py`.
    - **Dependências:** 1.2, 1.3, 2.1, 5.1.
    - _Requisitos: 4.1, 4.2, 4.3, 4.4, 8.2, 8.4_

  - [x] 5.3 Implementar criação atômica de pedidos e redução de estoque
    - Adicionar `POST /v1/orders` com `Idempotency-Key` obrigatório, validar todos os produtos antes de mutar, criar pedido/itens e reduzir estoque em uma transação.
    - Garantir rollback integral para produto inexistente, indisponível, estoque insuficiente, falha de unicidade ou erro inesperado.
    - **Escopo:** `app/orders/router.py`, `app/orders/service.py`, `app/orders/repository.py`, `app/products/repository.py`, `app/database.py`.
    - **Dependências:** 1.3, 2.4, 5.1, 5.2.
    - _Requisitos: 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10_

  - [x]* 5.4 Property 12: cálculos monetários são inteiros e consistentes
    - Criar teste Hypothesis com no mínimo 100 exemplos para subtotais, total, limites e ausência de arredondamento.
    - **Escopo:** `tests/property/test_order_properties.py`.
    - **Dependências:** 5.2.
    - **Propriedade:** Property 12 do design.
    - _Requisitos: 4.2, 8.4_

  - [x]* 5.5 Property 13: pedido preserva preço no momento da compra
    - Criar teste Hypothesis com no mínimo 100 exemplos alterando o produto depois da compra e comparando snapshots persistidos.
    - **Escopo:** `tests/property/test_order_properties.py`.
    - **Dependências:** 5.3.
    - **Propriedade:** Property 13 do design.
    - _Requisitos: 4.3_

  - [x]* 5.6 Property 14: falha de item reverte todo o pedido
    - Criar teste Hypothesis com no mínimo 100 exemplos contendo item inválido e comparar pedidos, itens e estoques antes/depois.
    - **Escopo:** `tests/property/test_order_properties.py`.
    - **Dependências:** 5.3.
    - **Propriedade:** Property 14 do design.
    - _Requisitos: 4.5, 4.6_

  - [x]* 5.7 Property 15: pedido atendível altera entidades consistentemente
    - Criar teste Hypothesis com no mínimo 100 exemplos para pedido confirmado, itens, totais e redução exata do estoque.
    - **Escopo:** `tests/property/test_order_properties.py`.
    - **Dependências:** 5.3.
    - **Propriedade:** Property 15 do design.
    - _Requisitos: 4.1, 4.7_

  - [x]* 5.8 Property 16: retry idempotente de pedido não repete efeitos
    - Criar teste Hypothesis com no mínimo 100 exemplos repetindo chave e corpo e verificando um pedido e uma redução de estoque.
    - **Escopo:** `tests/property/test_idempotency_properties.py`.
    - **Dependências:** 5.3.
    - **Propriedade:** Property 16 do design.
    - _Requisitos: 4.8_

  - [x]* 5.9 Property 17: fingerprint impede reuso conflitante
    - Criar teste Hypothesis com no mínimo 100 exemplos para chave concluída e corpo diferente, `409` e preservação do efeito original.
    - **Escopo:** `tests/property/test_idempotency_properties.py`.
    - **Dependências:** 5.3.
    - **Propriedade:** Property 17 do design.
    - _Requisitos: 4.9_

  - [x]* 5.10 Testar criação de pedidos, rollback, concorrência e contrato HTTP
    - Cobrir pedido multi-item, preço alterado, produto inválido, estoque insuficiente, payload duplicado, chave ausente/inválida, retry, colisão e duas criações concorrentes.
    - Confirmar `201/409/422`, `Location`, resposta sanitizada e ausência de alteração parcial.
    - **Escopo:** `tests/integration/test_orders_create.py`, `tests/integration/test_order_concurrency.py`.
    - **Dependências:** 5.3.
    - _Requisitos: 4.1–4.10, 8.1–8.7_

- [x] 6. Implementar consulta e cancelamento de pedidos
  - [x] 6.1 Implementar listagem de pedidos com visão, filtros e paginação
    - Adicionar `GET /v1/orders` com customer restrito ao próprio usuário, admin com visão global, filtro `user_id` aplicável somente a admin, status e datas inclusivas.
    - **Escopo:** `app/orders/router.py`, `app/orders/service.py`, `app/orders/repository.py`.
    - **Dependências:** 2.4, 5.2, 7.1.
    - _Requisitos: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.1–7.5_

  - [x] 6.2 Implementar consulta individual com proteção de propriedade
    - Adicionar `GET /v1/orders/{orderId}` e retornar `404` para inexistente ou invisível ao customer, mantendo acesso administrativo a qualquer estado.
    - **Escopo:** `app/orders/router.py`, `app/orders/service.py`.
    - **Dependências:** 2.4, 5.2.
    - _Requisitos: 5.7, 5.8, 5.9, 5.10_

  - [x] 6.3 Implementar cancelamento transacional e devolução única de estoque
    - Adicionar `POST /v1/orders/{orderId}/cancel`, autorizar owner/admin, devolver estoque somente na transição `confirmed -> canceled`, tornar repetição idempotente e rejeitar estados não canceláveis com `409`.
    - **Escopo:** `app/orders/router.py`, `app/orders/service.py`, `app/orders/repository.py`, `app/products/repository.py`.
    - **Dependências:** 5.3, 6.2.
    - _Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x]* 6.4 Property 18: listagem de pedidos mantém visão e filtros
    - Criar teste Hypothesis com no mínimo 100 exemplos para customer/admin, user_id, status, período inclusivo e resultado de referência.
    - **Escopo:** `tests/property/test_order_query_properties.py`.
    - **Dependências:** 6.1.
    - **Propriedade:** Property 18 do design.
    - _Requisitos: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x]* 6.5 Property 19: consulta individual respeita visibilidade
    - Criar teste Hypothesis com no mínimo 100 exemplos para owner/admin, pedido cancelado, pedido inexistente e ausência de enumeração.
    - **Escopo:** `tests/property/test_order_query_properties.py`.
    - **Dependências:** 6.2.
    - **Propriedade:** Property 19 do design.
    - _Requisitos: 5.7, 5.8, 5.10_

  - [x]* 6.6 Property 20: cancelamento autorizado devolve estoque uma vez
    - Criar teste Hypothesis com no mínimo 100 exemplos para cancelamento, repetição, status e quantidades.
    - **Escopo:** `tests/property/test_order_cancel_properties.py`.
    - **Dependências:** 6.3.
    - **Propriedade:** Property 20 do design.
    - _Requisitos: 6.1, 6.2, 6.3_

  - [x]* 6.7 Property 21: cancelamento não autorizado ou inválido não muta estado
    - Criar teste Hypothesis com no mínimo 100 exemplos para `404`, `403`, `409` e snapshots inalterados de pedido/estoque.
    - **Escopo:** `tests/property/test_order_cancel_properties.py`.
    - **Dependências:** 6.3.
    - **Propriedade:** Property 21 do design.
    - _Requisitos: 6.4, 6.5, 6.6_

  - [x]* 6.8 Testar consultas e cancelamento por integração HTTP
    - Cobrir customer/admin, pedidos próprios/de terceiros, filtros, paginação, cancelamento confirmado, repetido, não autorizado e estado inválido.
    - **Escopo:** `tests/integration/test_orders_query_cancel.py`.
    - **Dependências:** 6.1, 6.2, 6.3.
    - _Requisitos: 5.1–5.10, 6.1–6.6_

- [x] 7. Implementar paginação por cursor e filtros compartilhados
  - [x] 7.1 Criar Pagination_Service com cursor assinado e visão vinculada
    - Implementar `limit` default 20, máximo 100, ordenação determinística `(created_at, id)`, busca `limit + 1`, `next_cursor`, `has_next` e cursor opaco.
    - Incluir no cursor recurso, filtros, visão autorizada, última posição e expiração; rejeitar assinatura inválida ou incompatível com `422`.
    - **Escopo:** `app/core/pagination.py` e integração nos repositories de users/products/orders.
    - **Dependências:** 1.2, 2.1.
    - _Requisitos: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x]* 7.2 Property 22: travessia de páginas é estável e completa
    - Criar teste Hypothesis com no mínimo 100 exemplos para filtros/visão/limits, garantindo cada registro compatível uma única vez e metadados finais corretos.
    - **Escopo:** `tests/property/test_pagination_properties.py`.
    - **Dependências:** 7.1.
    - **Propriedade:** Property 22 do design.
    - _Requisitos: 7.1, 7.2, 7.3, 7.5_

  - [x]* 7.3 Property 23: cursor inválido não produz resultados
    - Criar teste Hypothesis com no mínimo 100 exemplos para cursor corrompido, recurso/filtro/visão incompatíveis e resposta `422` sem dados.
    - **Escopo:** `tests/property/test_pagination_properties.py`.
    - **Dependências:** 7.1.
    - **Propriedade:** Property 23 do design.
    - _Requisitos: 7.4_

  - [x]* 7.4 Testar paginação integrada em todas as listagens
    - Cobrir lista vazia, uma página, várias páginas, limites 1/20/100, filtros combinados e cursor incompatível para users/products/orders.
    - **Escopo:** `tests/integration/test_pagination.py`.
    - **Dependências:** 3.3, 4.2, 6.1, 7.1.
    - _Requisitos: 7.1–7.5_

- [x] 8. Integrar routers, schemas de resposta e contrato OpenAPI
  - [x] 8.1 Registrar routers e dependências na aplicação FastAPI
    - Incluir routers de users, products e orders em `app/main.py`, preservar `/health`, lifespan e criação/migration de schema conforme a estratégia adotada.
    - Confirmar que cada operação usa método, caminho, status e `response_model` do contrato.
    - **Escopo:** `app/main.py`, routers dos três módulos e ciclo de vida da aplicação.
    - **Dependências:** 3.4, 4.3, 5.3, 6.3, 7.1.
    - _Requisitos: 1.4, 2.1–2.14, 3.1–3.13, 4.1–4.10, 5.1–5.10, 6.1–6.6_

  - [x] 8.2 Ajustar serializadores e schemas de sucesso ao OpenAPI
    - Garantir campos obrigatórios, UUID, `date-time`, inteiros em centavos, enums, envelopes de página e ausência de propriedades extras/credenciais.
    - **Escopo:** `app/users/schemas.py`, `app/products/schemas.py`, `app/orders/schemas.py`, `app/common/responses.py`.
    - **Dependências:** 3.1, 4.1, 5.2, 8.1.
    - _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x]* 8.3 Property 24: serialização pública é compatível e segura
    - Criar teste Hypothesis com no mínimo 100 exemplos para users/products/orders/items, campos obrigatórios, tipos públicos e ausência de credenciais.
    - **Escopo:** `tests/property/test_serialization_properties.py`.
    - **Dependências:** 8.2.
    - **Propriedade:** Property 24 do design.
    - _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [x]* 8.4 Property 25: entrada inválida falha antes da persistência
    - Criar teste Hypothesis com no mínimo 100 exemplos para campos extras, obrigatórios ausentes, limites inválidos, UUIDs inválidos e estado inalterado.
    - **Escopo:** `tests/property/test_validation_properties.py`.
    - **Dependências:** 2.2, 3.1, 4.1, 5.2, 8.2.
    - **Propriedade:** Property 25 do design.
    - _Requisitos: 8.6_

  - [x]* 8.5 Property 26: todo erro previsto tem envelope consistente
    - Criar teste Hypothesis com no mínimo 100 exemplos sobre erros previstos, verificando media type, campos obrigatórios, `request_id` e igualdade entre status HTTP e `status` do corpo.
    - **Escopo:** `tests/property/test_error_properties.py`.
    - **Dependências:** 2.2, 8.1.
    - **Propriedade:** Property 26 do design.
    - _Requisitos: 8.7_

  - [x]* 8.6 Property 27: retry idempotente de cadastro não duplica usuário
    - Criar teste Hypothesis com no mínimo 100 exemplos para replay igual, conflito por corpo diferente e contagem persistida.
    - **Escopo:** `tests/property/test_idempotency_properties.py`.
    - **Dependências:** 3.2, 5.1.
    - **Propriedade:** Property 27 do design.
    - _Requisitos: 8.8, 8.9_

  - [x]* 8.7 Validar contrato OpenAPI contra respostas e rotas implementadas
    - Adicionar validação automatizada de status, media types, schemas, `additionalProperties`, headers `Location` e campos de erro, sem substituir `openapi.yaml` pela geração automática.
    - **Escopo:** `tests/contract/`, configuração de pytest e dependência de validação OpenAPI.
    - **Dependências:** 8.1, 8.2.
    - _Requisitos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

- [x] 9. Consolidar suíte, configuração de teste e regressão
  - [x] 9.1 Organizar fixtures e comandos de teste por camada
    - Separar testes unitários, property-based, integração, contrato e concorrência; configurar Hypothesis com `max_examples=100` nas propriedades.
    - Garantir que os testes não usam `users_by_email` como fonte de verdade e que o banco é isolado por caso.
    - **Escopo:** `tests/conftest.py`, `pyproject.toml` ou configuração equivalente, `requirements.txt`.
    - **Dependências:** 1.5, 2.5, 3.5, 5.10, 6.8, 7.4, 8.7.
    - _Requisitos: 2.1–2.14, 3.1–3.13, 4.1–4.10, 5.1–5.10, 6.1–6.6, 7.1–7.5, 8.1–8.9_

  - [x]* 9.2 Executar regressão do cadastro público existente
    - Preservar e ampliar `tests/test_users.py` para confirmar `201`, cliente ativo, normalização, hash Argon2, rejeição de role e conflito de e-mail, sem senha na resposta.
    - **Escopo:** `tests/test_users.py`, `app/users/*` somente se um ajuste de compatibilidade for necessário.
    - **Dependências:** 3.5, 8.1, 9.1.
    - _Requisitos: 8.1, 8.5, 8.6, 8.8, além do comportamento já existente do cadastro_

  - [x]* 9.3 Executar suíte automatizada completa e corrigir regressões de integração
    - Executar testes unitários, property-based, integração, contrato e concorrência em banco temporário; corrigir somente falhas relacionadas ao escopo da API.
    - Validar que não existem rotas fora do contrato nem respostas com credenciais.
    - **Escopo:** toda a suíte `tests/` e módulos de aplicação cobertos pelas falhas.
    - **Dependências:** 9.1, 9.2.
    - _Requisitos: todos os requisitos 1.1–8.9_

- [x] 10. Checkpoint de implementação incremental
  - Garantir que os testes das etapas concluídas passam e que dúvidas de comportamento do contrato sejam resolvidas antes de avançar para a próxima etapa.

- [x] 11. Checkpoint final de qualidade
  - Garantir que a suíte automatizada completa passa, que o cadastro existente permanece compatível e que cada endpoint do `openapi.yaml` está integrado aos módulos persistentes, autenticação, erros e paginação correspondentes.

## Notes

- Tarefas marcadas com `*` são opcionais para um MVP, mas são recomendadas para concluir a cobertura integral dos requisitos e do design.
- Todas as tarefas de implementação usam Python, FastAPI, SQLAlchemy, SQLite, Pydantic e Hypothesis conforme o design aprovado.
- O cadastro público existente deve continuar criando apenas clientes ativos, normalizando e-mails, usando Argon2 e omitindo senha/hash das respostas.
- `openapi.yaml` é o contrato HTTP. Não devem ser adicionados endpoints de login, pagamento ou outros endpoints ausentes do contrato.
- As propriedades 1 a 27 do design foram convertidas em tarefas independentes de property-based testing e cada uma referencia os requisitos que valida.
- As tarefas de teste são sub-tarefas dos respectivos blocos de implementação e não devem criar componentes desacoplados.
- O pipeline recomendado após as tarefas é: testes unitários/property/integrados/contrato, migration em banco temporário e teste de concorrência transacional.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["1.3", "2.2", "2.3"] },
    { "id": 3, "tasks": ["1.4", "2.4", "3.1", "4.1"] },
    { "id": 4, "tasks": ["1.5", "2.5", "2.6", "2.7", "2.8", "3.2", "7.1"] },
    { "id": 5, "tasks": ["3.3", "3.4", "4.2", "4.3", "5.1", "5.2"] },
    { "id": 6, "tasks": ["3.5", "3.6", "3.7", "3.8", "3.9", "4.4", "4.5", "4.6", "4.7", "5.3", "5.4", "5.5", "5.6", "5.7", "5.8", "5.9", "6.1", "6.2", "7.2", "7.3"] },
    { "id": 7, "tasks": ["3.10", "5.10", "6.3", "7.4"] },
    { "id": 8, "tasks": ["6.4", "6.5", "6.6", "6.7", "6.8", "8.1"] },
    { "id": 9, "tasks": ["8.2"] },
    { "id": 10, "tasks": ["8.3", "8.4", "8.5", "8.6", "8.7"] },
    { "id": 11, "tasks": ["9.1"] },
    { "id": 12, "tasks": ["9.2"] },
    { "id": 13, "tasks": ["9.3"] }
  ]
}
```
