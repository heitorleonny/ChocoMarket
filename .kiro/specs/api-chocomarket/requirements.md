# Requirements Document

## Introduction

A API ChocoMarket fornece os recursos de usuários, produtos e pedidos para uma loja online de chocolates. O projeto atual já contém o cadastro público de clientes com persistência em SQLite, validação de nome, e-mail e senha, normalização do e-mail, hash da senha, prevenção de e-mail duplicado e resposta sem senha.

Este documento define o restante da API conforme o contrato `openapi.yaml`. O escopo inclui gerenciamento de usuários ainda não implementado, autenticação e autorização das rotas protegidas, catálogo e estoque de produtos, pedidos, idempotência, paginação, erros padronizados e regras de visibilidade. O comportamento já implementado do cadastro público deve permanecer compatível com o contrato durante a evolução da API.

## Glossary

- **API_ChocoMarket**: API REST responsável pelos recursos da loja ChocoMarket e pelas respostas HTTP do contrato `openapi.yaml`.
- **User_Module**: módulo responsável por usuários, perfis, papéis, status, senhas e operações de gerenciamento de usuários.
- **Product_Module**: módulo responsável pelo catálogo, preços, estoque e status dos produtos.
- **Order_Module**: módulo responsável por pedidos, itens de pedido, totais, status e movimentação de estoque.
- **Auth_Service**: componente que autentica o token Bearer e autoriza operações conforme o papel e o status do usuário.
- **Customer**: usuário com o papel `customer`, autorizado a consultar o próprio perfil, consultar produtos ativos e gerenciar os próprios pedidos.
- **Administrator**: usuário com o papel `admin`, autorizado a gerenciar usuários, produtos e todos os pedidos conforme o contrato.
- **Active_User**: usuário com status `active` e autorização para usar recursos protegidos.
- **Inactive_User**: usuário com status `inactive`; o registro permanece armazenado, mas o usuário não pode autenticar nem usar recursos protegidos.
- **Product**: item do catálogo com nome, descrição, preço em centavos, quantidade em estoque e status `active`, `inactive` ou `out_of_stock`.
- **Order**: compra pertencente a um usuário, contendo um ou mais itens, total em centavos e status `confirmed` ou `canceled`.
- **OrderItem**: item persistido de um Order, contendo produto, quantidade, preço unitário vigente no momento da compra e subtotal.
- **Cursor_Pagination**: paginação que usa `limit`, `cursor`, `next_cursor` e `has_next` para percorrer listagens.
- **Idempotency_Key**: chave enviada no cabeçalho `Idempotency-Key` para repetir com segurança o resultado de uma operação de criação.
- **Problem_Response**: resposta de erro no formato `application/problem+json`, contendo `type`, `title`, `status`, `detail`, `code` e `request_id`.
- **Logical_Deletion**: desativação que mantém o registro persistido e altera o status para `inactive`.
- **Atomic_Transaction**: operação persistida integralmente ou revertida integralmente, sem alterações parciais em pedido ou estoque.

## Requirements

### Requisito 1: Autenticação e autorização

**História de usuário:** Como cliente ou administrador, quero que as rotas protegidas validem minha identidade e meu papel, para que somente operações autorizadas sejam executadas.

#### Critérios de aceitação

1. WHEN uma rota protegida recebe uma requisição sem cabeçalho `Authorization` no esquema Bearer, com token malformado, expirado ou não validável, THE Auth_Service SHALL rejeitar a requisição com HTTP 401 e uma Problem_Response, sem executar a operação solicitada.
2. WHEN um usuário autenticado e com status `active` solicita uma operação que o papel `customer` ou `admin` não autoriza conforme o contrato, THE Auth_Service SHALL rejeitar a requisição com HTTP 403 e uma Problem_Response, sem executar a operação solicitada.
3. WHILE o usuário associado ao token Bearer possui status `inactive`, THE Auth_Service SHALL rejeitar qualquer requisição a uma rota protegida com HTTP 401 e uma Problem_Response, sem executar a operação solicitada.
4. WHEN uma requisição sem token Bearer acessa uma operação de listagem ou consulta de produtos definida como pública no contrato, THE API_ChocoMarket SHALL processar a requisição sem exigir autenticação; se a operação não for uma rota pública definida no contrato, THE API_ChocoMarket SHALL exigir autenticação conforme os critérios anteriores.

### Requisito 2: Gerenciamento de usuários

**História de usuário:** Como administrador, quero listar, consultar, atualizar e desativar usuários, para que eu possa administrar as contas da loja.

#### Critérios de aceitação

1. WHEN um Administrator autenticado solicita `GET /v1/users` sem informar o filtro `status`, THE User_Module SHALL retornar uma página Cursor_Pagination cujo campo `data` contenha somente usuários com status `active` e cuja metainformação siga o contrato de paginação.
2. WHEN um Administrator autenticado solicita `GET /v1/users` informando um ou mais filtros de `name`, `email`, `role` ou `status`, THE User_Module SHALL retornar somente usuários que satisfaçam simultaneamente todos os filtros informados; o filtro `name` SHALL selecionar nomes que contenham o texto informado sem distinção entre maiúsculas e minúsculas, e os filtros `email`, `role` e `status` SHALL usar correspondência exata, com o e-mail comparado após normalização para minúsculas.
3. WHEN um Customer autenticado solicita o perfil correspondente ao próprio `userId` e esse perfil possui status `active`, THE User_Module SHALL retornar HTTP 200 com o perfil completo definido no contrato, sem os campos `password` ou `password_hash`.
4. WHEN um Customer autenticado solicita o perfil de outro usuário ou de um usuário com status `inactive`, THE User_Module SHALL retornar HTTP 404 com uma `Problem_Response` e não incluir dados do usuário solicitado na resposta.
5. WHEN um Administrator autenticado solicita o perfil de um usuário existente com status `active` ou `inactive`, THE User_Module SHALL retornar HTTP 200 com o perfil definido no contrato, sem os campos `password` ou `password_hash`.
6. WHEN um Customer autenticado atualiza o próprio perfil informando um ou ambos os campos `name` e `email` válidos, THE User_Module SHALL alterar somente os campos presentes na requisição, preservar os demais campos, inclusive `role` e `status`, e retornar HTTP 200 com o perfil atualizado sem os campos `password` ou `password_hash`.
7. WHEN um Administrator autenticado atualiza o perfil de um usuário existente informando um ou mais campos entre `name`, `email`, `role` e `status` válidos, THE User_Module SHALL alterar somente os campos presentes na requisição, preservar os demais campos e retornar HTTP 200 com o perfil atualizado sem os campos `password` ou `password_hash`.
8. WHEN uma atualização informa um e-mail que, após normalização para minúsculas, já está associado a outro usuário, THE User_Module SHALL retornar HTTP 409 com uma `Problem_Response` e preservar integralmente os dados anteriores do usuário atualizado.
9. WHEN um Customer autenticado solicita `DELETE /v1/users/{userId}` para o próprio perfil ativo ou um Administrator autenticado solicita a operação para um usuário existente com status `active`, THE User_Module SHALL aplicar `Logical_Deletion`, alterar o status para `inactive`, manter o registro persistido, retornar HTTP 204 e não incluir corpo na resposta.
10. WHEN um Administrator autenticado solicita `DELETE /v1/users/{userId}` para um usuário já `inactive`, THE User_Module SHALL retornar HTTP 204, manter o registro persistido com status `inactive` e não alterar os demais dados do registro.
11. WHEN um Customer autenticado solicita alteração de senha para o próprio perfil informando `current_password` e `new_password` válidas, ambas com comprimento de 12 a 128 caracteres, e a senha atual corresponde à credencial armazenada, THE User_Module SHALL substituir a senha armazenada pela nova senha protegida, retornar HTTP 204 sem corpo e não expor qualquer senha na resposta.
12. WHEN um Customer autenticado solicita alteração de senha para o próprio perfil e `current_password` não corresponde à credencial armazenada, THE User_Module SHALL retornar HTTP 401 com uma `Problem_Response`, preservar a senha anterior e não alterar o perfil.
13. WHEN um solicitante autenticado não tem permissão para alterar a senha do `userId` informado, THE User_Module SHALL retornar HTTP 403 com uma `Problem_Response` quando o usuário-alvo existir e estiver disponível ao solicitante, ou HTTP 404 com uma `Problem_Response` quando o usuário-alvo não existir ou não estiver disponível ao solicitante, sem alterar a senha nem o perfil.
14. WHEN um Administrator autenticado solicita o perfil, a atualização ou a desativação de um `userId` inexistente, THE User_Module SHALL retornar HTTP 404 com uma `Problem_Response` e não criar nem alterar qualquer registro.

### Requisito 3: Catálogo de produtos

**História de usuário:** Como cliente, quero consultar o catálogo, para que eu possa encontrar chocolates disponíveis para compra.

#### Critérios de aceitação

1. WHEN uma requisição pública ou de um Customer solicita `GET /v1/products` sem informar o filtro `status`, THE Product_Module SHALL retornar HTTP 200 com uma página Cursor_Pagination cujo campo `data` contenha somente produtos com status `active`.
2. WHEN um Administrator solicita `GET /v1/products` com um ou mais filtros de nome, `min_price_cents`, `max_price_cents` ou status, THE Product_Module SHALL retornar HTTP 200 somente produtos que satisfaçam simultaneamente todos os filtros informados, considerando o nome que contenha o valor informado, `price_cents` maior ou igual a `min_price_cents`, `price_cents` menor ou igual a `max_price_cents` e correspondência exata para `status`.
3. WHEN um Customer solicita um produto inexistente ou com status `inactive` ou `out_of_stock`, THE Product_Module SHALL retornar HTTP 404 com uma Problem_Response e não revelar os dados do produto.
4. WHEN um Administrator solicita um produto existente, THE Product_Module SHALL retornar HTTP 200 com o produto independentemente de seu status e sem os campos de senha, hash de senha ou credenciais.
5. WHEN um Administrator cria um produto com `stock_quantity` entre 1 e 1.000.000, THE Product_Module SHALL persistir o produto com status `active` e retornar HTTP 201 com o produto criado.
6. WHEN um Administrator cria um produto com `stock_quantity` igual a 0, THE Product_Module SHALL persistir o produto com status `out_of_stock` e retornar HTTP 201 com o produto criado.
7. WHEN um Administrator atualiza parcialmente um produto existente com pelo menos um campo válido, THE Product_Module SHALL atualizar somente os campos informados, preservar os campos ausentes e retornar HTTP 200 com o produto atualizado.
8. WHEN uma atualização de produto define `stock_quantity` igual a 0, THE Product_Module SHALL definir o status do produto como `out_of_stock` e retornar o produto atualizado.
9. WHEN uma atualização de produto define `stock_quantity` entre 1 e 1.000.000 para um produto com status `active` ou `out_of_stock`, THE Product_Module SHALL definir o status do produto como `active` e retornar o produto atualizado.
10. WHEN um Administrator solicita `DELETE /v1/products/{productId}` para um produto existente, THE Product_Module SHALL aplicar `Logical_Deletion`, alterar o status para `inactive`, preservar o registro para referências de pedidos e retornar HTTP 204 sem corpo.
11. WHEN um Administrator repete `DELETE /v1/products/{productId}` para um produto já com status `inactive`, THE Product_Module SHALL retornar HTTP 204 sem corpo, manter o registro persistido e não alterar seus demais dados.
12. WHEN uma requisição de criação ou atualização de produto informa `price_cents` fora do intervalo de 1 a 1.000.000, `stock_quantity` fora do intervalo de 0 a 1.000.000, nome com menos de 1 ou mais de 160 caracteres, descrição com mais de 2.000 caracteres ou outro valor fora das restrições do contrato, THE Product_Module SHALL retornar HTTP 422 com uma Problem_Response e não criar nem alterar o produto.
13. WHEN um Administrator solicita consulta, atualização ou exclusão de um `productId` inexistente, THE Product_Module SHALL retornar HTTP 404 com uma Problem_Response e não criar nem alterar qualquer produto.

### Requisito 4: Criação de pedidos

**História de usuário:** Como cliente, quero criar um pedido informando produtos e quantidades, para que a API calcule o valor da compra e reserve o estoque corretamente.

#### Critérios de aceitação

1. WHEN um Customer autenticado envia `POST /v1/orders` com um corpo que contém `items` com 1 a 50 itens, cada item com `product_id` UUID e `quantity` inteira entre 1 e 100, THE Order_Module SHALL identificar o usuário pelo token Bearer, criar um Order com status `confirmed` e retornar HTTP 201 com o Order criado.
2. WHEN um pedido é criado, THE Order_Module SHALL usar o preço vigente de cada Product no momento da confirmação da criação, calcular cada `subtotal_cents` como `quantity * unit_price_cents` e calcular `total_cents` como a soma dos subtotais de todos os itens.
3. WHEN um pedido é criado, THE Order_Module SHALL persistir em cada OrderItem o `unit_price_cents` vigente no momento da confirmação da criação, independentemente de alterações posteriores no preço do Product.
4. WHEN uma requisição de pedido contém o mesmo `product_id` em dois ou mais itens, THE Order_Module SHALL retornar HTTP 422 com uma Problem_Response e não criar um Order nem alterar o estoque.
5. WHEN uma requisição de pedido contém um Product inexistente, com status diferente de `active` ou com estoque inferior à `quantity` solicitada, THE Order_Module SHALL retornar HTTP 409 com uma Problem_Response, não criar um Order e não alterar o estoque de nenhum Product.
6. WHEN qualquer item de um pedido não pode ser atendido, THE Order_Module SHALL executar uma Atomic_Transaction que preserve o estado persistido anterior, sem criar o Order e sem alterar o estoque de nenhum item.
7. WHEN todos os itens de um pedido são válidos e podem ser atendidos, THE Order_Module SHALL executar uma Atomic_Transaction que persista o Order completo com todos os itens, subtotais, `total_cents` e status `confirmed`, reduza o estoque de cada Product pela quantidade solicitada e retorne HTTP 201.
8. WHEN uma `Idempotency_Key` válida é reutilizada com o mesmo conteúdo do corpo de uma criação de pedido concluída, THE Order_Module SHALL retornar o resultado original da criação, incluindo o mesmo Order, sem criar outro Order nem reduzir o estoque novamente.
9. WHEN uma `Idempotency_Key` é reutilizada com conteúdo diferente do corpo associado à criação original, THE Order_Module SHALL retornar HTTP 409 com uma Problem_Response e preservar o Order e o estoque da criação original.
10. WHEN uma requisição de criação de pedido não informa `Idempotency_Key` ou informa uma chave com menos de 16 ou mais de 128 caracteres, THE API_ChocoMarket SHALL retornar HTTP 422 com uma Problem_Response, sem criar um Order e sem alterar o estoque.

### Requisito 5: Consulta de pedidos

**História de usuário:** Como cliente ou administrador, quero consultar pedidos, para que eu possa acompanhar compras e operações da loja.

#### Critérios de aceitação

1. WHEN um Customer autenticado solicita `GET /v1/orders`, THE Order_Module SHALL retornar uma página Cursor_Pagination contendo exclusivamente Orders pertencentes ao Customer identificado pelo token Bearer, independentemente dos filtros informados.
2. WHEN um Administrator solicita `GET /v1/orders`, THE Order_Module SHALL retornar uma página Cursor_Pagination contendo Orders de todos os usuários, limitada pelos filtros válidos informados.
3. WHEN uma listagem de pedidos recebe um filtro de status `confirmed` ou `canceled`, THE Order_Module SHALL retornar somente Orders com o status informado.
4. WHEN uma listagem de pedidos recebe um filtro de período de criação, THE Order_Module SHALL retornar somente Orders cuja data de criação esteja dentro dos limites informados, incluindo os limites inicial e final quando fornecidos.
5. WHEN um Administrator solicita uma listagem de pedidos com o filtro `user_id`, THE Order_Module SHALL retornar somente Orders pertencentes ao usuário identificado por esse filtro.
6. WHEN um Customer solicita uma listagem de pedidos com o filtro `user_id`, THE Order_Module SHALL ignorar esse filtro e retornar somente Orders pertencentes ao Customer autenticado.
7. WHEN um Customer solicita um Order inexistente ou pertencente a outro usuário, THE Order_Module SHALL retornar HTTP 404 com uma Problem_Response.
8. WHEN um Administrator solicita um Order existente, THE Order_Module SHALL retornar o Order independentemente do usuário proprietário e do status do Order.
9. WHEN um Administrator solicita um Order inexistente, THE Order_Module SHALL retornar HTTP 404 com uma Problem_Response.
10. WHEN um Customer solicita um Order próprio, THE Order_Module SHALL retornar os itens, os subtotais, o total, o status e as datas de criação e atualização do Order.

### Requisito 6: Cancelamento de pedidos

**História de usuário:** Como cliente ou administrador, quero cancelar pedidos autorizados, para que o estoque seja devolvido quando uma compra for cancelada.

#### Critérios de aceitação

1. WHEN um Customer autenticado solicita o cancelamento de um Order próprio com status `confirmed`, THE Order_Module SHALL, em uma Atomic_Transaction, alterar o status do Order para `canceled`, devolver ao estoque a quantidade de cada item do Order e retornar HTTP 200 com o Order cancelado.
2. WHEN um Administrator autenticado solicita o cancelamento de um Order com status `confirmed`, THE Order_Module SHALL, em uma Atomic_Transaction, alterar o status do Order para `canceled`, devolver ao estoque a quantidade de cada item do Order e retornar HTTP 200 com o Order cancelado.
3. WHEN um usuário autorizado solicita o cancelamento de um Order com status `canceled`, THE Order_Module SHALL retornar HTTP 200 com o Order cancelado e manter inalteradas as quantidades em estoque.
4. WHEN um Customer solicita o cancelamento de um Order inexistente ou pertencente a outro usuário, THE Order_Module SHALL retornar HTTP 404 com uma Problem_Response e não alterar o Order nem o estoque.
5. WHEN um usuário autenticado cujo papel não autoriza o cancelamento solicita o cancelamento de um Order visível para esse usuário, THE Auth_Service SHALL retornar HTTP 403 com uma Problem_Response e não alterar o Order nem o estoque.
6. WHEN um usuário autorizado solicita o cancelamento de um Order com status diferente de `confirmed` ou `canceled`, THE Order_Module SHALL retornar HTTP 409 com uma Problem_Response e preservar o status do Order e as quantidades em estoque.

### Requisito 7: Paginação e filtros

**História de usuário:** Como consumidor da API, quero percorrer listagens de forma previsível, para que eu possa consultar muitos registros sem respostas excessivamente grandes.

#### Critérios de aceitação

1. THE API_ChocoMarket SHALL aceitar, em toda listagem que use Cursor_Pagination, um parâmetro `limit` inteiro entre 1 e 100, inclusive, e SHALL usar 20 quando `limit` não for informado.
2. WHEN uma listagem contém mais registros compatíveis com os filtros e as permissões do solicitante do que o valor de `limit`, THE API_ChocoMarket SHALL retornar exatamente `limit` registros, `has_next` igual a `true` e um `next_cursor` não nulo que permita solicitar a página seguinte da mesma listagem.
3. WHEN uma listagem não contém registros além dos retornados na página atual, inclusive quando a listagem retorna zero registros, THE API_ChocoMarket SHALL retornar `has_next` igual a `false` e `next_cursor` igual a `null`.
4. WHEN uma requisição informa um `cursor` malformado, um `cursor` não emitido pela API ou um `cursor` incompatível com o recurso ou com os filtros da requisição, ou informa `limit` não inteiro, menor que 1 ou maior que 100, THE API_ChocoMarket SHALL retornar HTTP 422 com uma Problem_Response e não SHALL retornar uma página de resultados.
5. WHEN uma requisição usa filtros definidos para o recurso, THE API_ChocoMarket SHALL retornar somente registros que satisfaçam simultaneamente todos os filtros permitidos informados e que estejam visíveis ao solicitante conforme suas permissões; a aplicação do `cursor` SHALL manter esses mesmos filtros e permissões na página retornada.

### Requisito 8: Respostas, validação e erros

**História de usuário:** Como consumidor da API, quero respostas consistentes, para que eu possa integrar a API sem interpretar formatos diferentes por recurso.

#### Critérios de aceitação

1. THE API_ChocoMarket SHALL incluir, em cada resposta de sucesso de usuários, produtos ou pedidos, todos os campos marcados como obrigatórios para a operação correspondente no contrato `openapi.yaml`, sem omitir nenhum desses campos.
2. THE API_ChocoMarket SHALL representar todos os identificadores aplicáveis de usuário, produto, pedido e item como valores no formato UUID definido pelo contrato `openapi.yaml`, tanto nas respostas quanto nas requisições que os contenham.
3. THE API_ChocoMarket SHALL representar todos os campos de criação e atualização de usuários, produtos e pedidos no formato `date-time` definido pelo contrato `openapi.yaml`.
4. THE API_ChocoMarket SHALL representar preços, preços unitários, subtotais e totais como números inteiros em centavos, sem casas decimais, nas respostas e nas requisições aplicáveis.
5. THE API_ChocoMarket SHALL omitir das respostas HTTP os campos e valores correspondentes a senha, hash de senha e qualquer outra credencial.
6. WHEN o corpo de uma requisição contém um campo desconhecido, omite um campo obrigatório ou contém um valor que viola uma restrição do contrato `openapi.yaml`, THE API_ChocoMarket SHALL retornar HTTP 422 com conteúdo `application/problem+json` e uma Problem_Response, sem criar o recurso nem alterar dados persistidos.
7. WHEN a API_ChocoMarket retorna um erro HTTP 4xx ou 5xx previsto no contrato, THE API_ChocoMarket SHALL retornar conteúdo `application/problem+json` contendo `type`, `title`, `status`, `detail`, `code` e um `request_id` não vazio, e o valor de `status` SHALL ser igual ao código HTTP retornado.
8. WHEN uma requisição de criação de usuário reutiliza uma `Idempotency_Key` vinculada a uma criação concluída e contém o mesmo conteúdo de requisição da criação original, THE User_Module SHALL retornar o mesmo resultado HTTP da criação original, sem criar outro usuário nem alterar o usuário criado originalmente.
9. WHEN uma requisição de criação de usuário reutiliza uma `Idempotency_Key` vinculada a uma criação concluída e contém conteúdo de requisição diferente da criação original, THE User_Module SHALL retornar HTTP 409 com conteúdo `application/problem+json` e uma Problem_Response, sem criar ou alterar qualquer usuário.
