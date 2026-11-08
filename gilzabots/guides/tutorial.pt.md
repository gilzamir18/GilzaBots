# GilzaBots — Guia rápido de uso

Bem-vindo ao **GilzaBots**, um agente de IA livre que trabalha dentro dos seus projetos:
ele lê e edita arquivos, roda comandos, pesquisa na web, escreve código e documentos e
fica de olho nas coisas com monitores em segundo plano. Tudo roda na sua máquina, e a IA
roda em um modelo local do Ollama ou em um provedor que você configura com a sua própria
chave de API. Não há conta, nem cobrança, nem serviço de nuvem do GilzaBots.

Este chat é o seu tutorial. Escolha uma pergunta no menu abaixo e eu respondo na hora —
não é preciso ter nenhum modelo configurado para isso. Depois de cadastrar um modelo,
você também pode digitar suas próprias perguntas aqui: mantenho este guia inteiro na
memória desta conversa.

## overview :: Como o GilzaBots funciona?

O GilzaBots tem três partes trabalhando juntas:

1. **O agente de IA** — um orquestrador de chat que lê e edita seus arquivos, roda
   comandos, pesquisa na web, gera documentos de escritório (.docx, .pptx), faz
   perguntas interativas para tirar dúvidas, segue skills da **Asset Store** em tarefas
   especializadas e passa trabalhos auxiliares (leituras longas, condensar logs,
   pesquisa na web) para agentes helpers.
2. **O espaço de trabalho** — editor de código-fonte com explorador de arquivos,
   terminal e Git, além de pré-visualização de Markdown, HTML e PDF e modo rich text.
3. **Ferramentas de documentos** — projetos LaTeX têm pré-visualização do PDF lado a
   lado, SyncTeX para pular entre o PDF e a linha `.tex` que o gerou, modo WYSIWYG
   baseado em modelo e compilação local com o Tectonic, sem exigir uma instalação de
   LaTeX no sistema.

O fluxo normal de trabalho é:

- criar ou importar um projeto (um projeto é uma pasta);
- cadastrar uma conexão de provedor e um modelo nas Configurações, e depois selecionar o
  modelo na barra do chat;
- pedir o que você precisa ao assistente no chat, ou instalar skills/templates pela
  **Asset Store**;
- compilar e conferir o PDF;
- revisar as alterações do assistente no modo **Review** antes de mantê-las.

O assistente roda em um de três **modos**, selecionáveis na barra do chat:

- **auto** — autonomia total; executa ferramentas sem pedir permissão a cada passo;
- **plan** — não pode modificar nada; reúne contexto e propõe um plano estruturado que
  você aprova antes;
- **edit** — edita arquivos diretamente, mas pergunta antes de rodar comandos no
  terminal.

## installation :: Quais sistemas são suportados e como instalo o GilzaBots?

O GilzaBots suporta **Ubuntu 24.04**, **Ubuntu 26.04** e **Windows 11**. O instalador não
exige privilégios de administrador e instala no seu perfil de usuário.

**Ubuntu** (x86_64/amd64, com `curl`): abra um terminal e execute

```bash
curl -fsSL https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.sh -o gilzabots-install.sh
bash gilzabots-install.sh
```

Ele baixa a versão para Linux em `~/.local/share/GilzaBots` e cria um atalho na área de
trabalho e o comando `~/.local/bin/gilzabots`. Se o `curl` não estiver instalado, instale-o
antes com `sudo apt update && sudo apt install curl`.

**Windows 11**: abra o PowerShell e execute

```powershell
irm https://raw.githubusercontent.com/gilzamir18/GilzaBots/main/install.ps1 | iex
```

O aplicativo fica em `%LOCALAPPDATA%\GilzaBots`, com atalhos na Área de Trabalho e no menu
Iniciar.

O GilzaBots roda com as permissões normais do seu usuário, então o terminal integrado e os
comandos do assistente enxergam os mesmos programas que o terminal do sistema. Comandos
administrativos continuam exigindo sua autorização via `sudo`.

## uninstall :: Como desinstalo o GilzaBots?

Primeiro feche o GilzaBots. No Ubuntu, a instalação fornece:

```bash
gilzabots-uninstall
```

Esse comando remove o aplicativo e preserva configurações globais, chats, caches,
credenciais e todos os diretórios de projetos. Use `gilzabots-uninstall --purge`
somente se também quiser apagar os dados globais do aplicativo; o comando mostra
o diretório exato e pede confirmação. Os diretórios dos projetos nunca são removidos.

No Windows, use **Configurações > Aplicativos > Aplicativos instalados > GilzaBots**
ou o atalho **Desinstalar GilzaBots** no menu Iniciar.

## projects :: Como crio e configuro um projeto?

Um projeto é uma pasta no seu disco mais os metadados que o GilzaBots guarda sobre ela
(modelos selecionados, modo, chats, memória, checkpoints do Git sombra).

- **Novo projeto** — o diálogo "Novo Projeto" pede um nome, uma pasta-mãe e,
  opcionalmente, um modelo inicial. Só é permitido um projeto por pasta.
- **Importar projeto** — aponte o GilzaBots para uma pasta existente que já contenha os
  seus fontes `.tex`.

Duas coisas costumam surpreender quem está começando, e as duas são intencionais:

- **Um projeto começa sem modelo configurado.** O GilzaBots nunca escolhe um modelo por
  você. Enquanto você não selecionar um, o assistente se recusa a rodar e avisa que é
  preciso escolher — ele não cai silenciosamente em algum padrão.
- **Não escolher modelo é um estado válido.** Você pode limpar a seleção nas
  Configurações do Projeto e voltar ao estado não configurado.

Os projetos guardam referências aos modelos por seu ID de catálogo (`<provedor>/<nome>`).
Credenciais globais (chaves de API e URLs base) pertencem ao catálogo global de modelos,
então atualizar uma chave nas Configurações atualiza todos os projetos automaticamente.

## project-settings :: O que significa cada configuração e parâmetro de projeto?

As opções de projeto são editadas no diálogo de Configurações do Projeto (ícone de
engrenagem na barra lateral) e estão organizadas em quatro abas:

### 1. Aba Geral (`geral`)

- **ID Interno e Nome de Exibição** — o `ID Interno` é a chave persistente no banco de
  dados; o `Nome de Exibição` é o título amigável mostrado na interface.
- **Caminho do Projeto e Arquivo Principal** — a pasta absoluta no disco e o arquivo
  `.tex` raiz utilizado como ponto de partida da compilação (ex.: `main.tex`).
- **Compilar LaTeX ao Salvar** — escolha entre `Desativado`, `Parcial` (compilação rápida
  do capítulo/arquivo em foco) ou `Completo` (recompila o documento inteiro a cada
  salvamento).
- **Raiz do Git do Usuário** — caminho opcional para a raiz de um repositório Git externo,
  caso o controle de versão fique fora da pasta do projeto.
- **Modo** — `auto`, `plan` ou `edit`.
- **Descrição** — resumo opcional do projeto fornecido ao assistente como contexto inicial.
- **Compartilhar Memória Principal** — quando ativado, todos os chats do projeto
  compartilham a mesma memória central persistente. Quando desativado, cada chat mantém
  uma memória independente.
- **Forçar Visão (Force Vision)** — força o envio de imagens/visão tanto para o modelo
  orquestrador quanto para o worker, mesmo se a detecção automática do provedor for
  conservadora.
- **Carregar Skills Contextuais** — botão que varre o diretório do projeto em busca de
  skills customizadas em `.gilzabots/skills` e as registra no projeto.

### 2. Abas Orquestrador e Worker

O projeto configura modelos e parâmetros de execução separados para o **Orquestrador**
(o agente de conversação e planejamento) e o **Worker** (o modelo dos helpers que o
`run_skill` cria). Ambas as abas oferecem:

- **Seleção de Modelo e Monitor de Hardware** — escolha entre os modelos cadastrados.
  Indicadores em tempo real de VRAM da GPU e RAM do sistema sinalizam a adequação do
  modelo (verde = roda com folga, amarelo = pode ser lento, vermelho = risco de estouro
  de VRAM).
- **Parâmetros de Amostragem e LiteLLM**:
  - `temperature` (0,0 a 2,0) — controla criatividade versus determinismo.
  - `max_tokens` — limite máximo de tokens na resposta.
  - `num_ctx` — tamanho da janela de contexto em tokens. **Essencial para modelos locais!**
  - `seed` — semente numérica para respostas reproduzíveis.
  - `top_p`, `top_k`, `min_p`, `frequency_penalty`, `presence_penalty`, `repetition_penalty`.
  - `reasoning_effort` — (`none`, `low`, `medium`, `high`, `xhigh`) para modelos de
    raciocínio com ajuste de esforço (ex.: o1, o3, Gemini, Claude).
  - `thinking` — ativa tokens de raciocínio/pensamento (para o Ollama, o GilzaBots mapeia
    automaticamente para `ollama_chat/` para raciocínio em fluxo contínuo).
  - `stream` — ativa streaming em tempo real das respostas.
- **Parâmetros de Execução do Agente**:
  - `max_heartbeats` — limite de iterações consecutivas de ferramentas por turno. Só é
    usado quando o seletor **Esforço / Orçamento** do chat está em **Personalizado**;
    Baixo, Médio e Alto enviam o próprio orçamento (20, 50, 100) a cada turno. Quando o
    orçamento acaba, o agente é consultado mais uma vez para dar a resposta, sem poder
    chamar mais ferramentas.
  - `max_context_tokens` — teto de orçamento de contexto antes da sumarização.
  - `eviction_threshold` — fração da janela (padrão `0,85`) na qual os turnos antigos
    passam a ser resumidos no resumo corrente.
  - `memory_pressure_threshold` — limiar para compactação agressiva de memória.
  - `loop_detection` e `loop_detection_limit` (padrão `3`) — detecta chamadas de
    ferramenta idênticas com os mesmos argumentos e as bloqueia antes da execução para
    interromper laços infinitos.
  - **`empty_response_reasoning_fallback`** (*Usar Raciocínio Quando a Resposta Estiver Vazia*) —
    quando ativado, caso um modelo de raciocínio concentre toda a resposta no canal de
    pensamento (thinking) e deixe o canal visível em branco, o GilzaBots publica o
    conteúdo do raciocínio como a resposta final em vez de falhar ou gastar turnos extras
    pedindo a resposta de novo.
  - `max_idle_heartbeats` — quantas vezes seguidas o modelo pode chamar `new_heartbeat`
    (manter o turno aberto sem agir) antes de o GilzaBots exigir a resposta.
  - `debug` — ativa logs detalhados de execução.
  - Limites específicos do Worker: `max_iterations` e `max_tool_calls`.

### 3. Aba Prefixo de Prompt (`prefixo`)

- **Prefixo de Prompt do Usuário (`user_prompt_prefix`)** — instruções personalizadas
  adicionadas antes da primeira mensagem do usuário em cada conclusão do chat (por exemplo,
  idioma de preferência, regras de estilo ou orientações gerais). Modifica as respostas do
  assistente sem alterar o histórico de conversas gravado nem ocupar a memória central.

## providers :: Como cadastro um provedor e um modelo?

O cadastro é um **catálogo em duas etapas**, para você digitar as credenciais uma vez só
e reaproveitá-las em todos os modelos que as compartilham.

**Etapa 1 — cadastre uma conexão de provedor** (Configurações → Editar Modelos →
Gerenciar Conexões → Adicionar):

- **Rótulo** — um nome para você, por exemplo "OpenRouter (pessoal)".
- **Provedor** — o slug de provedor do LiteLLM: `ollama`, `openai`, `gemini`,
  `anthropic`, `mistral`, `groq`, `together_ai`, `openrouter`, …
- **Chave de API** — a sua própria chave. Ela fica na sua máquina.
- **URL base da API** — só quando o provedor exige. Exemplos: Ollama local usa
  `http://localhost:11434/v1`; o Ollama Cloud usa `https://ollama.com`; o OpenRouter usa
  `https://openrouter.ai/api/v1`.

**Etapa 2 — cadastre um modelo vinculado a essa conexão** (Configurações → Editar
Modelos → Adicionar):

- **Nome** — o nome do modelo exatamente como o provedor o escreve, por exemplo
  `qwen2.5-coder:7b`, `gpt-5.5`, `gemini-2.5-pro`.
- **Conexão** — escolha a conexão que você criou; as credenciais vêm dela.
- **Capacidades** — veja o tópico "models" abaixo.

O modelo passa a aparecer no catálogo com o id `<provedor>/<nome>`. É esse id que os
projetos guardam, e ele não muda quando você edita as credenciais da conexão — então
trocar uma chave de API não quebra nenhum projeto.

Dois atalhos úteis:

- **Importar modelos locais do Ollama** — um botão em Editar Modelos consulta o Ollama
  em execução no endereço `http://127.0.0.1:11434/api/tags` e cadastra tudo que
  encontrar.
- **Apagar uma conexão que ainda tem modelos é recusado.** Isso é proposital: um projeto
  nunca pode acabar apontando para um modelo cujas credenciais não podem ser resolvidas.

## models :: Orquestrador, worker e capacidades do modelo

Um projeto usa **dois papéis de modelo**:

- **Orquestrador** — o agente principal, o modelo com quem você conversa no chat. Ele faz
  a sua tarefa: planeja, lê, faz **todas** as alterações nos seus arquivos, roda comandos e
  escreve as respostas finais. Reserve para ele o seu melhor modelo.
- **Worker** — o modelo dos **helpers**: agentes efêmeros (`run_skill`) que fazem um
  trabalho auxiliar para o agente principal — ler ou resumir um documento longo, extrair
  partes de um arquivo, condensar um log, pesquisar na web — e devolvem o que encontraram.
  Helpers nunca alteram seus arquivos. O trabalho deles é estreito, então um modelo menor
  e mais rápido serve bem e economiza tokens. Uma skill helper também pode indicar o
  próprio modelo; o chat mostra ao agente principal o modelo de cada helper e se ele
  enxerga imagens.

As skills seguem os mesmos dois tipos. A maioria é um conjunto de instruções que o agente
principal carrega (`load_skill`) e executa ele mesmo; uma skill marcada com `role: helper`
roda como helper.

Ao cadastrar um modelo no catálogo você pode declarar capacidades:

- **`supports_thinking`** — o modelo aceita um parâmetro de raciocínio (thinking). O
  GilzaBots só envia `think` quando isso está ativado. O raciocínio vem ligado por padrão
  para o orquestrador e desligado para os workers, porque um worker preso num laço de
  raciocínio trava uma chamada de ferramenta, enquanto o raciocínio do orquestrador fica
  visível e é útil para você. Para um modelo `ollama/` com thinking ativado, o GilzaBots
  passa a usar `ollama_chat/` internamente para que o raciocínio possa ser transmitido
  nativamente.
- **`requires_single_system_message`** — alguns templates de chat rejeitam uma
  requisição com mais de uma mensagem `system`, com o erro `system message must be at
  the beginning`. Isso depende do modelo, não do servidor: acontece tanto com modelos
  servidos pelo Ollama (observado com o qwen3.8) quanto com as mesmas famílias servidas
  por um endpoint compatível com OpenAI (vLLM/NIM). Ative essa opção para um modelo
  assim e o GilzaBots passa a unir todas as mensagens de sistema em uma única mensagem
  inicial — só para ele, seja qual for o provedor.
- **`prompt_profile`** (`full` ou `light`) — quanto texto de prompt o modelo recebe.
  `full` envia o prompt completo com todas as salvaguardas e exemplos; `light` envia uma
  versão condensada (mesmas regras, menos prosa) que economiza tokens e latência.
- **Trabalho auxiliar** (`direta` ou `delegada`) — quanto trabalho auxiliar este modelo,
  **quando for o orquestrador**, passa para os helpers. Em `direta` ele mesmo lê e busca,
  e só chama um helper para um trabalho grande demais para o próprio contexto. Em
  `delegada` ele manda leitura, resumo, extração, condensação de logs e pesquisa na web
  para os helpers por padrão, e reserva o próprio contexto para a sua tarefa. Nas duas, o
  orquestrador faz todas as alterações ele mesmo.
- **`force_vision`** (um parâmetro de modelo) — declara que um modelo aceita imagens quando
  o LiteLLM não o conhece (um llava ou Qwen-VL local, por exemplo). Decide se um helper
  que roda nesse modelo recebe trabalho com imagens.
- **`num_ctx`** — sobreposição opcional da janela de contexto no nível do catálogo para
  este modelo.

Perfil de prompt e trabalho auxiliar são **duas opções independentes**: a primeira diz
quanto texto o modelo lê, a segunda onde acontecem a leitura e a pesquisa. Qualquer uma
das quatro combinações é válida.

### Qual combinação usar

O padrão de um modelo novo é `full` + `direta`. Ajuste conforme os dois modelos do projeto:

| Seu cenário | Perfil de prompt | Trabalho auxiliar |
| --- | --- | --- |
| Modelo pequeno local em tudo (orquestrador e worker) | `light` | `direta` |
| Modelo grande no orquestrador, pequeno no worker | `full` | `delegada` |
| Modelo grande nos dois papéis (nuvem) | `full` | `direta` |

Por que cada linha:

- **Pequeno em tudo → prompt curto, direta.** Um modelo pequeno segue melhor um prompt
  curto, e delegar aqui só troca um modelo pequeno por outro: você paga uma ida e volta a
  mais, mais um helper que começa sem memória nenhuma, sem ganhar capacidade.
- **Grande + pequeno → prompt completo, delegada.** É onde os helpers compensam. O modelo
  grande reserva o próprio contexto — o caro, e o que você quer preservar ao longo da
  conversa — para a sua tarefa, enquanto a leitura volumosa e a pesquisa gastam os tokens
  do modelo pequeno. O modelo grande continua fazendo todas as alterações.
- **Grande em tudo → prompt completo, direta.** Contexto sobra e a qualidade não é o
  gargalo; menos idas e voltas significa resposta mais rápida.

Como o orquestrador faz todas as alterações, um orquestrador pequeno com um worker grande
não passa mais a edição para o modelo mais forte: coloque o modelo mais forte no
orquestrador. `delegada` custa uma chamada de modelo a mais por trabalho auxiliar; se as
respostas parecerem lentas, é a primeira opção a rever.

A opção fica em **Editar Modelos**, no formulário do modelo, logo abaixo do perfil de
prompt, e aparece como coluna na lista de modelos.

## settings :: Quais configurações eu devo usar?

**Janela de contexto (`num_ctx`)** — esta é a configuração mais importante para modelos
locals. O número que você define é o limite real, independentemente do que o modelo
anuncia: um "modelo de 128K" roda com o `num_ctx` que o seu projeto definir. Dimensione
conforme a sua VRAM (veja o tópico sobre modelos locais). O indicador de contexto do chat
mede contra esse valor, mostra quanto da janela foi consumido e reporta os tokens que o
provedor realmente cobrou, não uma estimativa.

**Modo** — comece em **auto** no dia a dia. Use **plan** quando quiser revisar o que o
assistente pretende fazer antes de ele tocar em qualquer coisa, e **edit** para
correções rápidas e pontuais.

**Streaming** — deixe ligado. Você vê a resposta sendo produzida e pode interromper uma
execução que está indo pelo caminho errado.

**Limite de detecção de laço** — padrão `3`. Quando o assistente repete a mesma chamada
de ferramenta com os mesmos argumentos essa quantidade de vezes, a repetição é bloqueada
antes de executar e o modelo é orientado a mudar de abordagem. Reduza se um modelo local
pequeno tende a girar em falso; aumente apenas se você tiver um fluxo legitimamente
repetitivo.

**Limiar de evicção** — padrão `0,85`. Os turnos mais antigos começam a ser resumidos e
retirados do contexto de trabalho quando a janela chega a 85% de ocupação, em vez de
esperar o estouro.

**Thinking e Fallback de Raciocínio** — deixe o thinking desligado a menos que a
documentação do modelo confirme o suporte. Se você usa modelos de raciocínio locais (como
DeepSeek-R1 ou variantes Qwen Thinking) e notar que a resposta às vezes fica em branco,
ative `empty_response_reasoning_fallback` nas Configurações do Projeto.

**Skills** — ative só o que você precisa. Cada skill ativa acrescenta a sua descrição ao
prompt de sistema, o que custa contexto em todos os turnos.

### Configurações Globais do IDE (ícone de engrenagem no canto inferior esquerdo)

- **Aparência e Editor** — escala da interface (UI Scale), tema claro/escuro, tamanho da
  fonte, tamanho da tabulação, quebra de linha e minimapa.
- **Ferramentas Externas** — instalador e verificador de status com um clique para
  Tectonic e Pandoc.
- **Produtividade e Mídia**:
  - Idioma de destino para tradução de trechos selecionados.
  - Leitura em voz alta / TTS com vozes locais Piper ou OpenAI.
  - Ditado de áudio por voz com modelos Whisper locais.
  - Configurações de modelo e pasta para geração de imagens (`generate_image`).
- **Sandbox Docker (Experimental)** — executa comandos e scripts do assistente dentro
  de um contêiner Docker isolado em vez do sistema operacional hospedeiro. Por ser um
  recurso **experimental**, vem desativado por padrão.

## asset-store :: Como funcionam a Asset Store, as skills e os templates?

A **Asset Store** (ícone de Loja na barra de atividades) é a central para estender o
GilzaBots com novas capacidades e modelos de documentos LaTeX.

### 1. Skills (aba `Skills`)

Skills são pacotes de instruções em markdown (com scripts opcionais) que ensinam tarefas
especializadas à IA.

- **Sub-aba Catálogo** — navegue e instale skills do repositório no seu projeto
  (`<projeto>/.gilzabots/skills/<nome>/`).
- **Sub-aba Ativas no projeto** — ative ou desative skills para o projeto atual.
- **Cópias locais e atualizações** — quando uma skill local é instalada, ela substitui a
  versão embutida de mesmo nome. Se o catálogo for atualizado, a Asset Store detecta a
  diferença e exibe o botão **"Atualizar cópia local"**. Para voltar à versão que vem de
  fábrica com o GilzaBots, basta clicar em **"Restaurar embutida"**.
- **Skills que precisam de outras skills** — algumas skills carregam outras enquanto
  trabalham. Instalar uma delas também instala e ativa as que ela precisa (uma cópia
  que você já tenha fica como está).
- **Duas formas de uma skill rodar** — o agente escolhe a skill sozinho quando o seu
  pedido combina com a descrição dela, ou você a roda digitando **`/nome`** no chat,
  seguido do que ela deve trabalhar (`/grill-with-docs meu plano`). Digitar `/` lista os
  comandos e, marcadas como **skill**, as skills que você pode rodar. Algumas skills só
  rodam assim: o agente nem as vê até você digitá-las. Veja o tópico das skills de
  engenharia, logo abaixo.

### 2. Templates LaTeX (aba `Templates LaTeX`)

Templates fornecem estruturas completas de documentos prontos para uso (artigos, teses,
livros, apresentações Beamer, currículos).

- **Instalação em um clique** — os templates são descompactados diretamente na raiz do
  seu projeto.
- **Detecção de conflitos e segurança** — se algum arquivo do template já existir no seu
  projeto, o GilzaBots detecta o conflito, avisa você, lista os arquivos concorrentes e
  pede confirmação explícita antes de sobrescrever.

## skill-pack :: Como uso as skills de engenharia (grill-with-docs, tdd, code-review…)?

A Asset Store traz um pacote de 26 skills para planejar e construir software, adaptadas
das skills de Matt Pocock (licença MIT). Elas levam você de uma ideia até código testado
e revisado, em passos pequenos e deliberados.

### Instalando

- **Na janela:** Asset Store → Skills → Catálogo → instale a skill. As skills de que ela
  precisa vêm junto, e todas são ativadas no projeto.
- **No terminal:** `/load_asset skill grill-with-docs` instala a skill e o que ela
  precisa; depois ative cada uma com `/addskill` (aqui: `grilling`, `domain-modeling`,
  `grill-with-docs`).

As skills instaladas ficam em `<projeto>/.gilzabots/skills/`. `/skills` lista todas.

### Rodando uma skill

Digite `/` no chat, escolha a skill e escreva depois do nome o que ela deve trabalhar:

```
/grill-with-docs quero exportar os chats para PDF
```

A linha que você digitou fica no chat; o agente recebe as instruções da skill junto com
o seu texto. Algumas skills (as que *conduzem* um fluxo, como `grill-with-docs`,
`to-spec` ou `implement`) rodam **só** quando você as digita. Outras (`tdd`,
`code-review`, `diagnosing-bugs`, `prototype`, `research`…) também podem ser escolhidas
pelo próprio agente quando o pedido combina, e os fluxos acima as carregam no caminho.

Se não souber qual usar, rode **`/which-skill o que você quer fazer`**: ela indica o
fluxo e avisa quais skills ainda faltam instalar.

### O fluxo principal: ideia → código entregue

1. **`/grill-with-docs <ideia>`** — uma entrevista em rodadas. Cada rodada traz
   perguntas numeradas com uma resposta recomendada (➡️); responda todas de uma vez
   ("1 sim, 2 não, prefiro X"). O agente procura sozinho os fatos no código e na web e só
   pergunta a você o que é decisão. No caminho, registra os termos do projeto no
   `GLOSSARY.md` e oferece registrar decisões difíceis de reverter como ADRs em
   `docs/adr/`. Termina quando não sobra pergunta e confirma com você antes de agir.
2. **`/to-spec`** — transforma a conversa numa especificação (sem novas perguntas).
3. **`/to-tickets`** — divide em tickets pequenos, cada um dizendo quais o bloqueiam.
4. **`/implement`** — constrói com testes primeiro (`tdd`) e fecha com uma revisão em
   dois eixos (`code-review`: os padrões do projeto e a especificação), depois faz o
   commit.
5. **`/retro`** — ao final, sugere melhorias no ambiente do agente (verificações,
   `AGENTS.md`, padrões de código).

Mantenha os passos 1–3 **no mesmo chat**, para a especificação e os tickets partirem do
mesmo raciocínio. Antes do primeiro `/to-spec`, rode uma vez
**`/setup-engineering-skills`**: ela pergunta onde ficam os tickets (GitHub, GitLab ou
arquivos locais em `.scratch/`) e registra isso em `docs/agents/` e no `AGENTS.md`.

### As outras skills

- **Entrevistas:** `/grill-me` (a mesma entrevista, sem gravar arquivos), `grilling` (a
  rotina de entrevista que as outras usam), `/to-questionnaire` (um questionário para
  quem sabe o que você não sabe), `/wait-what` (reexplica a última mensagem com
  palavras simples).
- **Construir e corrigir:** `tdd`, `diagnosing-bugs` (bugs difíceis: primeiro monta uma
  reprodução que falha, depois corrige com um teste de regressão), `code-review`,
  `prototype` (código descartável que responde a uma dúvida de design), `pr` (o formato
  da descrição de um pull request), `wizard` (um script que guia *você* pelos passos que
  só uma pessoa pode fazer, como criar chaves de API).
- **Planejar trabalho grande:** `/wayfinder` (um mapa de tickets de decisão para
  trabalho grande demais para uma sessão), `/triage` (organiza issues que chegam e
  escreve briefings que um agente consegue executar), `/improve-codebase-architecture`
  (um relatório visual de oportunidades de refatoração).
- **Conhecimento:** `research` (lê fontes primárias em segundo plano e salva uma nota
  com citações), `domain-modeling` (o glossário e os ADRs), `codebase-design`
  (vocabulário para projetar módulos), `/teach` (aprender um assunto ao longo de várias
  sessões), `/handoff` (um resumo para outra sessão ou outro agente),
  `writing-for-agents` (como escrever skills e o `AGENTS.md`).

### Bom saber

- O **`AGENTS.md`** na raiz do projeto é lido pelo agente a cada turno: coloque nele as
  instruções permanentes do projeto (como rodar os testes, convenções).
- Várias skills iniciam **subagentes**: agentes auxiliares que só leem (arquivos, Git, a
  web) e devolvem um relatório, vários ao mesmo tempo. Eles nunca alteram seus arquivos;
  o log de atividade os mostra como `subagent:…`.
- Quando um fluxo diz para começar do zero, abra um **novo chat** (Novo Chat no painel
  de chat, ou `/chat new <nome>`). **Não** use `/clear`: no GilzaBots ele apaga todos os
  chats do projeto.
- Se uma skill responder "installed but not active", ative-a com `/addskill <nome>` ou na
  aba *Ativas no projeto* da Asset Store.

## local-models :: Dicas para modelos locais (Ollama)

Rodar localmente é gratuito e privado, e funciona bem quando você respeita os limites.

- **Ollama 0.30.5 ou mais recente.** Versões anteriores não suportam chamadas de
  ferramenta corretamente, e é por meio delas que o assistente lê e edita seus arquivos.
- **Escolha um modelo que realmente suporte tool calling.** Um modelo sem isso conversa,
  mas não consegue fazer nada no seu projeto. Qwen2.5-Coder e famílias equivalentes com
  suporte a ferramentas são a escolha segura.
- **Dimensione o `num_ctx` pela sua VRAM, não pela ambição.** Um contexto que não cabe na
  VRAM transborda para a RAM do sistema e o modelo fica inutilizavelmente lento. Pontos
  de partida aproximados: ~8 GB de VRAM → modelo de 7B com contexto de 8K–16K; ~12–16 GB
  → modelo de 7B–14B com 16K–32K; menos de 6 GB → modelo de 3B, e encare isso como uma
  forma de conhecer a interface, não de fazer trabalho de verdade.
- **Use um modelo worker menor que o orquestrador.** Workers são executores de
  ferramentas; não precisam da qualidade de raciocínio que você quer no chat.
- **Com modelos pequenos nos dois papéis, use perfil `light` e escrita `direta`.** O
  prompt curto é mais fácil de seguir, e delegar entre dois modelos pequenos acrescenta
  uma ida e volta sem acrescentar capacidade. Se o orquestrador for um modelo grande e o
  worker pequeno, a recomendação se inverte: `full` + `delegada`. Veja a tabela no tópico
  sobre modelos.
- **Mantenha o thinking desligado nos workers.** Um modelo local de raciocínio, diante de
  uma entrada complexa, pode ficar em laço por muito tempo dentro de uma chamada de
  ferramenta.
- **Ative `empty_response_reasoning_fallback`** se o seu modelo local de raciocínio
  escrever a resposta no canal de pensamento em vez do canal de resposta visível.
- **Mantenha poucas skills ativas.** Cada uma custa contexto em todos os turnos.
- **Seja realista.** Modelos locais pequenos são ótimos para edições curtas, formatação e
  para aprender a interface. Eles sofrem com documentos longos, grandes volumes de dados
  e refatorações de vários passos — que é o assunto do tópico sobre modelos em nuvem.

## local-skills :: Quais skills combinam com modelos locais pequenos?

Skills são arquivos markdown de instruções que ensinam ao assistente uma tarefa
especializada. A maioria são instruções que o assistente carrega e segue ele mesmo;
algumas (`log-table-condenser`, `staged-reader`, `elementary-ops`, `web-search`) são
helpers para os quais ele passa um trabalho auxiliar com `run_skill`, que cria um agente
novo e sem memória que devolve o que encontrou. Para um modelo local pequeno, prefira skills que transformem um trabalho
grande em um **script**, e não em uma conversa longa:

- **`command-line`** — a mais útil de todas. Comandos de terminal, scripts de build,
  trechos de Python, operações em lote sobre arquivos. Quando a tarefa é grande, um
  modelo pequeno se sai muito melhor escrevendo um script Python de dez linhas e
  executando-o do que raciocinando sobre os dados.
- **`log-table-condenser`** — para logs e tabelas grandes (`.jsonl`, `.csv`, `.tsv`,
  `.log`). Ela processa e condensa o arquivo em fluxo, em vez de carregá-lo na janela de
  contexto. Use sempre que um arquivo de dados tiver mais que algumas centenas de linhas.
- **`latex-assistant`** — explica erros do compilador e gera fragmentos de LaTeX e
  matemática. São prompts curtos e bem delimitados, exatamente o que modelos pequenos
  fazem bem.

Saber o que está aberto no editor não exige skill alguma: o assistente já enxerga as abas
abertas, o arquivo em foco e o texto selecionado por meio da ferramenta nativa
`get_editor_state`. Pode dizer "este arquivo" ou "o trecho selecionado" à vontade.

Regras práticas para modelos pequenos:

- ative duas ou três skills, não todas;
- prefira edições cirúrgicas (`search_code` → `read_content_pos` →
  `edit_file` ou `replace_content_range`) a pedir um arquivo inteiro reescrito — um JSON de chamada de
  ferramenta muito longo é truncado pelo limite de saída;
- se a mesma skill falhar duas vezes seguidas, pare e mude de abordagem; o GilzaBots
  detecta isso e orienta o assistente a parar de redelegar.

## tools-features :: Quais ferramentas e recursos estão disponíveis (documentos, busca web, perguntas)?

Além da edição de código LaTeX, o GilzaBots inclui recursos e ferramentas nativas
avançadas:

- **Exportadores Nativos de Documentos (`create_docx_file`, `create_pptx_file`)** — o
  assistente pode gerar arquivos formatados do Microsoft Word (`.docx`) e apresentações do
  PowerPoint (`.pptx`) com cabeçalhos estruturados, tabelas, tópicos e imagens embutidas.
- **Pesquisa na Web ao Vivo** — o assistente pode pesquisar na web em tempo real para
  consultar documentação de pacotes no CTAN, soluções para erros de compilação, citações
  BibTeX e sintaxes de bibliotecas.
- **Perguntas Interativas ao Usuário (`ask_question`)** — quando o assistente precisa de
  esclarecimentos, escolhas de formato ou confirmação de opções, ele abre uma janela
  interativa (`AskModal`) na interface, permitindo que você responda sem quebrar o fluxo.
- **Confirmações de Segurança** — ações potencialmente destrutivas ou execuções de planos
  exigem confirmação explícita antes de prosseguir.
- **Edição Cirúrgica de Texto** — `search_code`, `read_content_pos`, `edit_file`,
  `replace_content_range` e `write_content_pos` permitem localizar e editar trechos exatos
  sem precisar ler nem reescrever arquivos imensos. O `edit_file` encontra o trecho pelo
  texto exato, então edições anteriores não o deslocam. Essas três últimas só editam arquivo
  já existente; arquivo novo é sempre criado com `write_file`.
- **Anexos e Entrada Multimodal** — arraste e solte imagens, PDFs, textos ou arquivos de
  dados no chat. O GilzaBots extrai o texto dos documentos ou repassa as imagens a modelos
  com capacidade de visão.
- **Geração de Imagens por IA (`generate_image`)** — gera figuras, ilustrações e
  diagramas a partir de descrições textuais diretamente na pasta do projeto (ex.: `figures/`).
- **Ações Rápidas na Seleção de Texto** — selecione qualquer trecho no PDF ou no preview
  para traduzir, ouvir a pronúncia em voz alta ou consultar a IA com um card explicativo.
- **Ditado por Voz** — dite instruções diretamente na caixa do chat usando reconhecimento
  de voz local com Whisper.
- **Painel de Atividades e Pensamentos** — blocos expansíveis "AI Thoughts" nas
  mensagens permitem inspecionar o raciocínio da IA sem poluir o texto final.
- **Sandbox Docker (Experimental)** — execução opcional e isolada de comandos e scripts
  dentro de um contêiner Docker montado na pasta do seu projeto.
- **Agentes Monitores (Experimental)** — observadores em segundo plano que avisam você, ou
  acordam um chat para o assistente retomar uma tarefa, quando algo acontece — mesmo com a
  janela fechada. Veja *O que são os agentes monitores e como usá-los?*
- **Conectores (MCP)** — deixam o assistente usar serviços externos (uma rede social, um
  rastreador de issues, um servidor local) por meio de servidores MCP que você conecta. Veja
  *Como conecto serviços externos (conectores MCP)?*

## docker-sandbox :: O que é o sandbox Docker e como ele funciona?

> **IMPORTANTE: O sandbox Docker é atualmente um recurso EXPERIMENTAL.** Ele vem
> desativado por padrão e é voltado para quem deseja que os comandos e scripts executados
> pela IA rodem dentro de um contêiner em vez de diretamente na sua máquina.

### O que ele faz

Quando ativado, os comandos de terminal do assistente (`run_command`), scripts Python
(`run_python_script`), comandos interativos (`run_interactive_command`) e comandos em
segundo plano (`run_background_command`) rodam dentro de um **contêiner Docker**, em vez
de executar diretamente no sistema operacional hospedeiro. Isso vale tanto para o agente
**Orquestrador** quanto para os sub-agentes **Worker**. O seu próprio terminal integrado
continua rodando na sua máquina.

Principais características:

- **O que fica isolado** — os processos que o assistente inicia. A pasta do projeto é
  montada com leitura e escrita em `/workspace`, então arquivos criados ou modificados ali
  refletem diretamente no seu projeto, e as ferramentas de arquivo do assistente continuam
  editando o projeto na sua máquina como antes. O sandbox não protege a pasta do projeto
  em si.
- **Seus arquivos continuam seus** — no Linux, os comandos rodam com o seu usuário e
  grupo, então os arquivos que eles criam no projeto pertencem a você (com Docker rootless
  ou no Windows/macOS o usuário da imagem é mantido, e o Docker já o mapeia para você).
- **Ambiente persistente** — o GilzaBots mantém um contêiner persistente por projeto
  (`gilzabots-sandbox-<hash>`). Pacotes instalados com `pip` (no diretório do usuário, já
  que os comandos não rodam como root) continuam disponíveis nos turnos seguintes até o
  contêiner ser recriado. Execute as ferramentas instaladas com `python3 -m <ferramenta>`.
  Pacotes do sistema (`apt-get`) exigem uma imagem personalizada que já os contenha.
- **Modos de rede** — `bridge` (padrão, permite baixar pacotes e acessar APIs externas)
  ou `none` (sem acesso à internet).
- **Skills** — as pastas de skills fora do projeto são montadas somente leitura, então os
  scripts das skills continuam funcionando. Scripts em qualquer outro lugar fora do projeto
  são recusados: copie-os para o projeto antes.
- **Ferramentas que não estão na imagem** — Tectonic e Pandoc não ficam dentro do
  contêiner; a compilação e a exportação da própria IDE continuam usando os da sua máquina.

### Como configurar

Abra as **Configurações** (ícone de engrenagem no canto inferior esquerdo) e role até a
seção **Sandbox Docker** em Preferências:

1. **Ativar sandbox** — marque "Executar comandos do agente em sandbox Docker".
2. **Imagem Docker** — escolha a imagem (o padrão é `python:3.12-slim`). Se precisar de
   ferramentas específicas (como TeX Live completo ou compiladores), você pode indicar uma
   imagem personalizada.
3. **Rede** — escolha `bridge` ou `none`.
4. **Verificação e status**:
   - Clique em **Verificar Docker** para testar se a CLI do Docker está disponível, se o
     daemon está respondendo, se as permissões estão corretas e se a imagem já foi baixada.
   - Se a imagem ainda não tiver sido baixada, clique em **Baixar Imagem Docker**. O
     sandbox nunca a baixa sozinho no meio de um comando.
   - Trocar a imagem ou a rede recria o contêiner no próximo comando, descartando o que
     foi instalado nele.
   - Se o contêiner estiver em mau estado, ou um comando deixou algo rodando nele, clique
     em **Recriar Sandbox do Projeto** para recriá-lo do zero.

### Requisitos e resolução de problemas

- **Docker Engine / Desktop** precisa estar instalado e em execução na máquina:
  - No Linux: seu usuário precisa estar no grupo `docker`
    (`sudo usermod -aG docker $USER`). **O grupo só passa a valer em uma nova sessão de
    login**: saia da sessão e entre de novo (ou reinicie o computador). Reiniciar apenas o
    GilzaBots não basta, porque ele herda os grupos da sessão em que foi aberto.
  - No Windows/macOS: garanta que o Docker Desktop está em execução e integrado ao WSL2.

Como esse recurso é **experimental**, a execução local padrão continua sendo o padrão.
Se encontrar problemas de permissão ou não necessitar de isolamento em contêiner, você pode
mantê-lo tranquilamente desativado.

## monitors :: O que são os agentes monitores e como usá-los?

> **IMPORTANTE: Os agentes monitores são atualmente um recurso EXPERIMENTAL.** Eles
> funcionam no Linux (testados no Ubuntu/GNOME), mas as integrações com Windows e macOS
> (notificações, início no login) ainda não foram testadas em máquinas reais, e um
> despertar que retoma uma tarefa com um modelo real ainda não foi verificado de ponta a
> ponta. Espere arestas, acompanhe o que seus monitores fazem e relate problemas.

Um **monitor** observa algo em segundo plano e age quando uma condição é atendida: mostra
uma **notificação na área de trabalho**, **acorda um chat** para o assistente continuar
uma tarefa, ou os dois. Os monitores continuam rodando com a janela do GilzaBots fechada.

### Duas formas de usar

- **Pedir para ser avisado** — basta dizer no chat: *"me avise sempre que o dólar subir
  mais de 10 centavos em relação ao real"*, *"avise quando esta página mudar"*,
  *"me lembre daqui a duas horas de revisar o capítulo 3"*. O assistente cadastra um
  monitor que notifica você a cada disparo (ou uma única vez, se você pedir um só aviso).
- **Deixar o assistente esperar em vez de ficar consultando** — quando uma tarefa depende
  de algo que termina depois (uma compilação longa, um download, um comando iniciado com
  `run_background_command`), o assistente pode cadastrar um monitor, dizer o que está
  esperando e encerrar o turno. Quando a condição é atendida, o chat é acordado com uma
  mensagem **⏰ Monitor** e o assistente continua de onde parou. Se a espera se esgotar
  (verificações demais ou prazo vencido), o chat é acordado mesmo assim, com o motivo,
  para o assistente decidir se espera mais ou pergunta a você se cancela.

**Qual chat é acordado.** Cada monitor que acorda o assistente faz isso de um de dois jeitos:

- **no chat em que foi criado** (o padrão) — o assistente continua com aquela conversa e
  tudo o que lembra dela. É a escolha certa para esperar uma tarefa.
- **num chat novo a cada vez** — cada despertar abre um chat novo no mesmo projeto
  (chamado *⏰ nome do monitor · data e hora*), e a conversa original fica intacta. Ele
  trabalha **em segundo plano**: a janela nunca sai do que você está fazendo. Ao terminar,
  chega uma notificação, o ícone de radar mostra um contador e o painel de Monitores lista o
  evento em *Eventos recentes* ("Novo evento do monitor …") com **Abrir chat**. Serve
  para trabalho recorrente, como *"toda vez que o dólar variar, escreva uma análise curta"*.
  O chat novo começa vazio, então as instruções do monitor precisam dizer tudo o que o
  assistente precisa saber.

**Uma vez ou sempre.** Um monitor dispara toda vez que a condição é atendida, a menos que
você peça uma única vez (**Disparar só uma vez** no formulário, ou diga no chat). Se você não
disser nada, um monitor que acorda o chat em que foi criado dispara uma vez (ele está
esperando uma tarefa), e os demais continuam disparando.

Peça com suas palavras (*"…e abra um chat novo a cada vez"*) ou escolha no campo
**Acordar um chat** do formulário de Novo monitor. Se o chat que o monitor deveria acordar
foi apagado, o despertar falha com uma notificação em vez de ir para outro chat.

### O que um monitor pode verificar

- **Endereço web (HTTP)** — lê uma página ou uma API JSON; um caminho JSON como
  `USDBRL.bid` ou uma expressão regular extrai o valor. É a opção barata e confiável para
  cotações e endpoints de status.
- **Comando** — roda um comando na pasta do projeto (dentro do sandbox Docker, quando ele
  está ativado) e olha a saída ou o código de saída.
- **Arquivo** — se um arquivo existe, ou se a data de modificação ou o conteúdo mudou.
- **Temporizador** — nenhuma verificação: simplesmente dispara depois do tempo definido.
- **Pergunta ao modelo** — o modelo worker do projeto responde a uma pergunta, com busca
  na web. Use só quando a condição exige julgamento ("só se a notícia envolver IA"): cada
  verificação é uma chamada ao modelo que gasta tokens, por isso o intervalo mínimo é de
  5 minutos.

As condições incluem *mudar*, *variar* (ex.: ±0,10 desde o último aviso), *maior / menor
que*, *igual a*, *contiver*, *casar com o padrão*, *código de saída for*, *existir / não
existir*. A primeira verificação roda no cadastro: se o endereço não responde ou o caminho
JSON não encontra nada, você recebe o erro na hora, em vez de um monitor que falha em
silêncio.

### Onde ele roda

Os monitores rodam num pequeno serviço em segundo plano, `gilzabots monitors daemon`,
separado da janela. **Iniciar no login** vem ligado por padrão para seus monitores
sobreviverem a uma reinicialização (um serviço `systemd --user` no Linux, a chave `Run`
do usuário no Windows); você
pode desligar no painel de Monitores. O serviço para sozinho depois de um tempo quando não
há mais nada para observar.

- **Ícone na bandeja** — enquanto o serviço roda, um ícone do GilzaBots na bandeja do
  sistema mostra que ele está trabalhando em segundo plano, mesmo com a janela fechada
  (fechar a janela continua fechando a janela). O menu mostra os monitores ativos e os
  eventos novos (um ponto vermelho marca os não vistos) e oferece **Abrir o GilzaBots**,
  **Painel de Monitores…**, **Pausar todos os monitores** / **Retomar monitores** e
  **Encerrar o serviço em segundo plano** (os monitores então esperam até o GilzaBots ser
  aberto ou você entrar na sessão de novo). No Ubuntu ele usa a extensão *Ubuntu
  AppIndicators*, ligada por padrão; o GNOME puro precisa da extensão AppIndicator.
  Desligue em **Ícone na bandeja** no painel de Monitores ou com `gilzabots monitors tray off`.
- **Notificações** — as nativas: a central de notificações do GNOME/KDE no Linux, toasts
  no Windows, a Central de Notificações no macOS. Clicar numa notificação (no Linux e no
  Windows) abre o GilzaBots no chat a que ela pertence.
- **Com a janela aberta** naquele projeto, o despertar roda ali mesmo, como um turno normal
  com streaming. Caso contrário, roda em segundo plano e uma notificação traz a resposta.
- **Tudo acontece no chat** — o turno de um monitor trabalha sozinho e nunca abre pergunta
  ou pedido de aprovação fora do seu chat (o novo ou o original, conforme você escolheu).
  Se precisar de algo de você — uma resposta, ou aprovação para uma ação que o modo `edit`
  perguntaria —, ele diz isso na resposta naquele chat, e você responde ali. Nada é aprovado
  por você. Por isso, ao pedir um monitor, dê ao assistente detalhes suficientes (o que
  procurar, onde, como relatar) para ele terminar sem perguntar.
- **Um turno por chat** — enquanto um turno em segundo plano roda num chat, uma nova
  mensagem para esse mesmo chat é recusada até ele terminar.

### Gerenciando os monitores

- **Painel de Monitores** — o ícone de radar na barra de atividades à esquerda (um contador
  mostra os eventos que você ainda não viu). Mostra os *Eventos recentes* com **Abrir chat** (**Limpar** esvazia essa lista; os chats
  continuam) e lista todos os monitores com tipo (*Espera de
  tarefa* ou *Observação*), gatilho, última verificação, último valor e erros, com
  **Pausar/Retomar**, **Testar** (verifica agora sem alterar nada), **Cancelar/Remover**,
  o histórico de cada monitor, um formulário de **Novo monitor**, o estado do serviço,
  **Iniciar no login**, **Ícone na bandeja** e **Testar notificação**.
- **No chat** — peça ao assistente para listar, pausar ou cancelar monitores
  (`list_monitors`, `pause_monitor`, `cancel_monitor`).
- **No terminal** — `gilzabots monitors list`, `info <id>`, `pause <id>`, `resume <id>`,
  `cancel <id>`, `test <id>`, `daemon --status`,
  `service status|install|uninstall`, `tray status|on|off`, `notify-test`; `/monitors` faz o mesmo dentro do
  `gilzabots --cli`.

### Cuidados

- Cadastrar um monitor conta como uma ação: no modo **edit** o assistente pergunta antes
  de criar um, e no modo **plan** ele não pode. Um monitor de comando executa o comando
  repetidamente sem ninguém olhando — leia-o antes de aprovar.
- Um monitor que falha cinco verificações seguidas para e avisa você.
- Se as notificações não aparecem, use **Testar notificação** (ou
  `gilzabots monitors notify-test`) e confira `gilzabots monitors daemon --status`. O
  serviço grava um log em `<pasta de dados do GilzaBots>/monitors/daemon.log`.

## connectors :: Como conecto serviços externos (conectores MCP)?

Um **conector** liga o GilzaBots a um serviço externo por meio de um **servidor MCP** (Model
Context Protocol). As ferramentas que o servidor oferece — publicar um post, ler issues,
buscar notas — passam a ser ferramentas que o assistente pode usar no chat.

**Pelo catálogo.** Abra **Conectores** (ícone de cabo na barra de atividades) → **Adicionar
conector**. O **catálogo** no topo tem entradas prontas: escolha uma, preencha o que ela pede e
salve.

- **Notion, Linear, Sentry** — OAuth: o navegador abre para você entrar assim que salvar.
- **GitHub, X (somente leitura)** — pedem um token (um personal access token do GitHub, ou o
  bearer token do seu app no X).
- **Documentação do X, Hugging Face, DeepWiki, Context7** — abertos, nada para preencher.
- **Arquivos (uma pasta local), Fetch** — rodam no seu computador; precisam do Node.js (`npx`)
  ou do uv (`uvx`).

**Manualmente**, para qualquer outro servidor:

- **URL remota ou local** — o endereço do servidor (`https://…/mcp`; `http://` simples só para
  `localhost`). Escolha a autenticação que o serviço usa: **OAuth** (você entra pelo
  navegador, como ao autorizar o Google Drive), um **token bearer**, ou nenhuma. Com OAuth, o
  GilzaBots se registra sozinho quando o serviço permite; se o serviço exigir um app
  registrado por você, informe o **client id** desse app e, se quiser, os **escopos** a pedir.
- **Comando local** — um servidor que o GilzaBots inicia no seu computador (por exemplo `npx`
  com seus argumentos, um por linha). Ele roda com as suas permissões: use apenas comandos
  em que você confia.

Depois clique em **Conectar** (conectores OAuth conectam ao salvar). O conector aparece como
*conectado* e lista as ferramentas. Em **Ferramentas que o agente pode usar** você pode manter
só as que quiser — um servidor pode oferecer centenas.

**Aprovação.** Por padrão (**Sempre perguntar**), cada chamada mostra um pedido de aprovação
com os argumentos exatos — o texto de um post, o destinatário de uma mensagem — mesmo no modo
auto. Com **Seguir o modo do chat**, as chamadas se comportam como as ferramentas nativas
(auto executa, edit pergunta, plan bloqueia). Turnos de monitores em segundo plano nunca
podem usar um conector em *Sempre perguntar*.

### Sua conta do X

O X só aceita login por meio de um app registrado nele e cobra o uso da API do dono do app,
por isso o GilzaBots não traz um: você usa o seu (e paga o seu próprio uso).

1. Em **developer.x.com**, crie um projeto e um app, e defina um limite de gastos.
2. Em **User authentication settings** do app: OAuth 2.0 ativado, tipo **Native App**,
   permissões **Read and write**, e estas três Callback URLs:
   `http://127.0.0.1:47821/oauth/callback`, `http://127.0.0.1:47822/oauth/callback`,
   `http://127.0.0.1:47823/oauth/callback`.
3. Copie o **OAuth 2.0 Client ID** (um Native App não tem secret).
4. No GilzaBots, **Adicionar conector** manualmente: URL `https://api.x.com/mcp`, autenticação
   **OAuth**, o seu **client id**, escopos `tweet.read tweet.write users.read offline.access`,
   aprovação **Sempre perguntar**. Salve e entre no X pelo navegador.

As regras do próprio X continuam valendo: nada de likes ou follows automatizados, e respostas
geradas por IA precisam de aprovação do X; usar um servidor MCP não muda isso. Para só ler, a
entrada **X (somente leitura)** do catálogo usa o bearer token do seu app.

**Bom saber.**

- Descrições e resultados das ferramentas vêm do servidor; o assistente os trata como dados.
- Se um conector disser que precisa de autorização, abra Conectores e clique em **Conectar**
  de novo. **Sair** apaga as credenciais salvas.
- No terminal: `/connectors` (veja `/help /connectors`).

## cloud-for-big-data :: Quando devo usar modelos em nuvem?

**Quando o seu problema envolve grande quantidade de dados, use um modelo em nuvem.**
Este é o conselho mais útil de todo este guia.

Documentos grandes, logs longos, bases de dados extensas, refatorações do projeto inteiro
e tarefas longas de vários passos exigem uma janela de contexto grande e um modelo que
acerte as chamadas de ferramenta de primeira. Um modelo local pequeno nessa situação vai
encher a janela, começar a descartar a conversa, produzir chamadas de ferramenta
malformadas e consumir muito mais do seu tempo do que a chamada de API teria custado.

Duas boas opções, ambas configuradas exatamente como qualquer outro provedor:

- **Ollama Cloud** — o mesmo Ollama que você já conhece, com os modelos rodando na
  infraestrutura deles. Cadastre uma conexão com provedor `ollama`, URL base
  `https://ollama.com` e a sua chave; os nomes dos modelos trazem o sufixo `:cloud`. O
  GilzaBots reconhece esses ids e aplica uma janela de 65K por padrão. É o menor degrau se
  você já usa o Ollama local.
- **Provedores de API** — o **OpenRouter** dá uma única chave e uma única conexão para
  modelos de vários fabricantes, o que facilita trocar de modelo sem recadastrar
  credenciais (slug de provedor `openrouter`, URL base `https://openrouter.ai/api/v1`).
  Gemini, OpenAI, Anthropic, Mistral, Groq e Together AI também são suportados
  diretamente.

Um híbrido prático que funciona bem: mantenha um modelo local como **worker** para os
passos mecânicos baratos e use um modelo em nuvem como **orquestrador** para o
raciocínio. Você pode trocar qualquer um dos dois pela barra do chat a qualquer momento,
sem mexer no projeto.

## context :: Como funciona a janela de contexto aqui?

O cabeçalho do chat mostra um indicador de contexto. Ele reporta os tokens que o provedor
realmente cobrou pela requisição — não um palpite por contagem de caracteres — e mede
contra a janela efetiva (`num_ctx`), que é o limite real independentemente do que o
modelo anuncia. O número indica quanto foi **consumido**, e a barra esvazia conforme a
janela enche.

Dois comportamentos decorrem disso, e ambos são intencionais:

- **`read_file` recusa um arquivo que não cabe no orçamento restante.** Ele informa o
  tamanho do arquivo, o orçamento que sobrou e o caminho de paginação a usar no lugar.
  Uma leitura grande demais é irrecuperável: ela entraria no histórico, o provedor
  truncaria a requisição pela frente e o assistente acabaria respondendo a uma pergunta
  que já não consegue mais ver.
- **`read_content_pos` pagina um arquivo grande** e sempre avisa quando devolve menos do
  que você pediu, incluindo a chamada exata para continuar. A receita normal para um
  arquivo grande é `search_code` para achar os números de linha e depois
  `read_content_pos` só naquele trecho.

Quando a janela enche mesmo assim, os turnos mais antigos são resumidos em um resumo
corrente em vez de simplesmente descartados — e o turno em andamento nunca é removido.

## compile-git :: Compilação, PDF e revisão das alterações

**Compilação** — o Tectonic compila localmente; não é preciso instalar LaTeX no sistema.
Você tem compilação completa, compilação parcial (um capítulo ou arquivo) e um rascunho
rápido de passe único para conferências ligeiras. Erros de compilação podem ser passados
ao assistente ou corrigidos direto pelo painel de problemas.

**PDF e SyncTeX** — a pré-visualização fica ao lado do fonte, e o SyncTeX mapeia um ponto
do PDF de volta para a linha `.tex` que o gerou.

**Checkpoints e modo Review** — todo turno do agente é envolvido por checkpoints de Git
sombra: um antes de rodar, outro depois. Se o turno não mudou nada, os dois são
descartados. Se mudou, o modo **Review** mostra aquele turno como uma única linha cujo
diff é a alteração inteira do início ao fim, para você ver exatamente o que o assistente
fez e desfazer se não concordar. O Git normal (seus próprios commits, branches e
histórico) fica na barra lateral de Controle de Versão e é totalmente separado desses
checkpoints.

## chat-memory :: Chats, memória e edição de mensagens

**Vários chats** — um projeto pode ter muitas conversas. Cada uma tem o seu próprio
histórico e o seu próprio contexto de trabalho, então uma longa sessão de depuração não
polui o chat em que você está escrevendo o seu artigo. Crie um com o `+` na barra lateral
de chats.

**Memória** — o assistente guarda fatos persistentes na *core memory* (coisas que valem a
pena lembrar entre conversas) e pode pesquisar todo o histórico de conversas do projeto.
Ele escreve na core memory depois de decisões relevantes; você não precisa gerenciar
isso.

**Limpar o chat** — "Limpar chat" é uma operação real no servidor: apaga o histórico
armazenado, reinicia o estado de trabalho do assistente, remove as entradas de arquivo
daquele chat e zera o indicador de contexto. Não é um reset de interface que deixaria o
modelo ainda lembrando de tudo.

**Editar uma mensagem** — nada é destruído:

- editar a sua **última** mensagem marca ela e tudo depois dela como substituído — a
  resposta some da tela, mas o histórico continua auditável;
- editar uma mensagem **anterior** ramifica a conversa em um novo chat e deixa a original
  intacta.

**Interromper** — você pode parar uma execução a qualquer momento. O que o assistente já
tinha feito é preservado como contexto, então dá para mandar continuar com instruções
corrigidas em vez de recomeçar do zero.

**Comandos de barra** — mensagens que começam com `/` são comandos do GilzaBots, e não
prompts: `/help`, `/clear`, `/skills`, `/models`, `/set-main-model <id>`,
`/set-worker-model <id>`, `/undo`, `/commit <msg>`, entre outros. Digitar `/` abre uma
lista dos comandos que se estreita conforme você digita: ↑/↓ para escolher, Enter para
executar (um comando que precisa de argumento vai para a caixa para você completar),
Tab para completar, Esc para fechar a lista. A saída de um comando abre em uma janela
própria e nunca é adicionada à conversa.

## command-line :: Posso usar o GilzaBots pelo terminal, sem a janela?

Pode. O mesmo agente, os mesmos projetos, as mesmas skills — renderizados num terminal
em vez de numa janela.

**Como iniciar** — `gilzabots --cli` abre a sessão interativa. Rode dentro do diretório de
um projeto e ele abre esse projeto; use `--here` para registrar o diretório atual como um
projeto novo, ou `--project <nome>` para escolher pelo nome.

**Uma pergunta só** — `gilzabots -p "corrija os erros de compilação do capítulo 3"` executa
um turno e sai, que é a forma de usar em um script ou Makefile. `-p -` lê o prompt da
entrada padrão.

**O que aparece enquanto ele trabalha** — a resposta vai saindo conforme o modelo escreve,
cada chamada de ferramenta aparece como `⏺ nome_da_ferramenta(...)` com o resultado
abaixo, e o raciocínio é mostrado em tom apagado. `/thoughts off` e `/tools off` silenciam
um ou outro.

**Interromper** — `Ctrl+C` para o turno em execução, não a sessão: o que o agente já havia
escrito é salvo, e `/resume` continua dali. `Ctrl+D` ou `/exit` encerra.

**Aprovações** — no modo `edit` o agente pergunta no terminal antes de cada alteração, e no
modo `plan` ele apresenta o plano para aprovação do mesmo jeito que a janela faz. `/mode`
alterna entre `auto`, `plan` e `edit`.

**Comandos** — `/help` lista tudo por seção, com os argumentos de cada comando, e
`/help <comando>` mostra os exemplos daquele comando (`/help /models`,
`/help /providers`, `/help /chat`). Tudo o que existe no chat da janela funciona aqui,
mais alguns que só fazem sentido num terminal: `/chat` para listar, criar e trocar de conversa,
`/effort` para definir o esforço de raciocínio do chat atual, `/compile` para gerar o PDF com
o Tectonic, `/cost` para ver quanto da janela de contexto está ocupada, `/resume`, e
`/monitors` para gerenciar os monitores em segundo plano (experimental; também `gilzabots monitors`).

**Instalar só isso** — a interface de linha de comando é a instalação base:
`pip install gilzabots` ou `uv tool install gilzabots` entrega o agente sem a pilha do
desktop. A janela precisa de `pip install "gilzabots[gui]"`. A compilação LaTeX usa um
`tectonic` encontrado no seu `PATH`.

**O que ela não faz** — tudo o que é editor e não agente: pré-visualização do PDF, SyncTeX,
os modos rich text e WYSIWYG, anotações em PDF, modo apresentação do PDF, ditado e leitura em voz
alta. Isso vive na janela.

## cli-models :: Como cadastro um modelo e um provedor pelo terminal?

Um modelo utilizável são dois registros, e você cria os dois a partir de qualquer
interface de texto — a sessão `--cli`, o `gilzabots -p`, o protocolo stdin, ou a caixa de
comandos deste chat.

**1. A conexão de provedor** guarda as credenciais, compartilhadas por todos os modelos
sob ela:

```
/providers add label="Ollama Gil" provider=ollama api_base=http://192.168.0.10:11434/v1
/providers add label="OpenAI" provider=openai api_key=sk-...
/providers                       # lista, com quantos modelos cada uma carrega
```

O id vem do rótulo (`ollama-gil`); passe `id=<seu-id>` para escolher. Rodar `add` de novo
com o mesmo id edita aquela conexão, e omitir `api_key` preserva a chave guardada — só
`api_key=` (vazio) limpa.

**2. O modelo de catálogo** nomeia um modelo sob uma conexão e o descreve:

```
/models add name=gemma4:26b connection=ollama-gil num_ctx=65000 supports_thinking=true
/models add name=gpt-4o-mini connection=openai temperature=0.3
/models list
/models show ollama/gemma4:26b
/models set ollama/gemma4:26b profile=light policy=delegate
/models remove ollama/gemma4:26b
```

**3. Aponte o projeto para ele:**

```
/set-main-model ollama/gemma4:26b
/set-worker-model ollama/gemma4:26b
```

**Por que os dois passos importam** — `/set-main-model` só guarda um id. Sem uma entrada
de catálogo por trás dele não há API base, nem chave, nem capacidades: a execução chega
ao provedor sem autenticação e toda configuração por modelo fica como não definida.

**Campos** — `name`, `connection` (apelido de `connection_id`), `id`, `num_ctx` (apelido
`context`), `supports_thinking` (apelido `thinking`), `requires_single_system_message`,
`prompt_profile` (apelido `profile`: `full` ou `light`), `orchestrator_policy` (apelido
`policy`: `direct` ou `delegate`), `temperature`, `max_tokens`, `seed`, `top_p`, `top_k`,
`min_p`, `frequency_penalty`, `presence_penalty`, `repetition_penalty`,
`reasoning_effort`, `supports_image_generation`, `image_route`,
`supports_speech_synthesis`, `speech_route`.

Qualquer outro campo é recusado em vez de adivinhado, para que um erro de digitação não
vire uma configuração invisível. Um parâmetro que você *quer* repassar ao provedor se
escreve com prefixo: `extra.keep_alive=30m`.

Valores com espaço vão entre aspas: `label="Meu provedor"`.
